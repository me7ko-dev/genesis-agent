"""Етап 3 (2026-10-11): цени, таксуване на ход от шлюза, Stripe — само stdlib.

Парите са цели микрокредити (µc): 1 кредит = 1 000 000 µc, 100 кредита = 1 EUR.
Тарифите в prices.json са кредити на 1M токена — тоест точно µc на токен,
затова цялата сметка е с цели числа, без float.

Таксува се само по usage.jsonl на шлюза: токените в „done“ събитието ги казва
кодът в контейнера (чужд код), затова плащане без шлюз не тръгва.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
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


def _tokens(usage: list[dict[str, Any]]) -> int:
    """Токените така, както ги брои шлюзът срещу бюджета (Usage.total_tokens)."""
    return sum(v for line in usage if type(v := line.get("total_tokens")) is int and v > 0)


def charge_turn(store: Store, prices: Prices, user_id: int, turn_id: int,
                usage: list[dict[str, Any]]) -> bool:
    """Таксува хода веднъж (event_id turn:<id>). True ако е таксуван сега.

    Токените до този момент се записват: задържаното до края на жетона е
    само неизхарченото от бюджета (held)."""
    new = store.add_entry(user_id, f"turn:{turn_id}", "charge", -turn_cost(prices, usage),
                          turn_id=turn_id, ref=json.dumps(usage, ensure_ascii=False)[:2000])
    if new:
        store.set_job_used(turn_id, _tokens(usage))
    return new


# Жетонът е проверен преди заявката (шлюзът проверява и след тялото), а една
# заявка с всички ключове е най-много UPSTREAM_TIMEOUT — +1 мин запас.
LATE_MARGIN = gw.UPSTREAM_TIMEOUT + 60


def settle_after(start: float, ttl: int) -> float:
    """След този момент шлюзът не може да запише нов разход за задачата."""
    return start + ttl + LATE_MARGIN


def _usage(usage_path: Path, rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Разходът на задачите в `rows` — от най-ранния usage_start сред тях.

    Деплой одит 2026-10-11: файлът на шлюза расте без край (1M реда — 3.3 s
    на цял прочит), а settle върви покрай всеки ход и всеки прием. По-рано от
    usage_start на задачата нейни редове няма (записан е преди контейнера)."""
    start = min((int(r.get("usage_start") or 0) for r in rows), default=0)
    return gw.usage_lines_by_job(usage_path, {r["job"] for r in rows}, start)


def settle(store: Store, prices: Prices, usage_path: Path, *, now: float | None = None,
           user_id: int | None = None) -> int:
    """Окончателната сметка на ходовете, чийто жетон вече не може да харчи.

    Ревю 2026-10-11: ходът се таксуваше веднъж, щом контейнерът спре, а
    жетонът живее до limits.seconds + 60 s — записан в /work, следващият ход
    харчеше по него без такса. Разликата след изтичането е втори ред
    turn:<id>:late; и двата са по event_id, затова повторно пускане не таксува
    втори път. Връща броя на новите редове."""
    now = time.time() if now is None else now
    due = [r for r in store.unsettled(user_id) if r["status"] in ("ok", "failed")
           and (r["settle_after"] is None or r["settle_after"] <= now)]
    if not due:
        return 0
    usage = _usage(usage_path, due)
    written = 0
    for r in due:
        lines = usage[r["job"]]
        if r["charged"] is None:
            written += charge_turn(store, prices, r["user_id"], r["turn_id"], lines)
        else:
            late = turn_cost(prices, lines) - r["charged"]
            if late > 0:
                written += store.add_entry(
                    r["user_id"], f"turn:{r['turn_id']}:late", "charge", -late,
                    turn_id=r["turn_id"], ref=json.dumps(lines, ensure_ascii=False)[:2000])
        store.mark_settled(r["turn_id"])
    return written


def held(store: Store, prices: Prices, user_id: int) -> int:
    """µc, задържани от баланса за ходове с още жив жетон: колкото най-много
    може да изхарчи жетонът до края си. Без това ход 1 харчеше малко, ход 2
    получаваше почти целия баланс, а жетонът на ход 1 още можеше да изхарчи
    своя бюджет — два бюджета за парите на един (ревю 2026-10-11)."""
    total = 0
    for r in store.unsettled(user_id):
        if r["charged"] is None:
            total += prices.per_turn_micro + r["budget"] * prices.max_rate
        else:
            total += max(0, r["budget"] - (r["used"] or 0)) * prices.max_rate
    return total


def reconcile(store: Store, prices: Prices, usage_path: Path, *,
              now: float | None = None) -> int:
    """Ходовете без такса (срив между края на хода и таксата, рестарт посред
    хода) — таксуват се от файла на шлюза; после окончателната сметка на тези
    с изтекъл жетон (settle). Повторно пускане не таксува нищо."""
    rows = store.unbilled_turns()
    charged = 0
    if rows:
        usage = _usage(usage_path, rows)
        charged = sum(charge_turn(store, prices, r["user_id"], r["turn_id"], usage[r["job"]])
                      for r in rows)
    return charged + settle(store, prices, usage_path, now=now)


# ── Stripe ───────────────────────────────────────────────────────────────────

STRIPE_API = "https://api.stripe.com/v1"
CHECKOUT_PREFIX = "https://checkout.stripe.com/"
WEBHOOK_TOLERANCE = 300


class StripeError(Exception):
    """Stripe не отговори както трябва — подробностите са в stderr, не при клиента."""


@dataclass(frozen=True)
class StripeConfig:
    secret_key: str
    webhook_secret: str
    public_url: str = ""

    @property
    def live(self) -> bool:
        return self.secret_key.startswith(("sk_live_", "rk_live_"))

    @classmethod
    def from_env(cls, environ: Mapping[str, str], public_url: str = "") -> StripeConfig | None:
        """Ключовете само от средата — никога от флаг (видим в `ps`) или от
        prices.json. Нито един → None (плащането с карта е изключено); само
        единият или грешен префикс → ValueError: половин настроено плащане
        взима пари, без да зарежда кредити (2026-10-11)."""
        key = environ.get("STRIPE_SECRET_KEY", "").strip()
        hook = environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
        if not key and not hook:
            return None
        if not key or not hook:
            raise ValueError("Stripe иска и STRIPE_SECRET_KEY, и STRIPE_WEBHOOK_SECRET — "
                             "зададен е само единият")
        if not key.startswith(("sk_", "rk_")):
            raise ValueError("STRIPE_SECRET_KEY трябва да започва със sk_ или rk_ "
                             "(pk_ е публичният ключ)")
        if not hook.startswith("whsec_"):
            raise ValueError("STRIPE_WEBHOOK_SECRET трябва да започва с whsec_")
        return cls(key, hook, public_url)


def check_public_url(url: str, *, allow_http: bool = False) -> str:
    """Адресът за връщане от Stripe — от настройките, никога от Host заглавието
    (иначе чужд Host праща човека след плащането където си иска)."""
    url = url.strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    schemes = ("https", "http") if allow_http else ("https",)
    if parts.scheme not in schemes or not parts.netloc or parts.query or parts.fragment:
        raise ValueError("--public-url трябва да е https://адрес (http:// само с --insecure-cookie)")
    return url


def verify_stripe_signature(payload: bytes, header: str, secret: str, *,
                            now: float | None = None, tolerance: int = WEBHOOK_TOLERANCE) -> bool:
    """Подписът `Stripe-Signature: t=…,v1=…` върху СУРОВИТЕ байтове на тялото.

    Повече от един v1 (смяна на тайната) — стига един да съвпада; v0 и
    непознати схеми не се броят. Времето е ±tolerance: стара доставка,
    повторена от някой друг, не минава след 5 минути."""
    if not header or not secret:
        return False
    stamp: str | None = None
    sigs: list[str] = []
    for item in header.split(","):
        key, sep, value = item.strip().partition("=")
        if not sep:
            continue
        if key == "t" and stamp is None:
            stamp = value
        elif key == "v1":
            sigs.append(value)
    # До 12 цифри (ревю 2026-10-11): int() на над 4300 цифри хвърля ValueError
    # (3.10.7+), а webhook-ът го пускаше нагоре — прекъсната връзка вместо 400.
    if (stamp is None or len(stamp) > 12 or not (stamp.isascii() and stamp.isdigit())
            or not sigs):
        return False
    if abs((time.time() if now is None else now) - int(stamp)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), stamp.encode() + b"." + payload,
                        hashlib.sha256).hexdigest().encode()
    # Байтове, не str: compare_digest хвърля TypeError на не-ASCII низ, а
    # заглавието е каквото е пратил клиентът. Без ранен изход — всички се сравняват.
    ok = False
    for sig in sigs:
        ok |= hmac.compare_digest(expected, sig.encode("utf-8", "replace"))
    return ok


Opener = Callable[..., Any]


def create_checkout(cfg: StripeConfig, *, user_id: int, email: str, offer: Offer, mode: str,
                    opener: Opener = urllib.request.urlopen) -> str:
    """Нова Stripe Checkout сесия → адресът ѝ (https://checkout.stripe.com/…).

    Винаги customer_email, никога стар клиент (ревю 2026-10-11): всеки
    абонамент е свой клиент, затова фактурата по клиента казва точно кой план
    е платен — и два раздела с плана, платени и двата, зареждат и двата."""
    if mode not in ("payment", "subscription"):
        raise ValueError(mode)
    what = "pack" if mode == "payment" else "plan"
    form = [
        ("mode", mode),
        ("line_items[0][price]", offer.stripe_price),
        ("line_items[0][quantity]", "1"),
        ("success_url", f"{cfg.public_url}/?view=account&paid=1"),
        ("cancel_url", f"{cfg.public_url}/?view=account"),
        ("client_reference_id", str(user_id)),
        ("metadata[user_id]", str(user_id)),
        (f"metadata[{what}]", offer.id),
    ]
    if mode == "subscription":
        form += [("subscription_data[metadata][user_id]", str(user_id)),
                 ("subscription_data[metadata][plan]", offer.id)]
    form.append(("customer_email", email))
    req = urllib.request.Request(
        f"{STRIPE_API}/checkout/sessions", data=urllib.parse.urlencode(form).encode(),
        method="POST", headers={
            "Authorization": f"Bearer {cfg.secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
            # Повторена заявка (мрежата) не прави втора сесия.
            "Idempotency-Key": str(uuid.uuid4()),
        })
    try:
        with opener(req, timeout=20) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:2000].decode("utf-8", "replace")
        _log(f"checkout {e.code}: {detail}")
        raise StripeError(f"stripe {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        _log(f"checkout: {type(e).__name__}: {e}")
        raise StripeError(type(e).__name__) from None
    try:
        url = json.loads(body).get("url")
    except (ValueError, AttributeError, RecursionError):
        url = None
    if not isinstance(url, str) or not url.startswith(CHECKOUT_PREFIX):
        _log(f"checkout без адрес на checkout.stripe.com: {url!r}"[:300])
        raise StripeError("bad url")
    return url


def _log(msg: str) -> None:
    print(f"stripe: {msg}", file=sys.stderr)


def _str(obj: dict[str, Any], key: str) -> str:
    v = obj.get(key)
    return v if isinstance(v, str) else ""


def handle_event(store: Store, prices: Prices, event: dict[str, Any], *,
                 live: bool) -> tuple[int, str]:
    """Подписано събитие от Stripe → (HTTP статус, бележка).

    Само полетата най-горе в обекта (id, customer, status, amount_*,
    currency, metadata, client_reference_id): пътищата към редовете и цените
    на фактурата се местиха между версиите на API-то (2025 „basil“ ги сложи
    под parent/pricing), тези — не. Всеки запис е един ред с ключ по сесията
    или фактурата, затова повторена доставка не зарежда втори път."""
    if event.get("livemode") is not live:
        # Тестово събитие никога не зарежда истинската база (и обратно).
        return 200, "друг режим (test/live) — пропуснато"
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    if not isinstance(obj, dict):
        return 200, "без обект — пропуснато"
    etype = _str(event, "type")
    ref = _str(event, "id")
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return _checkout_done(store, prices, obj, ref)
    if etype == "invoice.paid":
        return _invoice_paid(store, prices, obj, ref)
    if etype == "customer.subscription.deleted":
        # Само абонаментът на този клиент; друг активен остава (ревю 2026-10-11).
        store.end_subscription(_str(obj, "customer"))
        return 200, "абонаментът е спрян"
    return 200, "пропуснато"


def _checkout_done(store: Store, prices: Prices, obj: dict[str, Any], ref: str) -> tuple[int, str]:
    ref_id = _str(obj, "client_reference_id")
    uid = int(ref_id) if ref_id.isascii() and ref_id.isdigit() else None
    if uid is None or not store.user_exists(uid):
        _log(f"сесия {_str(obj, 'id')}: непознат човек {ref_id!r}")
        return 200, "непознат човек — пропуснато"
    raw_meta = obj.get("metadata")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    paid = obj.get("payment_status")
    if obj.get("mode") == "payment":
        # Бавните методи (SEPA) идват като completed с unpaid и после
        # async_payment_succeeded — същата функция, същият ключ по сесията.
        if paid != "paid":
            return 200, "още не е платено"
        pack = prices.packs.get(str(meta.get("pack", "")))
        session = _str(obj, "id")
        if pack is None or not session.startswith("cs_"):
            _log(f"сесия {session}: непознат пакет {meta.get('pack')!r}")
            return 200, "непознат пакет — пропуснато"
        if (obj.get("amount_total") != pack.amount_cents
                or _str(obj, "currency").lower() != pack.currency):
            # Грешен price id в таблото не бива да зарежда пакет на чужда цена.
            _log(f"сесия {session}: {obj.get('amount_total')} {obj.get('currency')} ≠ "
                 f"{pack.amount_cents} {pack.currency} за {pack.id} — без кредити")
            return 200, "сумата не съвпада — пропуснато"
        store.add_entry(uid, f"stripe:{session}", "purchase", pack.credits * MICRO, ref=ref)
        return 200, "заредено"
    if obj.get("mode") == "subscription":
        if paid not in ("paid", "no_payment_required"):
            return 200, "още не е платено"
        plan = prices.plans.get(str(meta.get("plan", "")))
        customer = _str(obj, "customer")
        if plan is None or not customer.startswith("cus_"):
            _log(f"сесия {_str(obj, 'id')}: непознат план {meta.get('plan')!r} или клиент")
            return 200, "непознат план — пропуснато"
        # Кредитите идват с invoice.paid; тук само кой е клиентът и планът.
        # Нов ред за всеки клиент — вторият абонамент не изтрива първия.
        if not store.link_customer(customer, uid, plan.id):
            _log(f"клиент {customer} вече е на човек {store.user_by_customer(customer)}, "
                 f"не на {uid}")
            return 200, "клиентът е на друг човек — пропуснато"
        return 200, "абонаментът е записан"
    return 200, "пропуснато"


def _invoice_paid(store: Store, prices: Prices, obj: dict[str, Any], ref: str) -> tuple[int, str]:
    customer = store.customer(_str(obj, "customer"))
    if customer is None:
        # Stripe не пази реда: invoice.paid може да дойде преди
        # checkout.session.completed. 503 → Stripe опитва пак.
        return 503, "непознат клиент — ще се опита пак"
    uid = customer["user_id"]
    # Планът на клиента, не на профила: всеки абонамент е свой клиент.
    plan = prices.plans.get(customer["plan"])
    invoice = _str(obj, "id")
    if plan is None or not invoice.startswith("in_"):
        _log(f"фактура {invoice}: човек {uid} без план ({customer['plan']!r})")
        return 200, "без план — пропуснато"
    amount = obj.get("amount_paid")
    if (obj.get("status") != "paid" or type(amount) is not int or amount < plan.amount_cents
            or _str(obj, "currency").lower() != plan.currency):
        _log(f"фактура {invoice}: {obj.get('status')} {amount} {obj.get('currency')} "
             f"не покрива {plan.id} — без кредити")
        return 200, "сумата не съвпада — пропуснато"
    store.add_entry(uid, f"stripe:{invoice}", "subscription", plan.credits * MICRO, ref=ref)
    return 200, "заредено"
