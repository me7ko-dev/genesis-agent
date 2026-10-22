"""
genesis_agent.anthropic_spend — колко е похарчено с ANTHROPIC_API_KEY, таванът
и свършеният кредит.

Защо (2026-10-22): операторът получи предплатен кредит от $100 за ~20 дни и
иска Genesis да го ползва, докато свърши, а после да работи на безплатната
верига без грешки. Дотогава цена се записваше само ако доставчикът я каже сам
(`claude` CLI), Anthropic API-то не я казва — тоест нямаше как да се види
колко остава, нито да се спре навреме.

  • Цената на всеки отговор се смята от `usage` по таблицата долу. Непознат
    модел → без цена (None), не грешна: таблица, която тихо брои грешно, е
    по-лоша от липсваща.
  • Сборът през сесиите е в ~/.genesis/anthropic_spend.json (временен файл +
    os.replace, заключен между процесите — чат, bench и `genesis serve`
    пишат едновременно).
  • GENESIS_ANTHROPIC_MAX_USD (средата или ~/.genesis/.env): стигне ли сборът
    тавана, веригата прескача Anthropic като мъртъв ключ. Без таван — без
    граница; истинската стена е предплатеният баланс.
  • Свършен кредит (402 billing_error или 400 „credit balance is too low“)
    спира Anthropic до края на процеса, с един ред, а не 5-минутен cooldown.

Нов кредит → изтрий файла, за да започне броенето от нула.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from genesis_agent.paths import GENESIS_HOME, get_secret

log = logging.getLogger("genesis.anthropic_spend")

SPEND_PATH: Path = Path(GENESIS_HOME) / "anthropic_spend.json"
CAP_ENV = "GENESIS_ANTHROPIC_MAX_USD"

# $ за милион токена: (вход без кеша, изход, четене от кеша). Ценоразписът на
# Anthropic API към 2026-10-22. Изходът включва и мисленето (usage.output_tokens
# го брои). Четенето от кеша НЕ е навсякъде 0.1× от входа — при Opus 5.5 е
# 0.05× ($0.20 при $4), затова е отделна колона, не множител. Ключът е точното
# име: claude-opus-5 и claude-opus-5-5 са различни модели с различна цена, и
# сравнение по префикс би ги смесило.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    # По-старите: сървърният резервен модел (`fallbacks="default"`) може да
    # довърши отказана заявка на claude-opus-4-8, а haiku-4-5 беше в менюто.
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 0.50),
    "claude-opus-4-6": (5.00, 25.00, 0.50),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}
# Haiku 5.5 над 100K входни токена на заявка струва повече ($0.50 / $2.50).
_LONG_PROMPT: dict[str, tuple[int, tuple[float, float, float]]] = {
    "claude-haiku-5-5": (100_000, (0.50, 2.50, 0.05)),
}
# Записът в кеша: 1.25× от входа за 5 минути, 2× за 1 час. Запис без разбивка
# по TTL се брои по по-скъпата — таванът не бива да брои по-малко от сметката.
_WRITE_5M = 1.25
_WRITE_1H = 2.0

_lock = threading.Lock()
# Състоянието на сесията (процеса): свършил ли е кредитът, казано ли е вече.
_session: dict[str, Any] = {}


def reset_session() -> None:
    """Нова сесия: кредитът отново се пробва, известията пак се казват."""
    _session.clear()
    _session.update({"credit_gone": False, "cap_noticed": False, "unpriced": set()})


reset_session()


# ── цената на един отговор ──────────────────────────────────────────────────

def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _tokens(obj: Any, name: str) -> int:
    value = _get(obj, name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def usage_cost(model: str, usage: Any) -> float | None:
    """$ за един опит по неговия `usage`; None за модел без цена."""
    name = (model or "").strip().lower()
    price = PRICES.get(name)
    if price is None or usage is None:
        return None
    inp = _tokens(usage, "input_tokens")            # само некешираният остатък
    out = _tokens(usage, "output_tokens")
    read = _tokens(usage, "cache_read_input_tokens")
    write = _tokens(usage, "cache_creation_input_tokens")
    split = _get(usage, "cache_creation")
    w5 = _tokens(split, "ephemeral_5m_input_tokens")
    w1h = _tokens(split, "ephemeral_1h_input_tokens")
    unsplit = max(0, write - w5 - w1h)
    long = _LONG_PROMPT.get(name)
    if long and inp + read + write > long[0]:
        price = long[1]
    p_in, p_out, p_read = price
    cost = (inp * p_in + out * p_out + read * p_read
            + w5 * p_in * _WRITE_5M + (w1h + unsplit) * p_in * _WRITE_1H)
    return cost / 1_000_000


def response_cost(requested: str, resp: Any) -> float | None:
    """$ за целия отговор. При сървърен резервен модел `usage.iterations` е
    сметката за всеки опит (горното `usage` е само за последния), и всеки се
    плаща по цената на модела, който го е изпълнил."""
    usage = _get(resp, "usage")
    if usage is None:
        return None
    served = _get(resp, "model")
    served = served if isinstance(served, str) and served else requested
    iterations = _get(usage, "iterations")
    if isinstance(iterations, (list, tuple)) and iterations:
        total = 0.0
        for item in iterations:
            model = _get(item, "model")
            if not isinstance(model, str) or not model:
                model = served if _get(item, "type") == "fallback_message" else requested
            cost = usage_cost(model, item)
            if cost is None:
                return None        # част от сметката без цена — цялата е неизвестна
            total += cost
        return total
    return usage_cost(served, usage)


# ── сборът през сесиите ────────────────────────────────────────────────────

def _read() -> dict[str, Any]:
    try:
        data = json.loads(SPEND_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.debug("anthropic_spend: файлът не се чете — броенето започва от нула", exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, Any]) -> None:
    tmp = SPEND_PATH.with_name(f"{SPEND_PATH.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    # На Windows os.replace пада с PermissionError, докато друг процес чете
    # файла в същия миг — кратък повтор, иначе разходът се губи.
    for attempt in range(5):
        try:
            os.replace(tmp, SPEND_PATH)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05)


def total_usd_of(data: dict[str, Any]) -> float:
    value = data.get("total_usd", 0.0)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def total_usd() -> float:
    return total_usd_of(_read())


def add(cost: float, model: str) -> float:
    """Добавя един платен отговор; връща новия сбор. Никога не хвърля."""
    try:
        from genesis_agent.file_lock import locked
        SPEND_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _lock, locked(SPEND_PATH):
            data = _read()
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            total = round(total_usd_of(data) + float(cost), 6)
            stored = data.get("by_model")
            by_model: dict[str, Any] = stored if isinstance(stored, dict) else {}
            prev = by_model.get(model)
            before = float(prev) if isinstance(prev, (int, float)) and not isinstance(prev, bool) else 0.0
            by_model[model] = round(before + float(cost), 6)
            count = data.get("calls")
            calls = count if isinstance(count, int) and not isinstance(count, bool) else 0
            _write({"total_usd": total, "calls": calls + 1, "by_model": by_model,
                    "since": data.get("since") or now, "updated": now})
            return total
    except Exception:
        log.debug("anthropic_spend: разходът не се записа", exc_info=True)
        return total_usd()


# ── таванът ────────────────────────────────────────────────────────────────

def cap_setting() -> tuple[float | None, str]:
    """(таванът в $ или None, суровата стойност). Запетая или $ се приемат
    („0,50“, „$95“) — така ги пише човек; друго → None, а /doctor го казва."""
    raw = (get_secret(CAP_ENV) or "").strip()
    if not raw:
        return None, ""
    try:
        value = float(raw.replace("$", "").replace(",", ".").strip())
    except ValueError:
        return None, raw
    if not math.isfinite(value) or value < 0:
        return None, raw
    return value, raw


def max_usd() -> float | None:
    return cap_setting()[0]


def blocked() -> str | None:
    """Защо Anthropic да не се вика сега, или None. Казва го веднъж на сесия."""
    if _session.get("credit_gone"):
        return "кредитът свърши"
    cap = max_usd()
    if cap is None:
        return None
    spent = total_usd()
    if spent < cap:
        _session["cap_noticed"] = False     # таванът е вдигнат посред сесията
        return None
    if not _session.get("cap_noticed"):
        _session["cap_noticed"] = True
        print(f"  [Brain] 💳 Anthropic: похарчени ${spent:.2f} от ${cap:.2f} — "
              "минавам на безплатните модели")
    return f"таванът от ${cap:.2f} е достигнат"


def note_unpriced(model: str) -> None:
    """Отговор на модел без цена при зададен таван — казва се веднъж, защото
    иначе таванът тихо не важи за него."""
    if model in _session["unpriced"] or max_usd() is None:
        return
    _session["unpriced"].add(model)
    print(f"  [Brain] 💳 Anthropic: няма цена за {model} — разходът му не влиза в тавана")


# ── свършеният кредит ──────────────────────────────────────────────────────

def is_credit_error(error: str) -> bool:
    """402 billing_error или (по-старият вид) 400 „credit balance is too low“."""
    low = (error or "").lower()
    return (low.startswith("http_402") or "billing_error" in low
            or ("credit balance" in low and "too low" in low))


def mark_credit_gone() -> None:
    if _session.get("credit_gone"):
        return
    _session["credit_gone"] = True
    print("  [Brain] 💳 Кредитът в Anthropic свърши — продължавам с безплатните модели")


def credit_gone() -> bool:
    return bool(_session.get("credit_gone"))


def summary() -> str:
    """Един ред за `genesis budget` и /doctor (без ключа, само сумите)."""
    spent = total_usd()
    cap, raw = cap_setting()
    if cap is not None:
        return f"похарчени ${spent:.2f} от тавана ${cap:.2f}"
    if raw:
        return f"похарчени ${spent:.2f}; {CAP_ENV}={raw!r} не е сума в долари"
    return f"похарчени ${spent:.2f} (без таван — {CAP_ENV} не е зададен)"
