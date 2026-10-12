"""Плащането от край до край в тестовия режим на Stripe — за 5 минути.

    STRIPE_SECRET_KEY=sk_test_… python -m cloud.stripe_check prices [--prices P]
    STRIPE_SECRET_KEY=sk_test_… python -m cloud.stripe_check checkout ana@example.com \\
        --data DIR [--prices P] [--pack p5 | --plan basic] [--public-url URL]
    STRIPE_WEBHOOK_SECRET=whsec_… python -m cloud.stripe_check simulate ana@example.com \\
        --data DIR [--url http://127.0.0.1:8080] [--prices P] [--pack p5]

Защо (2026-10-12): тестовете в tests/test_cloud_billing.py минават с фалшив
Stripe, а истинският Stripe с ключовете на оператора не е пускан нито веднъж.
Грешен price_… в prices.json, сума в таблото, различна от amount_cents, или
whsec_ от друг `stripe listen` значат „парите са взети, кредити няма“ — а
това се вижда чак при първия истински клиент. Тук:

  - prices   — всяка stripe_price съществува в тестовия режим (GET /v1/prices/<id>),
               активна е, сумата и валутата съвпадат с пакета/плана (иначе
               webhook-ът не зарежда), пакетът е еднократен, планът — месечен;
  - checkout — prices + Checkout сесия по пътя на сървъра (billing.create_checkout),
               адресът за плащане с 4242 4242 4242 4242 и чакане, докато в
               web.db се появи редът от webhook-а; балансът преди и след;
  - simulate — без мрежа навън: подписано checkout.session.completed към
               пуснатия локален сървър; зарежда точно веднъж, повторът не
               зарежда втори път, чужд подпис → 400.

Ключовете само от средата (както в сървъра): флаг се вижда в `ps`. Live ключ
(sk_live_/rk_live_) — отказ още преди всичко: тук се плаща с тестова карта.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from cloud.web import billing
from cloud.web.store import MICRO, Store

TEST_CARD = "4242 4242 4242 4242"
WEBHOOK_PATH = "/api/stripe/webhook"     # същият адрес като в cloud/web/server.py do_POST
LIVE_PREFIXES = ("sk_live_", "rk_live_")
TEST_PREFIXES = ("sk_test_", "rk_test_")

Opener = Callable[..., Any]


class CheckError(Exception):
    """Настройката не позволява проверка — код 2, без нищо пратено навън."""


# ── ключове и подпис ─────────────────────────────────────────────────────────

def refuse_live(environ: Mapping[str, str]) -> None:
    """Live ключ в средата → отказ, каквато и команда да е: checkout с него би
    взел истински пари, а simulate е тест само срещу тестов сървър."""
    key = environ.get("STRIPE_SECRET_KEY", "").strip()
    if key.startswith(LIVE_PREFIXES):
        raise CheckError("STRIPE_SECRET_KEY е live ключ (sk_live_/rk_live_) — отказвам. "
                         "Тази проверка е само с тестови ключове (sk_test_…)")


def test_key(environ: Mapping[str, str]) -> str:
    refuse_live(environ)
    key = environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key.startswith(TEST_PREFIXES):
        raise CheckError("нужен е STRIPE_SECRET_KEY=sk_test_… в средата "
                         "(Stripe табло → Developers → API keys, с включен Test mode)")
    return key


def webhook_secret(environ: Mapping[str, str]) -> str:
    hook = environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not hook.startswith("whsec_"):
        raise CheckError("нужен е STRIPE_WEBHOOK_SECRET=whsec_… в средата — същият, с който "
                         "върви сървърът (от `stripe listen --print-secret`)")
    return hook


def sign(payload: bytes, secret: str, t: int | None = None) -> str:
    """Заглавието `Stripe-Signature`, както го праща Stripe: HMAC-SHA256 върху
    „<t>.<сурово тяло>“. Проверява се с billing.verify_stripe_signature."""
    stamp = int(time.time()) if t is None else t
    sig = hmac.new(secret.encode(), f"{stamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={sig}"


def _redact(text: str, key: str) -> str:
    return text.replace(key, key[:8] + "…") if key else text


# ── Stripe API (само четене на цени) ─────────────────────────────────────────

def _get(path: str, key: str, opener: Opener) -> tuple[int, dict[str, Any]]:
    """GET към Stripe → (HTTP код, JSON). Мрежова грешка е код 0."""
    req = urllib.request.Request(f"{billing.STRIPE_API}/{path}",
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with opener(req, timeout=20) as r:
            body = r.read()
        code = 200
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": {"message": f"{type(e).__name__}: {e}"}}
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        data = None
    return code, data if isinstance(data, dict) else {}


def _stripe_error(data: dict[str, Any], key: str) -> str:
    err = data.get("error")
    msg = err.get("message") if isinstance(err, dict) else None
    return _redact(str(msg or "без обяснение")[:200], key)


def check_prices(prices: billing.Prices, key: str, opener: Opener,
                 out: Callable[[str], None] = print) -> list[str]:
    """Всяка stripe_price от prices.json срещу тестовия режим → списък с проблеми."""
    problems: list[str] = []
    priced = 0
    for kind, offers, want in (("пакет", prices.packs, "one_time"),
                               ("план", prices.plans, "recurring")):
        for o in offers.values():
            if not o.stripe_price:
                out(f"  {kind} {o.id}: без stripe_price — не се предлага, пропускам")
                continue
            priced += 1
            where = f"{kind} {o.id} ({o.stripe_price})"
            code, p = _get(f"prices/{urllib.parse.quote(o.stripe_price, safe='')}", key, opener)
            if code != 200:
                hint = " — няма я в тестовия режим (live цена или друг акаунт?)" if code == 404 else ""
                problems.append(f"{where}: Stripe {code or 'без връзка'}: "
                                f"{_stripe_error(p, key)}{hint}")
                continue
            bad: list[str] = []
            if p.get("livemode") is not False:
                bad.append("не е тестова цена")
            if p.get("active") is not True:
                bad.append("неактивна е")
            if p.get("type") != want:
                bad.append(f"вид {p.get('type')!r}, а трябва {want!r}")
            if want == "recurring":
                raw = p.get("recurring")
                rec: dict[str, Any] = raw if isinstance(raw, dict) else {}
                if rec.get("interval") != "month" or rec.get("interval_count", 1) != 1:
                    bad.append("планът трябва да е всеки месец")
            amount, currency = p.get("unit_amount"), str(p.get("currency", "")).lower()
            if amount != o.amount_cents or currency != o.currency:
                # handle_event сверява сумата — с тази цена webhook-ът не зарежда.
                bad.append(f"сумата е {amount} {currency}, а в prices.json "
                           f"{o.amount_cents} {o.currency} — webhook-ът няма да зареди")
            if bad:
                problems.append(f"{where}: {'; '.join(bad)}")
            else:
                out(f"  {kind} {o.id}: {o.stripe_price} = {o.price} — наред")
    if priced == 0:
        problems.append("нито един пакет или план няма stripe_price в prices.json — "
                        "Checkout няма какво да продаде")
    return problems


# ── общото ───────────────────────────────────────────────────────────────────

def _open(data: Path, email: str) -> tuple[Store, dict[str, Any]]:
    db = data / "web.db"
    # Store() би създал празна база — грешна папка щеше да чака вечно.
    if not db.is_file():
        raise CheckError(f"{db} не съществува — --data е папката на `serve --data`")
    store = Store(db)
    user = store.user_by_email(email)
    if user is None:
        raise CheckError(f"няма такъв човек: {email.strip().lower()} "
                         "(python -m cloud.web.server add-user … --data …)")
    return store, user


def _offer(prices: billing.Prices, pack: str | None, plan: str | None, *,
           need_price: bool) -> tuple[billing.Offer, str]:
    if plan:
        found = prices.plans.get(plan)
        mode = "subscription"
    else:
        packs = [o for o in prices.packs.values() if o.stripe_price or not need_price]
        found = prices.packs.get(pack) if pack else (packs[0] if packs else None)
        mode = "payment"
    if found is None:
        raise CheckError(f"няма такъв {'план' if plan else 'пакет'} в prices.json: "
                         f"{plan or pack or '(нито един)'}")
    if need_price and not found.stripe_price:
        raise CheckError(f"{found.id} няма stripe_price в prices.json")
    return found, mode


def _find_credit(store: Store, uid: int, mode: str, session: str,
                 since: float) -> dict[str, Any] | None:
    for row in store.ledger_entries(uid, 50):
        if mode == "payment" and row["event_id"] == f"stripe:{session}":
            return row
        # Абонаментът се зарежда по фактурата (stripe:in_…), не по сесията.
        if mode == "subscription" and row["kind"] == "subscription" and row["created"] >= since:
            return row
    return None


def _no_proxy_open(req: Any, timeout: float = 20) -> Any:
    """Локалният сървър — никога през HTTP(S)_PROXY от средата."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout)


# ── команди ──────────────────────────────────────────────────────────────────

def run_prices(a: argparse.Namespace, environ: Mapping[str, str], opener: Opener) -> int:
    key = test_key(environ)
    prices = billing.load_prices(a.prices)
    print(f"цените от {a.prices} срещу тестовия режим на Stripe:")
    problems = check_prices(prices, key, opener)
    for p in problems:
        print(f"  ГРЕШКА {p}", file=sys.stderr)
    print("цените: наред" if not problems else f"цените: {len(problems)} проблем(а)")
    return 1 if problems else 0


def run_checkout(a: argparse.Namespace, environ: Mapping[str, str], opener: Opener,
                 sleep: Callable[[float], None], clock: Callable[[], float]) -> int:
    key = test_key(environ)
    prices = billing.load_prices(a.prices)
    offer, mode = _offer(prices, a.pack, a.plan, need_price=True)
    public = billing.check_public_url(
        a.public_url or environ.get("GENESIS_PUBLIC_URL", "") or "http://localhost:8080",
        allow_http=True)
    store, user = _open(a.data, a.email)
    uid = user["id"]
    if mode == "subscription" and store.account(uid)["plan"]:
        # Сървърът отказва същото с 409 — втори абонамент не е проверка.
        raise CheckError(f"{user['email']} вече има план — пробвай с пакет (--pack) "
                         "или с друг тестов човек")
    print(f"1/3 цените от {a.prices}:")
    problems = check_prices(prices, key, opener)
    if problems:
        for p in problems:
            print(f"  ГРЕШКА {p}", file=sys.stderr)
        return 1
    seen: dict[str, Any] = {}

    def capture(req: Any, timeout: float = 20) -> Any:
        # Същият create_checkout като в сървъра; само id-то на сесията се
        # запомня — по него се търси редът в сметката.
        with opener(req, timeout=timeout) as r:
            body = r.read()
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                seen.update(data)
        except (ValueError, RecursionError):
            pass
        return io.BytesIO(body)

    cfg = billing.StripeConfig(key, environ.get("STRIPE_WEBHOOK_SECRET", ""), public)
    since = time.time()
    before = store.balance(uid)
    print(f"2/3 Checkout за {offer.id} ({offer.price}) на {user['email']}:")
    try:
        url = billing.create_checkout(cfg, user_id=uid, email=user["email"], offer=offer,
                                      mode=mode, opener=capture)
    except billing.StripeError as e:
        print(f"  Stripe отказа сесията ({e}) — подробностите са горе в stderr", file=sys.stderr)
        return 1
    session = str(seen.get("id", ""))
    if seen.get("livemode") is not False:
        print("  сесията не е в тестовия режим — спирам", file=sys.stderr)
        return 1
    print(f"  {url}")
    print(f"  Отвори адреса и плати с карта {TEST_CARD}, всяка бъдеща дата (12/34),")
    print("  всеки CVC (123), всяко име и пощенски код.")
    print(f"3/3 чакам webhook-а до {a.timeout} s (сесия {session}); "
          f"баланс преди: {billing.fmt_credits(before)} кредита")
    deadline = clock() + a.timeout
    while True:
        row = _find_credit(store, uid, mode, session, since)
        if row is not None:
            after = store.balance(uid)
            print(f"  ред {row['event_id']} ({row['kind']}): +{billing.fmt_credits(row['amount'])}")
            print(f"  баланс: {billing.fmt_credits(before)} → {billing.fmt_credits(after)} кредита")
            if row["amount"] != offer.credits * MICRO:
                print(f"  ГРЕШКА: очаквах +{offer.credits} кредита", file=sys.stderr)
                return 1
            if mode == "subscription":
                print(f"  план: {store.account(uid)['plan'] or '(няма)'}")
            print("УСПЕХ: Stripe → webhook → сметка работи в тестовия режим")
            return 0
        if clock() >= deadline:
            break
        sleep(a.interval)
    print(f"  за {a.timeout} s ред в сметката не дойде. Провери:\n"
          f"  - върви ли `stripe listen --forward-to localhost:<порт>{WEBHOOK_PATH}`;\n"
          "  - сървърът пуснат ли е с whsec_ от СЪЩИЯ `stripe listen` (STRIPE_WEBHOOK_SECRET);\n"
          "  - --data е папката на същия сървър;\n"
          "  - в stderr на сървъра редове „stripe: …“ (сумата не съвпада, непознат пакет).",
          file=sys.stderr)
    if mode == "subscription":
        # stripe listen не повтаря сам; invoice.paid преди сесията е 503 (billing).
        print("  - планът: invoice.paid с [503] в `stripe listen` → "
              "`stripe events resend evt_…` за фактурата.", file=sys.stderr)
    return 1


def _post(url: str, payload: bytes, header: str, opener: Opener) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json", "Stripe-Signature": header})
    try:
        with opener(req, timeout=20) as r:
            code, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise CheckError(f"{url} не отговаря ({type(e).__name__}: {e}) — върви ли "
                         "`python -m cloud.web.server serve … --billing`?") from None
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        data = None
    return code, data if isinstance(data, dict) else {}


def simulated_event(uid: int, email: str, offer: billing.Offer, tag: str) -> dict[str, Any]:
    """checkout.session.completed на платен пакет — точно полетата, които
    чете billing.handle_event, във вида от тестовия режим на Stripe."""
    session = f"cs_test_genesis_check_{tag}"
    return {
        "id": f"evt_genesis_check_{tag}", "object": "event", "livemode": False,
        "type": "checkout.session.completed", "created": int(time.time()),
        "data": {"object": {
            "id": session, "object": "checkout.session", "livemode": False,
            "mode": "payment", "status": "complete", "payment_status": "paid",
            "client_reference_id": str(uid), "customer_email": email,
            "amount_total": offer.amount_cents, "currency": offer.currency,
            "metadata": {"user_id": str(uid), "pack": offer.id},
        }},
    }


def run_simulate(a: argparse.Namespace, environ: Mapping[str, str], opener: Opener) -> int:
    refuse_live(environ)
    secret = webhook_secret(environ)
    prices = billing.load_prices(a.prices)
    offer, _ = _offer(prices, a.pack, None, need_price=False)
    store, user = _open(a.data, a.email)
    uid = user["id"]
    url = a.url.rstrip("/") + WEBHOOK_PATH
    event = simulated_event(uid, user["email"], offer, uuid.uuid4().hex[:16])
    session = event["data"]["object"]["id"]
    payload = json.dumps(event).encode()
    want = offer.credits * MICRO
    before = store.balance(uid)
    print(f"симулирано плащане на {offer.id} ({offer.price}) за {user['email']} → {url}")
    print(f"  баланс преди: {billing.fmt_credits(before)} кредита")
    failed: list[str] = []

    def rows() -> list[dict[str, Any]]:
        return [r for r in store.ledger_entries(uid, 50) if r["event_id"] == f"stripe:{session}"]

    # 1. Чужд подпис — сървърът трябва да откаже, преди да чете тялото.
    code, _ = _post(url, payload, sign(payload, "whsec_not_the_server_secret"), opener)
    ok = code == 400 and not rows()
    print(f"  чужд подпис: {code} {'— отказан, нищо не е заредено' if ok else '— ОЧАКВАХ 400'}")
    if not ok:
        failed.append("чужд подпис не е отказан")
    # 2. Истинският подпис — точно един ред, точно кредитите на пакета.
    header = sign(payload, secret)
    delivered, data = _post(url, payload, header, opener)
    note = data.get("note") or data.get("error") or ""
    got = rows()
    ok = delivered == 200 and len(got) == 1 and got[0]["amount"] == want
    print(f"  доставка: {delivered} {note}")
    if delivered == 400:
        failed.append("сървърът не прие подписа — STRIPE_WEBHOOK_SECRET тук и в сървъра е различен")
    elif "друг режим" in str(note):
        failed.append("сървърът върви с live ключ — тестово събитие не зарежда")
    elif not ok:
        failed.append(f"очаквах един ред stripe:{session} с +{offer.credits} кредита, "
                      f"а редовете са {len(got)} (--data на същия сървър ли е?)")
    # 3. Повторът (Stripe праща пак при таймаут) — не зарежда втори път.
    # 2026-10-12: преди всеки отговор ≠ 200 се отчиташе „ЗАРЕДИ ВТОРИ ПЪТ“ — и
    # при грешна whsec (400 и на двете) до вярната диагноза излизаше фалшива
    # тревога. Двойно зареждане е само повече редове; отказ е отделна грешка,
    # а след неприета доставка повторът нищо не казва — пропуска се.
    if delivered != 200:
        print("  повторна доставка: пропусната — първата не е приета")
    else:
        code, _ = _post(url, payload, header, opener)
        again = rows()
        # Бележката на сървъра е „заредено“ и при повтор — броят се редовете.
        if len(again) > len(got):
            print(f"  повторна доставка: {code} — ЗАРЕДИ ВТОРИ ПЪТ")
            failed.append("повторът е зареден втори път")
        elif code != 200:
            print(f"  повторна доставка: {code} — отказана (не е заредено пак)")
            failed.append(f"повторът върна {code} — Stripe ще го праща отново, "
                          "виж лога на сървъра")
        else:
            print(f"  повторна доставка: {code} — редът вече е, не е заредено втори път")
    after = store.balance(uid)
    print(f"  баланс: {billing.fmt_credits(before)} → {billing.fmt_credits(after)} кредита "
          f"(+{billing.fmt_credits(after - before)}, очаквано +{billing.fmt_credits(want)})")
    for f in failed:
        print(f"  ГРЕШКА: {f}", file=sys.stderr)
    if failed:
        return 1
    print("УСПЕХ: подписаният webhook зарежда точно веднъж")
    return 0


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None,
         opener: Opener | None = None, sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic) -> int:
    p = argparse.ArgumentParser(prog="python -m cloud.stripe_check",
                                description="Stripe в тестов режим: цени, Checkout, webhook.")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("prices", help="stripe_price от prices.json срещу тестовия режим")
    co = sub.add_parser("checkout", help="цени + истинска тестова Checkout сесия + чакане на webhook-а")
    si = sub.add_parser("simulate", help="подписан webhook към локалния сървър, без Stripe")
    for sp in (pr, co, si):
        sp.add_argument("--prices", type=Path, default=billing.PRICES)
    for sp in (co, si):
        sp.add_argument("email", help="тестов човек в web.db")
        sp.add_argument("--data", type=Path, default=Path("/srv/genesis/web"),
                        help="папката с web.db (като `serve --data`)")
        sp.add_argument("--pack", help="пакет от prices.json (по подразбиране първият)")
    co.add_argument("--plan", help="план вместо пакет (абонамент)")
    co.add_argument("--public-url", default="",
                    help="адрес за връщане след плащането (иначе GENESIS_PUBLIC_URL "
                         "или http://localhost:8080)")
    co.add_argument("--timeout", type=int, default=300, help="секунди чакане на webhook-а")
    co.add_argument("--interval", type=float, default=2.0, help=argparse.SUPPRESS)
    si.add_argument("--url", default="http://127.0.0.1:8080", help="пуснатият уеб сървър")
    a = p.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        if a.cmd == "prices":
            return run_prices(a, env, opener or urllib.request.urlopen)
        if a.cmd == "checkout":
            if a.pack and a.plan:
                raise CheckError("или --pack, или --plan")
            return run_checkout(a, env, opener or urllib.request.urlopen, sleep, clock)
        return run_simulate(a, env, opener or _no_proxy_open)
    except (CheckError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
