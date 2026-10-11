"""Етап 3 (2026-10-11): цени, таксуване на ход от шлюза, Stripe — само stdlib.

Парите са цели микрокредити (µc): 1 кредит = 1 000 000 µc, 100 кредита = 1 EUR.
Тарифите в prices.json са кредити на 1M токена — тоест точно µc на токен,
затова цялата сметка е с цели числа, без float.

Таксува се само по usage.jsonl на шлюза: токените в „done“ събитието ги казва
кодът в контейнера (чужд код), затова плащане без шлюз не тръгва.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from cloud.gateway import gateway as gw
from cloud.web.store import MICRO, Store

PRICES = Path(__file__).with_name("prices.json")
_ID = re.compile(r"^[a-z0-9_-]{1,32}$")


# ── цени ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Rate:
    """µc на токен (= кредити на 1M токена)."""
    input: int
    cached: int
    output: int

    @property
    def highest(self) -> int:
        return max(self.input, self.cached, self.output)


@dataclass(frozen=True)
class Offer:
    """Пакет кредити (еднократно) или план (месечен абонамент)."""
    id: str
    credits: int
    amount_cents: int
    currency: str
    stripe_price: str

    @property
    def price(self) -> str:
        return f"{Decimal(self.amount_cents).scaleb(-2):.2f} {self.currency.upper()}"


@dataclass(frozen=True)
class Prices:
    default: Rate
    models: Mapping[str, Rate]
    per_turn: int                  # кредити на ход (≈ минута контейнер)
    min_turn_tokens: int           # под толкова достъпни токена ход не тръгва
    packs: Mapping[str, Offer]
    plans: Mapping[str, Offer]

    def rate(self, provider: str, model: str) -> Rate:
        """Точно „доставчик/модел“, после „доставчик/*“, после default."""
        return (self.models.get(f"{provider}/{model}") or self.models.get(f"{provider}/*")
                or self.default)

    @property
    def max_rate(self) -> int:
        """Най-скъпият токен — по него парите се обръщат в таван на токени."""
        return self.default.highest

    @property
    def per_turn_micro(self) -> int:
        return self.per_turn * MICRO


def _rate_value(where: str, v: Any) -> int:
    # type() а не isinstance: true/1.5 в JSON не са тарифа.
    if type(v) is not int or v < 0:
        raise ValueError(f"{where}: тарифата трябва да е цяло число ≥ 0, а е {v!r}")
    return v


def _rate(where: str, d: Any) -> Rate:
    if not isinstance(d, dict) or set(d) != {"input", "cached", "output"}:
        raise ValueError(f"{where}: нужни са точно input, cached и output")
    return Rate(*(_rate_value(f"{where}.{k}", d[k]) for k in ("input", "cached", "output")))


def _offers(where: str, d: Any) -> dict[str, Offer]:
    if not isinstance(d, dict):
        raise ValueError(f"{where}: очаква се обект")  # noqa: TRY004 — serve хваща ValueError
    out: dict[str, Offer] = {}
    for oid, o in d.items():
        w = f"{where}.{oid}"
        if not _ID.match(oid) or not isinstance(o, dict):
            raise ValueError(f"{w}: името е [a-z0-9_-] до 32 знака, стойността — обект")
        credits, cents = o.get("credits"), o.get("amount_cents")
        currency, price = o.get("currency"), o.get("stripe_price", "")
        if type(credits) is not int or credits <= 0 or type(cents) is not int or cents <= 0:
            raise ValueError(f"{w}: credits и amount_cents трябва да са цели числа > 0")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency):
            raise ValueError(f"{w}: currency е код от 3 букви (напр. eur)")
        if not isinstance(price, str) or (price and not price.startswith("price_")):
            raise ValueError(f"{w}: stripe_price е празно или price_…")
        out[oid] = Offer(oid, credits, cents, currency.lower(), price)
    return out


def parse_prices(data: Any) -> Prices:
    if not isinstance(data, dict) or "default" not in data:
        raise ValueError("цените: липсва 'default'")
    default = _rate("default", data["default"])
    models: dict[str, Rate] = {}
    raw_models = data.get("models", {})
    if not isinstance(raw_models, dict):
        raise ValueError("цените: 'models' трябва да е обект")  # noqa: TRY004 — както горе
    for key, d in raw_models.items():
        provider, _, model = str(key).partition("/")
        if provider not in gw.UPSTREAMS or not model:
            raise ValueError(f"цените: '{key}' трябва да е доставчик/модел или доставчик/*, "
                             f"с доставчик от шлюза ({', '.join(gw.UPSTREAMS)})")
        r = _rate(f"models.{key}", d)
        # default е таванът (2026-10-11): името на модела в usage.jsonl е каквото
        # е пратил кодът в контейнера — псевдоним, друг регистър или измислено
        # име пада към default и никога не бива да е по-евтино от истинското.
        for f in ("input", "cached", "output"):
            if getattr(r, f) > getattr(default, f):
                raise ValueError(f"цените: models.{key}.{f} е над default.{f} — "
                                 "default трябва да е най-скъпото")
        models[str(key)] = r
    per_turn, min_tokens = data.get("per_turn", 0), data.get("min_turn_tokens", 0)
    _rate_value("per_turn", per_turn)
    _rate_value("min_turn_tokens", min_tokens)
    return Prices(default, models, per_turn, min_tokens,
                  _offers("packs", data.get("packs", {})), _offers("plans", data.get("plans", {})))


def load_prices(path: Path = PRICES) -> Prices:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ValueError(f"{path}: не се чете ({e.strerror})") from None
    except (ValueError, RecursionError):
        raise ValueError(f"{path}: невалиден JSON") from None
    return parse_prices(data)


# ── пари ─────────────────────────────────────────────────────────────────────

def fmt_credits(micro: int) -> str:
    """µc → кредити с два знака, банково закръгляне: 12 345 678 → '12.35'."""
    d = Decimal(micro).scaleb(-6).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return f"{d:.2f}" if d else "0.00"   # без '-0.00'


def parse_credits(text: str) -> int:
    """'12.5' → 12 500 000 µc. Повече от 6 знака след точката — ValueError."""
    try:
        d = Decimal(text.strip())
    except InvalidOperation:
        raise ValueError(f"'{text}' не е число") from None
    if not d.is_finite():
        raise ValueError(f"'{text}' не е число")
    micro = d.scaleb(6)
    if micro != micro.to_integral_value():
        raise ValueError(f"'{text}': най-много 6 знака след точката (1 µc)")
    return int(micro)


def turn_cost(prices: Prices, usage: list[dict[str, Any]]) -> int:
    """Цената на ход в µc: таксата на ход + токените по модели."""
    def n(v: Any) -> int:
        return v if type(v) is int and v > 0 else 0

    cost = prices.per_turn_micro
    for line in usage:
        r = prices.rate(str(line.get("provider", "")), str(line.get("model", "")))
        prompt, completion = n(line.get("prompt_tokens")), n(line.get("completion_tokens"))
        total = n(line.get("total_tokens"))
        if line.get("estimated"):
            # Шлюзът е записал само оценка — не се знае кое е вход и кое изход,
            # затова всичко по най-скъпата тарифа на модела.
            cost += max(total, prompt + completion) * r.highest
            continue
        cached = min(n(line.get("cached_tokens")), prompt)
        # Токените за разсъждение някои доставчици дават само в total.
        out = max(completion, total - prompt)
        cost += (prompt - cached) * r.input + cached * r.cached + out * r.output
    return cost


def charge_turn(store: Store, prices: Prices, user_id: int, turn_id: int,
                usage: list[dict[str, Any]]) -> bool:
    """Таксува хода веднъж (event_id turn:<id>). True ако е таксуван сега."""
    return store.add_entry(user_id, f"turn:{turn_id}", "charge", -turn_cost(prices, usage),
                           turn_id=turn_id, ref=json.dumps(usage, ensure_ascii=False)[:2000])


def reconcile(store: Store, prices: Prices, usage_path: Path) -> int:
    """Ходовете без такса (срив между края на хода и таксата, рестарт посред
    хода) — таксуват се от файла на шлюза. Повторно пускане не таксува нищо."""
    rows = store.unbilled_turns()
    if not rows:
        return 0
    usage = gw.usage_lines_by_job(usage_path, {r["job"] for r in rows})
    return sum(charge_turn(store, prices, r["user_id"], r["turn_id"], usage[r["job"]])
               for r in rows)
