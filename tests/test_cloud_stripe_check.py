"""cloud/stripe_check.py (2026-10-12) — проверката на Stripe в тестов режим.

Без мрежа и без ключове: Stripe е фалшив opener, simulate праща към сървър
в същия процес (127.0.0.1, временна база). Истинските тестови ключове ги има
само операторът — с тях стъпките са в cloud/README.md."""
from __future__ import annotations

import io
import json
import threading
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from cloud import stripe_check as sc
from cloud.runner import launch
from cloud.web import billing
from cloud.web import server as web
from cloud.web.store import MICRO, Store

SK = "sk_test_123"
WHSEC = "whsec_test_secret"
NOW = 1_760_000_000

_PRICES: dict[str, Any] = {
    "default": {"input": 300, "cached": 75, "output": 1200},
    "models": {"groq/*": {"input": 80, "cached": 40, "output": 100}},
    "per_turn": 2, "min_turn_tokens": 20000,
    "packs": {"p5": {"credits": 500, "amount_cents": 500, "currency": "eur",
                     "stripe_price": "price_p5"}},
    "plans": {"basic": {"credits": 1500, "amount_cents": 1500, "currency": "eur",
                        "stripe_price": "price_basic"}},
}


def _prices_file(tmp_path: Path) -> Path:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(_PRICES), encoding="utf-8")
    return path


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "web.db")
    store.add_user("ana@example.com", "correct-horse-battery")
    return store


class _NoRun:
    def __call__(self, text: str, workspace: Path, **kw: Any) -> launch.Result:
        return launch.Result(True, False, 0, 0.1)


@pytest.fixture
def server(tmp_path):
    """Истинският уеб сървър с плащане и Stripe, в този процес."""
    store = _store(tmp_path)
    started: list[Any] = []

    def start(key: str = SK) -> int:
        app = web.App(store, tmp_path / "jobs", runner=_NoRun(), secure_cookie=False,
                      gateway=launch.Gateway("s" * 40, tmp_path / "usage.jsonl", ("GROQ_API_KEY",)),
                      prices=billing.parse_prices(_PRICES),
                      stripe=billing.StripeConfig(key, WHSEC, "https://genesis.example"))
        srv = web.make_server(app, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append((app, srv))
        return int(srv.server_address[1])

    yield store, start
    for app, srv in started:
        srv.shutdown()
        srv.server_close()
        app.pool.shutdown(wait=True)


def _simulate(tmp_path: Path, port: int, secret: str = WHSEC) -> int:
    return sc.main(["simulate", "ana@example.com", "--data", str(tmp_path),
                    "--url", f"http://127.0.0.1:{port}", "--prices", str(_prices_file(tmp_path))],
                   environ={"STRIPE_WEBHOOK_SECRET": secret})


def _purchases(store: Store) -> list[dict[str, Any]]:
    return [r for r in store.ledger_entries(1) if r["kind"] == "purchase"]


# ── подпис ───────────────────────────────────────────────────────────────────

def test_the_signature_matches_the_servers_check() -> None:
    payload = json.dumps(sc.simulated_event(1, "a@x", billing.parse_prices(_PRICES).packs["p5"],
                                            "t1")).encode()
    v = billing.verify_stripe_signature
    assert v(payload, sc.sign(payload, WHSEC, NOW), WHSEC, now=NOW)
    assert v(payload, sc.sign(payload, WHSEC), WHSEC)                  # сега
    assert not v(payload, sc.sign(payload, "whsec_other", NOW), WHSEC, now=NOW)
    assert not v(payload + b" ", sc.sign(payload, WHSEC, NOW), WHSEC, now=NOW)
    assert not v(payload, sc.sign(payload, WHSEC, NOW - 301), WHSEC, now=NOW)


def test_the_simulated_event_is_what_handle_event_credits(tmp_path) -> None:
    store = _store(tmp_path)
    prices = billing.parse_prices(_PRICES)
    event = sc.simulated_event(1, "ana@example.com", prices.packs["p5"], "t1")
    assert billing.handle_event(store, prices, event, live=False) == (200, "заредено")
    assert store.balance(1) == 500 * MICRO


# ── live ключове ─────────────────────────────────────────────────────────────

def _boom(*a: Any, **k: Any) -> Any:
    raise AssertionError("с live ключ нищо не бива да тръгне към Stripe")


@pytest.mark.parametrize("key", ["sk_live_abc", "rk_live_abc", " sk_live_abc "])
@pytest.mark.parametrize("cmd", [["prices"], ["checkout", "ana@example.com"],
                                 ["simulate", "ana@example.com"]])
def test_live_keys_are_refused_outright(tmp_path, capsys, key, cmd) -> None:
    _store(tmp_path)
    env = {"STRIPE_SECRET_KEY": key, "STRIPE_WEBHOOK_SECRET": WHSEC}
    argv = [*cmd, "--prices", str(_prices_file(tmp_path))]
    if cmd[0] != "prices":
        argv += ["--data", str(tmp_path)]
    assert sc.main(argv, environ=env, opener=_boom, sleep=_boom) == 2
    assert "live" in capsys.readouterr().err


@pytest.mark.parametrize("key", ["", "pk_test_abc", "sk_abc"])
def test_a_network_check_needs_a_test_secret_key(tmp_path, capsys, key) -> None:
    assert sc.main(["prices", "--prices", str(_prices_file(tmp_path))],
                   environ={"STRIPE_SECRET_KEY": key}, opener=_boom) == 2
    assert "sk_test_" in capsys.readouterr().err


def test_a_wrong_data_dir_is_not_created(tmp_path, capsys) -> None:
    assert sc.main(["simulate", "ana@example.com", "--data", str(tmp_path / "nope")],
                   environ={"STRIPE_WEBHOOK_SECRET": WHSEC}, opener=_boom) == 2
    assert not (tmp_path / "nope" / "web.db").exists()


# ── simulate срещу истинския сървър ──────────────────────────────────────────

def test_simulate_credits_exactly_once(tmp_path, server, capsys) -> None:
    store, start = server
    assert _simulate(tmp_path, start()) == 0
    out = capsys.readouterr().out
    assert "УСПЕХ" in out and "0.00 → 500.00" in out
    assert store.balance(1) == 500 * MICRO
    assert len(_purchases(store)) == 1


def test_simulate_with_another_secret_credits_nothing(tmp_path, server, capsys) -> None:
    store, start = server
    assert _simulate(tmp_path, start(), secret="whsec_from_another_listen") == 1
    out = capsys.readouterr()
    assert "STRIPE_WEBHOOK_SECRET" in out.err
    # 2026-10-12: повторът също връщаше 400 и се отчиташе „ЗАРЕДИ ВТОРИ ПЪТ“ —
    # фалшива тревога до вярната диагноза, а не беше заредено нищо.
    assert "втори път" not in out.out + out.err
    assert store.balance(1) == 0


def test_simulate_a_rejected_replay_is_not_a_double_credit(tmp_path, server, capsys) -> None:
    """Доставката минава, повторът връща 503 — грешка е, но не двойно зареждане."""
    import urllib.request
    store, start = server
    port = start()
    posts: list[int] = []

    def opener(req: Any, timeout: float = 0) -> Any:
        posts.append(1)
        if len(posts) == 3:
            raise urllib.error.HTTPError(req.full_url, 503, "busy", None,  # type: ignore[arg-type]
                                         io.BytesIO(b"{}"))
        return urllib.request.urlopen(req, timeout=timeout)

    rc = sc.main(["simulate", "ana@example.com", "--data", str(tmp_path),
                  "--url", f"http://127.0.0.1:{port}", "--prices", str(_prices_file(tmp_path))],
                 environ={"STRIPE_WEBHOOK_SECRET": WHSEC}, opener=opener)
    out = capsys.readouterr()
    assert rc == 1
    assert "втори път" not in out.out + out.err
    assert "повторът върна 503" in out.err
    assert len(_purchases(store)) == 1


def test_simulate_tells_a_live_server_apart(tmp_path, server, capsys) -> None:
    store, start = server
    assert _simulate(tmp_path, start(key="sk_live_x")) == 1
    assert "live" in capsys.readouterr().err
    assert store.balance(1) == 0


def test_simulate_without_a_server_says_so(tmp_path, capsys) -> None:
    _store(tmp_path)
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert _simulate(tmp_path, port) == 2
    assert "не отговаря" in capsys.readouterr().err


# ── prices и checkout с фалшив Stripe ────────────────────────────────────────

class _Resp(io.BytesIO):
    status = 200


def _price(pid: str, amount: int, monthly: bool, **over: Any) -> dict[str, Any]:
    return {"id": pid, "object": "price", "active": True, "livemode": False,
            "currency": "eur", "unit_amount": amount,
            "type": "recurring" if monthly else "one_time",
            "recurring": {"interval": "month", "interval_count": 1} if monthly else None,
            **over}


class FakeStripe:
    def __init__(self, **prices: dict[str, Any]) -> None:
        self.prices = {"price_p5": _price("price_p5", 500, False),
                       "price_basic": _price("price_basic", 1500, True), **prices}
        self.seen: list[Any] = []

    def __call__(self, req: Any, timeout: float = 0) -> Any:
        self.seen.append(req)
        url = req.full_url
        if url.endswith("/checkout/sessions"):
            assert req.get_method() == "POST"
            return _Resp(json.dumps({"id": "cs_test_abc", "object": "checkout.session",
                                     "livemode": False,
                                     "url": "https://checkout.stripe.com/c/pay/cs_test_abc"}).encode())
        pid = url.rsplit("/", 1)[1]
        if pid not in self.prices:
            raise urllib.error.HTTPError(url, 404, "nf", {},  # type: ignore[arg-type]
                                         io.BytesIO(json.dumps({"error": {
                                             "message": f"No such price: '{pid}'"}}).encode()))
        assert req.headers["Authorization"] == f"Bearer {SK}"
        return _Resp(json.dumps(self.prices[pid]).encode())


def test_prices_match_the_test_mode(tmp_path, capsys) -> None:
    fake = FakeStripe()
    assert sc.main(["prices", "--prices", str(_prices_file(tmp_path))],
                   environ={"STRIPE_SECRET_KEY": SK}, opener=fake) == 0
    assert "цените: наред" in capsys.readouterr().out
    assert len(fake.seen) == 2


@pytest.mark.parametrize(("over", "why"), [
    ({"price_p5": _price("price_p5", 499, False)}, "webhook-ът няма да зареди"),
    ({"price_p5": _price("price_p5", 500, False, currency="usd")}, "webhook-ът няма да зареди"),
    ({"price_p5": _price("price_p5", 500, True)}, "one_time"),
    ({"price_p5": _price("price_p5", 500, False, active=False)}, "неактивна"),
    ({"price_basic": _price("price_basic", 1500, True,
                            recurring={"interval": "year", "interval_count": 1})}, "всеки месец"),
])
def test_a_price_the_webhook_would_refuse_is_reported(tmp_path, capsys, over, why) -> None:
    assert sc.main(["prices", "--prices", str(_prices_file(tmp_path))],
                   environ={"STRIPE_SECRET_KEY": SK}, opener=FakeStripe(**over)) == 1
    assert why in capsys.readouterr().err


def test_a_missing_price_is_a_404_with_a_hint(tmp_path, capsys) -> None:
    fake = FakeStripe()
    del fake.prices["price_basic"]
    assert sc.main(["prices", "--prices", str(_prices_file(tmp_path))],
                   environ={"STRIPE_SECRET_KEY": SK}, opener=fake) == 1
    assert "404" in (err := capsys.readouterr().err) and "тестовия режим" in err


def _checkout(tmp_path: Path, fake: FakeStripe, sleep: Any, clock: Any, *extra: str) -> int:
    return sc.main(["checkout", "ana@example.com", "--data", str(tmp_path),
                    "--prices", str(_prices_file(tmp_path)), "--timeout", "10", *extra],
                   environ={"STRIPE_SECRET_KEY": SK, "STRIPE_WEBHOOK_SECRET": WHSEC},
                   opener=fake, sleep=sleep, clock=clock)


def test_checkout_waits_for_the_webhook_and_shows_the_balance(tmp_path, capsys) -> None:
    from urllib.parse import parse_qsl
    store = _store(tmp_path)
    fake = FakeStripe()
    prices = billing.parse_prices(_PRICES)
    paid: list[float] = []

    def sleep(s: float) -> None:
        # Човекът плаща с 4242 → Stripe → webhook на сървъра → ред в web.db.
        paid.append(s)
        if len(paid) == 2:
            obj = sc.simulated_event(1, "ana@example.com", prices.packs["p5"], "x")
            obj["data"]["object"]["id"] = "cs_test_abc"
            assert billing.handle_event(store, prices, obj, live=False)[0] == 200

    assert _checkout(tmp_path, fake, sleep, lambda: 0.0) == 0
    out = capsys.readouterr().out
    assert "https://checkout.stripe.com/c/pay/cs_test_abc" in out and sc.TEST_CARD in out
    assert "0.00 → 500.00" in out and "УСПЕХ" in out
    # пътят на сървъра: същата форма като от /api/checkout
    form = dict(parse_qsl(fake.seen[-1].data.decode()))
    assert form["line_items[0][price]"] == "price_p5" and form["client_reference_id"] == "1"
    assert form["success_url"] == "http://localhost:8080/?view=account&paid=1"


def test_checkout_times_out_with_what_to_check(tmp_path, capsys) -> None:
    store = _store(tmp_path)
    ticks = iter(range(100))
    assert _checkout(tmp_path, FakeStripe(), lambda s: None, lambda: float(next(ticks))) == 1
    assert "stripe listen" in capsys.readouterr().err
    assert store.balance(1) == 0


def test_checkout_stops_before_paying_on_a_bad_price(tmp_path, capsys) -> None:
    _store(tmp_path)
    fake = FakeStripe(price_p5=_price("price_p5", 100, False))
    assert _checkout(tmp_path, fake, _boom, lambda: 0.0) == 1
    assert not any(r.full_url.endswith("/checkout/sessions") for r in fake.seen)
