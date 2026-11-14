"""Готово ли е за истински пари — без нито едно обръщение към Stripe (2026-11-13).

Чете точно това, с което върви сървърът на машината:
  /etc/default/genesis-web   GENESIS_WEB_ARGS (пише го cloud/deploy.sh от /srv/genesis/web.args)
  /srv/genesis/stripe.env    STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET (EnvironmentFile на услугата)
и файловете, към които сочат аргументите (цените, keys.env, --legal).

    sudo python3 -m cloud.golive_check
    sudo python3 -m cloud.golive_check --stripe-prices /root/stripe-prices.json

Сумите в prices.json се сверяват със Stripe само офлайн: `stripe prices list
--live --limit 100 > stripe-prices.json` (Stripe CLI на оператора) и после
`--stripe-prices`. Ключовете не се печатат — само дали са във верния вид.
Код 0 — „готово за live“; 1 — какво липсва, на български.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cloud.gateway import gateway as gw
from cloud.web import billing, legal
from cloud.web import server as web

DEFAULTS = Path("/etc/default/genesis-web")
STRIPE_ENV = Path("/srv/genesis/stripe.env")
REPO = Path(__file__).resolve().parent.parent
LIVE_KEY = re.compile(r"(sk|rk)_live_[A-Za-z0-9]{16,}")
WEBHOOK_KEY = re.compile(r"whsec_[A-Za-z0-9+/=]{16,}")
# `доставчик/*` пуска всеки модел на доставчика, а там цените са от $0.05 до
# над $10 на 1M — таксата по един ред не покрива най-скъпия (README, Етап 3).
RISKY_WILDCARD = ("openai", "openrouter", "together", "huggingface", "gemini")
# Условията на доставчика забраняват платен продукт (проверено 2026-11-13):
# NVIDIA API Trial Terms — „not in production“; Cerebras Developer — „not
# intended for production use“ (production е Enterprise по договор).
NOT_FOR_PRODUCTION = {
    "nvidia": "пробният достъп на NVIDIA е само за тестове („not in production“)",
}
WARN_PRODUCTION = {
    "cerebras": "Cerebras Developer е „not intended for production“ — нужен е Enterprise договор",
}


@dataclass(frozen=True)
class Check:
    ok: bool
    text: str
    warn: bool = False        # не спира live, само се казва


def _env_file(path: Path) -> dict[str, str]:
    """KEY=value на ред (като EnvironmentFile на systemd), без кавички около стойността."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def _private(path: Path) -> bool:
    """Само root (и групата на шлюза) — никой друг не чете тайните."""
    if os.name == "nt":
        return True         # на Windows няма тези права; машината е Linux
    return not (path.stat().st_mode & stat.S_IRWXO)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def check_stripe_keys(env: Mapping[str, str]) -> list[Check]:
    key = env.get("STRIPE_SECRET_KEY", "").strip()
    hook = env.get("STRIPE_WEBHOOK_SECRET", "").strip()
    out: list[Check] = []
    if LIVE_KEY.fullmatch(key):
        out.append(Check(True, "STRIPE_SECRET_KEY е live ключ (sk_live_/rk_live_)"))
    elif key.startswith(("sk_test_", "rk_test_")):
        out.append(Check(False, "STRIPE_SECRET_KEY е още тестов — сложи live ключа в stripe.env"))
    else:
        out.append(Check(False, "STRIPE_SECRET_KEY липсва или не е във вида sk_live_…/rk_live_…"))
    out.append(Check(bool(WEBHOOK_KEY.fullmatch(hook)),
                     "STRIPE_WEBHOOK_SECRET е във вида whsec_…" if WEBHOOK_KEY.fullmatch(hook)
                     else "STRIPE_WEBHOOK_SECRET липсва или не е whsec_… (от live webhook-а)"))
    return out


def check_public_url(url: str) -> Check:
    try:
        url = billing.check_public_url(url)
    except ValueError:
        return Check(False, "--public-url липсва или не е https://адрес")
    host = url.split("://", 1)[1].split("/", 1)[0].split(":")[0].lower()
    if host in ("localhost", "127.0.0.1") or re.search(
            r"(^|\.)(example\.(com|org|net)|example|test|local|invalid|localhost)$", host):
        return Check(False, f"--public-url е пробен адрес ({host}) — нужен е истинският домейн")
    return Check(True, f"--public-url е https ({host})")


def _stripe_index(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("очаква се изходът на `stripe prices list` (обект с data)")  # noqa: TRY004 — хваща се като ValueError
    return {p["id"]: p for p in items if isinstance(p, dict) and isinstance(p.get("id"), str)}


def check_offers(prices: billing.Prices, stripe_prices: Path | None) -> list[Check]:
    out: list[Check] = []
    offers = [("пакет", o, "one_time") for o in prices.packs.values()]
    offers += [("план", o, "recurring") for o in prices.plans.values()]
    if not offers:
        return [Check(False, "в цените няма нито пакет, нито план — няма какво да се купи")]
    index: dict[str, dict[str, Any]] | None = None
    if stripe_prices is not None:
        try:
            index = _stripe_index(stripe_prices)
        except (OSError, ValueError, KeyError, RecursionError) as e:
            out.append(Check(False, f"--stripe-prices не се чете: {e}"))
    for kind, o, want in offers:
        name = f"{kind} {o.id} ({o.price})"
        if not o.stripe_price:
            out.append(Check(False, f"{name}: няма stripe_price"))
            continue
        if index is None:
            continue
        p = index.get(o.stripe_price)
        if p is None:
            out.append(Check(False, f"{name}: {o.stripe_price} го няма в --stripe-prices"))
            continue
        bad = []
        if p.get("livemode") is not True:
            bad.append("не е live цена")
        if p.get("active") is not True:
            bad.append("не е активна")
        if p.get("unit_amount") != o.amount_cents or str(p.get("currency", "")).lower() != o.currency:
            bad.append(f"в Stripe е {p.get('unit_amount')} {p.get('currency')}, "
                       f"а в цените {o.amount_cents} {o.currency}")
        if p.get("type") != want:
            bad.append(f"видът е {p.get('type')}, а трябва {want}")
        rec = p.get("recurring")
        if want == "recurring" and not (isinstance(rec, dict) and rec.get("interval") == "month"
                                        and rec.get("interval_count", 1) == 1):
            bad.append("не е месечна")
        out.append(Check(not bad, f"{name}: " + ("; ".join(bad) if bad else "съвпада със Stripe")))
    if index is None and stripe_prices is None:
        out.append(Check(False, "сумите не са сверени със Stripe: `stripe prices list --live "
                                "--limit 100 > stripe-prices.json`, после --stripe-prices"))
    return out


def check_models(prices: billing.Prices, keys: Mapping[str, str]) -> list[Check]:
    try:
        models = gw.read_models(dict(keys))
        billing.check_models(prices, models)
    except ValueError as e:
        return [Check(False, str(e))]
    assert models is not None          # check_models отказва празен списък
    out = [Check(True, f"GATEWAY_MODELS: {', '.join(sorted(models))} — всеки има цена")]
    for m in sorted(models):
        provider, _, model = m.partition("/")
        if model == "*" and provider in RISKY_WILDCARD:
            out.append(Check(False, f"{m}: изброй моделите поотделно — цените при {provider} "
                                    "са много различни"))
        if provider in NOT_FOR_PRODUCTION:
            out.append(Check(False, f"{m}: {NOT_FOR_PRODUCTION[provider]}"))
        if provider in WARN_PRODUCTION:
            out.append(Check(False, f"{m}: {WARN_PRODUCTION[provider]}", warn=True))
        env = gw.UPSTREAMS[provider][1]
        if not keys.get(env, "").strip():
            out.append(Check(False, f"{m}: в keys.env няма {env}"))
    return out


def check_legal(path: Path | None) -> list[Check]:
    if path is None:
        return [Check(False, "няма --legal: правните страници са с празни места {{ПРОДАВАЧ}}…")]
    try:
        values = legal.load_values(path)
    except ValueError as e:
        return [Check(False, f"--legal: {e}")]
    out = []
    empty = legal.missing(values)
    out.append(Check(not empty, "правните страници: всички данни на продавача са попълнени"
                     if not empty else f"--legal: не са попълнени {', '.join(empty)}"))
    left = sorted({f"{page}.{lang}: {{{{{s}}}}}" for page in legal.PAGES for lang in legal.LANGS
                   for s in legal.slots(legal.render(page, lang, values))})
    if left and not empty:
        out.append(Check(False, f"празни места в страниците: {', '.join(left)}"))
    out.append(Check(legal.reviewed(values),
                     "правните страници са прегледани (без „чернова“)" if legal.reviewed(values)
                     else f"страниците са още „чернова“ — след преглед от юрист "
                          f"\"{legal.REVIEWED}\": \"да\" в --legal"))
    return out


def run_checks(args_line: str, stripe_env: Mapping[str, str], *,
               stripe_prices: Path | None = None, repo: Path = REPO) -> list[Check]:
    out: list[Check] = []
    try:
        a = web.build_parser().parse_args(["serve", *shlex.split(args_line)])
    except SystemExit:
        return [Check(False, "GENESIS_WEB_ARGS не се чете като аргументи на serve")]
    if not a.billing:
        out.append(Check(False, "сървърът е без --billing — плащането е изключено "
                                "(добави го в /srv/genesis/web.args и пусни deploy.sh)"))
    if not a.gateway_usage or not a.env_file:
        out.append(Check(False, "без --gateway-usage и --env-file таксата няма откъде да дойде"))
    out.extend(check_stripe_keys(stripe_env))
    out.append(check_public_url(a.public_url or stripe_env.get("GENESIS_PUBLIC_URL", "")))

    try:
        prices = billing.load_prices(Path(a.prices))
    except ValueError as e:
        out.append(Check(False, f"цените: {e}"))
        prices = None
    if prices is not None:
        if _inside(Path(a.prices), repo):
            out.append(Check(False, "цените с price_… са в репото — сложи ги в /srv/genesis/"
                                    "prices.json (--prices): git pull ги презаписва, а репото "
                                    "е публично"))
        out.extend(check_offers(prices, stripe_prices))
        keys: dict[str, str] = {}
        if a.env_file:
            try:
                keys = gw.read_keys(Path(a.env_file))
            except OSError as e:
                out.append(Check(False, f"--env-file не се чете: {e.strerror}"))
        out.extend(check_models(prices, keys))
        if len(keys.get("GATEWAY_SECRET", "")) < 32:
            out.append(Check(False, "в keys.env няма GATEWAY_SECRET от поне 32 знака"))
        if a.env_file and Path(a.env_file).exists() and not _private(Path(a.env_file)):
            out.append(Check(False, f"{a.env_file} се чете от всички — chmod 640"))

    try:
        cap = billing.parse_credits(a.monthly_cap)
    except ValueError:
        cap = 0
    out.append(Check(cap > 0, f"месечен таван: {billing.fmt_credits(cap)} кредита на човек"
                     if cap > 0 else "месечният таван (--monthly-cap) е 0 или невалиден"))
    out.append(Check(a.turns_per_day > 0, f"дневен лимит: {a.turns_per_day} задачи"
                     if a.turns_per_day > 0 else "дневният лимит (--turns-per-day) е 0"))
    out.append(Check(a.max_tokens_per_turn > 0, f"таван на ход: {a.max_tokens_per_turn} токена"
                     if a.max_tokens_per_turn > 0 else "--max-tokens-per-turn е 0"))
    out.extend(check_legal(a.legal))
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="python -m cloud.golive_check",
                                description="готово ли е за live — без обръщение към Stripe")
    p.add_argument("--defaults", type=Path, default=DEFAULTS,
                   help="файлът с GENESIS_WEB_ARGS (по подразбиране /etc/default/genesis-web)")
    p.add_argument("--stripe-env", type=Path, default=STRIPE_ENV,
                   help="Stripe ключовете на услугата (по подразбиране /srv/genesis/stripe.env)")
    p.add_argument("--stripe-prices", type=Path,
                   help="изходът на `stripe prices list --live --limit 100` (JSON)")
    a = p.parse_args(argv)
    checks: list[Check] = []
    try:
        args_line = _env_file(a.defaults).get("GENESIS_WEB_ARGS", "")
    except OSError:
        checks.append(Check(False, f"няма {a.defaults} — пусни cloud/deploy.sh"))
        args_line = ""
    stripe_env: dict[str, str] = {}
    try:
        stripe_env = _env_file(a.stripe_env)
        if not _private(a.stripe_env):
            checks.append(Check(False, f"{a.stripe_env} се чете от всички — chmod 600"))
    except OSError:
        checks.append(Check(False, f"няма {a.stripe_env} (STRIPE_SECRET_KEY и "
                                   "STRIPE_WEBHOOK_SECRET за услугата)"))
    checks += run_checks(args_line, stripe_env, stripe_prices=a.stripe_prices)
    for c in checks:
        print(f"  {'✓' if c.ok else ('!' if c.warn else '✗')} {c.text}")
    blocking = [c for c in checks if not c.ok and not c.warn]
    if blocking:
        print(f"\nНЕ Е ГОТОВО ЗА LIVE — липсват {len(blocking)} неща:")
        for c in blocking:
            print(f"  - {c.text}")
        return 1
    print("\nГОТОВО ЗА LIVE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
