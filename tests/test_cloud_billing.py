"""cloud/web — платената част (етап 3, 2026-10-11): сметка само с добавяне,
спрени профили, покани, цени, таксуване от шлюза, прием на ход, Stripe и
командите на оператора. Без мрежа и без ключове: Stripe е фалшив `opener`,
подписите са с тестова тайна, шлюзът е JSONL файл в tmp."""
from __future__ import annotations

import hashlib
import http.client
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from cloud.gateway import gateway as gw
from cloud.runner import launch
from cloud.web import billing
from cloud.web import server as web
from cloud.web import store as store_mod
from cloud.web.store import MICRO, Store

PASSWORD = "correct-horse-battery"


class Client:
    def __init__(self, port: int) -> None:
        self.port = port
        self.cookie = ""

    def req(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None,
            raw: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        if self.cookie:
            h["Cookie"] = self.cookie
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        if "set-cookie" in hdrs:
            self.cookie = hdrs["set-cookie"].split(";")[0]
        return r.status, hdrs, out

    def json(self, method: str, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
        code, _, out = self.req(method, path, body, **kw)
        return code, json.loads(out or b"{}")

    def login(self, email: str = "ana@example.com", password: str = PASSWORD) -> int:
        return self.json("POST", "/api/login", {"email": email, "password": password})[0]


def _serve(app: web.App):
    srv = web.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class _NoRun:
    def __call__(self, text: str, workspace: Path, **kw: Any) -> launch.Result:
        return launch.Result(True, False, 0, 0.1)


@pytest.fixture
def plain(tmp_path):
    """Сървър без плащане — за профилите и поканите."""
    store = Store(tmp_path / "plain" / "web.db")
    store.add_user("ana@example.com", PASSWORD)
    app = web.App(store, tmp_path / "plain" / "jobs", runner=_NoRun(), secure_cookie=False)
    srv = _serve(app)
    yield app, lambda: Client(srv.server_address[1])
    srv.shutdown()
    srv.server_close()
    app.pool.shutdown(wait=True)


# ── S1: сметка само с добавяне ───────────────────────────────────────────────

def test_the_same_event_counts_once(tmp_path) -> None:
    s = Store(tmp_path / "db")
    uid = s.add_user("a@b.c", PASSWORD)
    assert s.add_entry(uid, "stripe:cs_1", "purchase", 5 * MICRO) is True
    assert s.add_entry(uid, "stripe:cs_1", "purchase", 5 * MICRO) is False
    assert s.balance(uid) == 5 * MICRO
    assert s.has_entry("stripe:cs_1") and not s.has_entry("stripe:cs_2")


def test_the_ledger_cannot_be_edited_or_deleted(tmp_path) -> None:
    s = Store(tmp_path / "db")
    uid = s.add_user("a@b.c", PASSWORD)
    s.add_entry(uid, "admin:1", "grant", 7)
    c = sqlite3.connect(tmp_path / "db")
    try:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("UPDATE ledger SET amount = 999999999")
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("DELETE FROM ledger")
    finally:
        c.close()
    assert s.balance(uid) == 7


@pytest.mark.parametrize("amount", [1.5, True, "5"])
def test_amounts_are_whole_micro_credits(tmp_path, amount) -> None:
    s = Store(tmp_path / "db")
    with pytest.raises(ValueError):
        s.add_entry(1, "admin:x", "grant", amount)
    with pytest.raises(ValueError):
        s.add_entry(1, "admin:y", "gift", 1)
    assert not s.has_entry("admin:x")


def test_month_spent_counts_only_this_months_charges(tmp_path) -> None:
    s = Store(tmp_path / "db")
    uid = s.add_user("a@b.c", PASSWORD)
    s.add_entry(uid, "turn:1", "charge", -3 * MICRO, turn_id=1)
    s.add_entry(uid, "stripe:cs", "purchase", 50 * MICRO)
    assert s.month_spent(uid) == 3 * MICRO
    assert s.balance(uid) == 47 * MICRO
    # Следващия месец — миналите такси не се броят.
    nxt = datetime.now(timezone.utc).replace(day=28).timestamp() + 5 * 86400
    assert s.month_spent(uid, now=nxt) == 0


def test_an_old_database_keeps_its_rows_and_gains_the_new_tables(tmp_path) -> None:
    db = tmp_path / "web.db"
    old = sqlite3.connect(db)
    old.executescript(store_mod._SCHEMA.split("-- Сметката")[0])
    old.execute("INSERT INTO users (email, password, created) VALUES ('old@x.y', ?, 1)",
                (store_mod.hash_password(PASSWORD),))
    old.commit()
    old.close()
    s = Store(db)
    assert s.check_password("old@x.y", PASSWORD) == 1
    assert s.balance(1) == 0 and s.account(1)["disabled"] is False
    assert s.add_entry(1, "admin:a", "grant", 1)


# ── S2: профили ──────────────────────────────────────────────────────────────

def test_disabling_ends_the_live_session_and_blocks_login(plain) -> None:
    app, client = plain
    cl = client()
    assert cl.login() == 200
    assert cl.req("GET", "/api/me")[0] == 200
    app.store.set_disabled(1, True)
    assert cl.req("GET", "/api/me")[0] == 401
    assert cl.login() == 401
    app.store.set_disabled(1, False)
    assert cl.login() == 200


def test_a_session_of_a_disabled_user_is_refused_even_if_left_behind(tmp_path) -> None:
    s = Store(tmp_path / "db")
    uid = s.add_user("a@b.c", PASSWORD)
    token = s.new_session(uid)
    s.set_account(uid, disabled=True)      # без изтриване на сесиите
    assert s.session_user(token) is None


def test_a_disabled_login_costs_exactly_one_scrypt(tmp_path, monkeypatch) -> None:
    s = Store(tmp_path / "db")
    uid = s.add_user("a@b.c", PASSWORD)
    s.set_disabled(uid, True)
    store_mod._dummy_hash()
    calls: list[int] = []
    real = hashlib.scrypt

    def counting(*a: Any, **kw: Any) -> bytes:
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(store_mod.hashlib, "scrypt", counting)
    assert s.check_password("a@b.c", PASSWORD) is None
    assert len(calls) == 1
    assert s.check_password("nobody@b.c", PASSWORD) is None
    assert len(calls) == 2


def test_account_defaults_and_whitelist(tmp_path) -> None:
    s = Store(tmp_path / "db")
    assert s.account(5) == {"disabled": False, "turns_per_day": None, "max_tokens_per_turn": None,
                            "monthly_cap": None, "stripe_customer": None, "plan": ""}
    s.set_account(5, turns_per_day=3, plan="basic")
    s.set_account(5, stripe_customer="cus_1")
    assert s.account(5)["turns_per_day"] == 3 and s.account(5)["plan"] == "basic"
    assert s.user_by_customer("cus_1") == 5 and s.user_by_customer("") is None
    with pytest.raises(ValueError):
        s.set_account(5, email="x")


def test_turns_today_counts_failed_turns(tmp_path) -> None:
    s = Store(tmp_path / "db")
    uid = s.add_user("a@b.c", PASSWORD)
    chat = s.new_chat(uid, "t")
    t1 = s.add_turn(chat, "a")
    s.finish_turn(t1, ok=False, error="x", tokens={}, seconds=0)
    s.add_turn(chat, "b")
    assert s.turns_today(uid) == 2
    assert s.turns_today(uid, now=time.time() + 2 * 86400) == 0


def test_unbilled_turns_skip_charged_and_unfinished(tmp_path) -> None:
    s = Store(tmp_path / "db")
    uid = s.add_user("a@b.c", PASSWORD)
    chat = s.new_chat(uid, "t")
    done, charged, running, nojob = (s.add_turn(chat, x) for x in "abcd")
    for t in (done, charged, running):
        s.set_job(t, f"genesis-task-{t}")
    for t in (done, charged, nojob):
        s.finish_turn(t, ok=True, error="", tokens={}, seconds=1)
    s.add_entry(uid, f"turn:{charged}", "charge", -1, turn_id=charged)
    assert s.unbilled_turns() == [{"turn_id": done, "user_id": uid, "job": f"genesis-task-{done}"}]
    assert s.turn(uid, charged)["cost_micro"] == 1      # type: ignore[index]
    assert "cost_micro" not in s.turn(uid, done)        # type: ignore[operator]


# ── S11: покани ──────────────────────────────────────────────────────────────

def test_an_invite_works_once_and_is_stored_hashed(tmp_path) -> None:
    s = Store(tmp_path / "db")
    (code,) = s.new_invites(1, 3 * MICRO, "бета")
    uid = s.signup(code, "New@x.y", PASSWORD)
    assert s.balance(uid) == 3 * MICRO
    assert s.check_password("new@x.y", PASSWORD) == uid
    with pytest.raises(ValueError, match="поканата"):
        s.signup(code, "other@x.y", PASSWORD)
    assert code.encode() not in (tmp_path / "db").read_bytes()


def test_a_duplicate_email_does_not_burn_the_invite(tmp_path) -> None:
    s = Store(tmp_path / "db")
    s.add_user("ana@x.y", PASSWORD)
    (code,) = s.new_invites(1)
    with pytest.raises(ValueError, match="вече съществува"):
        s.signup(code, "ana@x.y", PASSWORD)
    with pytest.raises(ValueError):
        s.signup(code, "bob@x.y", "short")
    assert s.signup(code, "bob@x.y", PASSWORD) > 0


def test_ten_parallel_signups_with_one_code_make_one_user(tmp_path) -> None:
    s = Store(tmp_path / "db")
    (code,) = s.new_invites(1, MICRO)
    results: list[Any] = []

    def go(i: int) -> None:
        try:
            results.append(s.signup(code, f"u{i}@x.y", PASSWORD))
        except ValueError as e:
            results.append(e)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(isinstance(r, int) for r in results) == 1
    assert len(s.users()) == 1


def test_signup_over_http_sets_a_session(plain) -> None:
    app, client = plain
    (code,) = app.store.new_invites(1, 2 * MICRO)
    cl = client()
    code_, hdrs, _ = cl.req("POST", "/api/signup",
                            {"code": code, "email": "new@x.y", "password": PASSWORD})
    assert code_ == 201 and "HttpOnly" in hdrs["set-cookie"]
    assert cl.json("GET", "/api/me")[1]["email"] == "new@x.y"
    assert client().json("POST", "/api/signup", {"code": code, "email": "b@x.y",
                                                 "password": PASSWORD})[0] == 400


def test_guessing_invite_codes_is_rate_limited(plain) -> None:
    app, client = plain
    (code,) = app.store.new_invites(1)
    cl = client()
    codes = [cl.json("POST", "/api/signup", {"code": f"guess{i}", "email": "g@x.y",
                                             "password": PASSWORD})[0] for i in range(6)]
    assert codes[:5] == [400] * 5 and codes[5] == 429
    # и истинският код от същия IP вече чака
    assert cl.json("POST", "/api/signup", {"code": code, "email": "g@x.y",
                                           "password": PASSWORD})[0] == 429


def test_signup_needs_same_origin(plain) -> None:
    app, client = plain
    (code,) = app.store.new_invites(1)
    code_, _ = client().json("POST", "/api/signup", {"code": code, "email": "g@x.y",
                                                     "password": PASSWORD},
                             headers={"Origin": "https://evil.example"})
    assert code_ == 403


# ── S3: цени ─────────────────────────────────────────────────────────────────

_PRICES: dict[str, Any] = {
    "default": {"input": 300, "cached": 75, "output": 1200},
    "models": {"groq/*": {"input": 80, "cached": 40, "output": 100},
               "groq/exact": {"input": 10, "cached": 5, "output": 20}},
    "per_turn": 2, "min_turn_tokens": 20000,
    "packs": {"p5": {"credits": 500, "amount_cents": 500, "currency": "eur",
                     "stripe_price": "price_p5"}},
    "plans": {"basic": {"credits": 1500, "amount_cents": 1500, "currency": "EUR",
                        "stripe_price": "price_basic"}},
}


def _prices(**over: Any) -> billing.Prices:
    return billing.parse_prices({**json.loads(json.dumps(_PRICES)), **over})


def test_the_shipped_prices_load_without_tomllib() -> None:
    p = billing.load_prices()
    assert p.max_rate == 1200 and p.per_turn == 2
    # Измерената задача (5 536 токена, 2026-09-25) по default: ≈ 3.7 кредита ≈ 0.04 EUR.
    cost = billing.turn_cost(p, [{"provider": "x", "model": "y", "prompt_tokens": 5536,
                                  "completion_tokens": 0, "total_tokens": 5536}])
    assert billing.fmt_credits(cost) == "3.66"


@pytest.mark.parametrize("change", [
    {"models": {"groq/*": {"input": 80, "cached": 40, "output": 1201}}},   # над default
    {"default": {"input": 300.0, "cached": 75, "output": 1200}},           # float
    {"default": {"input": True, "cached": 75, "output": 1200}},            # bool
    {"default": {"input": -1, "cached": 75, "output": 1200}},
    {"models": {"acme/*": {"input": 1, "cached": 1, "output": 1}}},        # непознат доставчик
    {"models": {"groq": {"input": 1, "cached": 1, "output": 1}}},
    {"packs": {"p5": {"credits": 0, "amount_cents": 500, "currency": "eur"}}},
    {"plans": {"b": {"credits": 5, "amount_cents": 500, "currency": "euro"}}},
    {"per_turn": 1.5},
])
def test_unsafe_price_files_are_refused(change) -> None:
    with pytest.raises(ValueError):
        _prices(**change)
    with pytest.raises(ValueError):
        billing.parse_prices({"models": {}})


def test_lookup_order_and_unknown_names_fall_to_default() -> None:
    p = _prices()
    assert p.rate("groq", "exact").output == 20
    assert p.rate("groq", "llama-3.3-70b").output == 100
    assert p.rate("GROQ", "x") == p.default
    assert p.rate("nvidia", "whatever") == p.default


def test_an_invalid_prices_file_stops_serve(tmp_path, capsys) -> None:
    bad = tmp_path / "prices.json"
    bad.write_text('{"default": {"input": 1, "cached": 1, "output": 1}, '
                   '"models": {"groq/*": {"input": 2, "cached": 1, "output": 1}}}', encoding="utf-8")
    code = web.main(["serve", "--data", str(tmp_path), "--billing", "--prices", str(bad),
                     "--gateway-usage", str(tmp_path / "u.jsonl"), "--env-file", "x"])
    assert code == 2 and "default" in capsys.readouterr().err


# ── S4: цена на ход ──────────────────────────────────────────────────────────

def test_turn_cost_by_hand() -> None:
    p = _prices()
    line = {"provider": "groq", "model": "x", "prompt_tokens": 1000, "completion_tokens": 500,
            "total_tokens": 1500, "cached_tokens": 200}
    assert billing.turn_cost(p, [line]) == 2_000_000 + 800 * 80 + 200 * 40 + 500 * 100
    # оценка без usage — целият сбор по най-скъпата тарифа на модела
    est = {"provider": "groq", "model": "x", "total_tokens": 4000, "estimated": True}
    assert billing.turn_cost(p, [est]) == 2_000_000 + 4000 * 100
    # total над prompt + completion — разликата е изход (разсъждения)
    hidden = {**line, "total_tokens": 2000, "cached_tokens": 0}
    assert billing.turn_cost(p, [hidden]) == 2_000_000 + 1000 * 80 + 1000 * 100
    # боклук не прави сметката отрицателна или дробна
    junk = {"provider": "groq", "model": "x", "prompt_tokens": -5, "completion_tokens": 1.5,
            "total_tokens": "9", "cached_tokens": 10**9}
    assert billing.turn_cost(p, [junk]) == 2_000_000
    assert billing.turn_cost(p, []) == 2_000_000


def test_usage_lines_group_by_model_for_one_job(tmp_path) -> None:
    path = tmp_path / "u.jsonl"
    led = gw.Ledger(path)
    led.add("j1", "groq", "a", {"prompt_tokens": 10, "completion_tokens": 1})
    led.add("j1", "groq", "a", {"prompt_tokens": 20, "completion_tokens": 2})
    led.add("j1", "nvidia", "b", {"total_tokens": 7})
    led.add("j2", "groq", "a", {"prompt_tokens": 999})
    with path.open("a", encoding="utf-8") as f:
        f.write("{broken\n[1]\n")
    lines = gw.usage_lines(path, "j1")
    assert len(lines) == 2
    a = next(x for x in lines if x["model"] == "a")
    assert (a["prompt_tokens"], a["completion_tokens"], a["total_tokens"], a["calls"]) == (30, 3, 33, 2)
    assert gw.usage_lines(tmp_path / "missing.jsonl", "j1") == []


class _Resp:
    status = 200

    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *a: object) -> None:
        pass


def test_the_estimated_flag_survives_in_the_gateway_file(tmp_path) -> None:
    g = gw.Gateway("s" * 40, {"GROQ_API_KEY": "k"}, gw.Ledger(tmp_path / "u.jsonl"),
                   opener=lambda req, timeout=0: _Resp(b'{"choices": []}'))
    status, _ = g.forward("groq", b'{"model": "m", "max_tokens": 100}', gw.Grant("j", 10**12, 10**6))
    assert status == 200
    (line,) = gw.usage_lines(tmp_path / "u.jsonl", "j")
    assert line["estimated"] is True and line["total_tokens"] > 0


def test_run_task_uses_the_given_name_and_returns_usage_per_model(monkeypatch, tmp_path) -> None:
    usage = tmp_path / "u.jsonl"
    g = launch.Gateway("s" * 40, usage, ("GROQ_API_KEY",))

    class Proc:
        def __init__(self, argv: list[str], **kw: Any) -> None:
            name = argv[argv.index("--name") + 1]
            gw.Ledger(usage).add(name, "groq", "x", {"prompt_tokens": 9, "completion_tokens": 1})
            import io
            self.stdout = io.BytesIO(b'{"kind": "done", "ok": true, "tokens": {}}\n')

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(launch.subprocess, "Popen", Proc)
    res = launch.run_task("x", tmp_path / "ws", gateway=g, name="genesis-task-fixed")
    assert res.job == "genesis-task-fixed"
    assert [(u["model"], u["total_tokens"]) for u in res.usage] == [("x", 10)]


# ── S5: таксуване ────────────────────────────────────────────────────────────

class BillRunner:
    """Като контейнер през шлюза: шлюзът записва разхода под името на задачата,
    а „done“ лъже, че токените са 0."""

    def __init__(self, usage: Path) -> None:
        self.usage = usage
        self.gate = threading.Event()
        self.gate.set()
        self.kw: list[dict[str, Any]] = []

    def __call__(self, text: str, workspace: Path, **kw: Any) -> launch.Result:
        self.kw.append(kw)
        gw.Ledger(self.usage).add(kw["name"], "groq", "x",
                                  {"prompt_tokens": 1000, "completion_tokens": 500})
        self.gate.wait(5)
        if text == "boom":
            raise RuntimeError("docker липсва")
        return launch.Result(text != "fail", False, 0, 0.5, tokens={"total_tokens": 0},
                             job=kw["name"])


GROQ_TURN = 2_000_000 + 1000 * 80 + 500 * 100


def _billing_app(tmp_path: Path, runner: Any = None, **kw: Any) -> tuple[web.App, Any]:
    usage = tmp_path / "usage.jsonl"
    runner = runner or BillRunner(usage)
    store = Store(tmp_path / "web.db")
    if store.user_by_email("ana@example.com") is None:
        store.add_user("ana@example.com", PASSWORD)
        store.add_user("bob@example.com", PASSWORD)
    gateway = launch.Gateway("s" * 40, usage, ("GROQ_API_KEY",))
    app = web.App(store, tmp_path / "jobs", runner=runner, secure_cookie=False,
                  gateway=gateway, prices=kw.pop("prices", None) or _prices(), **kw)
    return app, runner


@pytest.fixture
def paid(tmp_path):
    app, runner = _billing_app(tmp_path)
    app.store.add_entry(1, "admin:start", "grant", 100 * MICRO)
    srv = _serve(app)
    yield app, runner, lambda: Client(srv.server_address[1])
    runner.gate.set()
    srv.shutdown()
    srv.server_close()
    app.pool.shutdown(wait=True)


def _turn(cl: Client, text: str = "направи CSV", chat: str | None = None) -> tuple[int, Any]:
    if chat is None:
        chat = cl.json("POST", "/api/chats", {"title": text})[1]["id"]
    return cl.json("POST", f"/api/chats/{chat}/turns", {"text": text})


def _wait(cl: Client, turn_id: int) -> dict[str, Any]:
    for _ in range(300):
        _code, data = cl.json("GET", f"/api/turns/{turn_id}/events")
        if data.get("status") in ("ok", "failed") and "cost" in data:
            return data
        time.sleep(0.02)
    raise AssertionError("ходът не завърши или не е таксуван")


def test_a_turn_is_charged_from_the_gateway_not_the_container(paid) -> None:
    app, _, client = paid
    cl = client()
    cl.login()
    code, data = _turn(cl)
    assert code == 202
    done = _wait(cl, data["turn_id"])
    assert done["tokens"] == {"total_tokens": 0}           # контейнерът казва 0
    assert app.store.balance(1) == 100 * MICRO - GROQ_TURN
    assert done["cost"] == billing.fmt_credits(GROQ_TURN) == "2.13"
    assert "cost_micro" not in done
    # reconcile след това не таксува нищо втори път
    assert billing.reconcile(app.store, app.prices, app.gateway.usage) == 0  # type: ignore[arg-type, union-attr]
    assert app.store.balance(1) == 100 * MICRO - GROQ_TURN


def test_failed_turns_are_charged_too(paid) -> None:
    app, _, client = paid
    cl = client()
    cl.login()
    for text in ("fail", "boom"):
        _, data = _turn(cl, text)
        assert _wait(cl, data["turn_id"])["status"] == "failed"
    assert app.store.balance(1) == 100 * MICRO - 2 * GROQ_TURN


def test_a_crash_before_the_charge_is_billed_once_on_restart(tmp_path, monkeypatch) -> None:
    app, _ = _billing_app(tmp_path)
    real = billing.charge_turn

    def broken(*a: Any, **k: Any) -> bool:
        raise OSError("диск")

    monkeypatch.setattr(billing, "charge_turn", broken)
    app.store.add_entry(1, "admin:start", "grant", 100 * MICRO)
    chat = app.store.new_chat(1, "t")
    turn = app.start_turn({"id": 1, "email": "ana@example.com"}, chat, "x")
    app.pool.shutdown(wait=True)
    assert app.store.turn(1, turn)["status"] == "ok"        # type: ignore[index]
    assert app.store.balance(1) == 100 * MICRO
    monkeypatch.setattr(billing, "charge_turn", real)
    _billing_app(tmp_path)                                   # „рестарт“
    _billing_app(tmp_path)                                   # и още един
    assert app.store.balance(1) == 100 * MICRO - GROQ_TURN


def test_a_turn_killed_by_a_restart_is_billed_from_the_gateway(tmp_path) -> None:
    app, runner = _billing_app(tmp_path)
    runner.gate.clear()
    app.store.add_entry(1, "admin:start", "grant", 100 * MICRO)
    chat = app.store.new_chat(1, "t")
    turn = app.start_turn({"id": 1, "email": "ana@example.com"}, chat, "x")
    for _ in range(200):
        if runner.kw:
            break
        time.sleep(0.01)
    # сървърът „умира“ посред хода; новият го маркира failed и го таксува
    app2, _ = _billing_app(tmp_path)
    assert app2.store.turn(1, turn)["status"] == "failed"   # type: ignore[index]
    assert app2.store.balance(1) == 100 * MICRO - GROQ_TURN
    runner.gate.set()
    app.pool.shutdown(wait=True)
    assert app.store.balance(1) == 100 * MICRO - GROQ_TURN  # и после — пак веднъж


def test_billing_without_the_gateway_is_refused(tmp_path, capsys) -> None:
    assert web.main(["serve", "--data", str(tmp_path), "--billing"]) == 2
    assert "--gateway-usage" in capsys.readouterr().err
    with pytest.raises(ValueError):
        web.App(Store(tmp_path / "db"), tmp_path / "jobs", runner=_NoRun(), prices=_prices())


def test_turns_before_billing_was_on_are_never_charged(tmp_path) -> None:
    usage = tmp_path / "usage.jsonl"
    store = Store(tmp_path / "web.db")
    store.add_user("ana@example.com", PASSWORD)
    free = web.App(store, tmp_path / "jobs", runner=BillRunner(usage),
                   gateway=launch.Gateway("s" * 40, usage, ("GROQ_API_KEY",)))
    free.start_turn({"id": 1}, store.new_chat(1, "t"), "x")
    free.pool.shutdown(wait=True)
    _billing_app(tmp_path)
    assert store.balance(1) == 0 and store.unbilled_turns() == []


# ── S6: прием на ход ─────────────────────────────────────────────────────────

# per_turn 2 кредита + 20 000 токена по 1 200 µc = 26 кредита
THRESHOLD = 2 * MICRO + 20_000 * 1200
ANA = {"id": 1, "email": "ana@example.com"}


def _turn_rows(app: web.App) -> int:
    return sum(len(app.store.turns(c["id"])) for c in app.store.chats(1))


def test_just_under_the_threshold_is_a_402_without_a_turn(tmp_path) -> None:
    app, runner = _billing_app(tmp_path)
    app.store.add_entry(1, "admin:a", "grant", THRESHOLD - 1)
    with pytest.raises(web.TurnRefused) as e:
        app.start_turn(ANA, app.store.new_chat(1, "t"), "x")
    assert e.value.status == 402
    assert e.value.message == ("балансът ти е 26.00 кредита — за нова задача трябват поне "
                               "26.00. Зареди от „Сметка“.")
    assert _turn_rows(app) == 0 and runner.kw == []


def test_at_the_threshold_the_gateway_budget_is_what_the_balance_pays(tmp_path) -> None:
    app, runner = _billing_app(tmp_path)
    app.store.add_entry(1, "admin:a", "grant", THRESHOLD)
    app.start_turn(ANA, app.store.new_chat(1, "t"), "x")
    app.pool.shutdown(wait=True)
    assert runner.kw[0]["gateway"].budget == 20_000
    assert app.gateway.budget == launch.TASK_BUDGET          # type: ignore[union-attr]


def test_max_tokens_per_turn_caps_the_budget(tmp_path) -> None:
    app, runner = _billing_app(tmp_path, max_tokens_per_turn=5000)
    app.store.add_entry(1, "admin:a", "grant", 1000 * MICRO)
    app.start_turn(ANA, app.store.new_chat(1, "t"), "x")
    app.store.set_account(1, max_tokens_per_turn=7000)
    app.pool.shutdown(wait=True)
    assert runner.kw[0]["gateway"].budget == 5000


def test_the_monthly_cap_refuses_even_with_a_big_balance(tmp_path) -> None:
    app, _ = _billing_app(tmp_path, monthly_cap=10 * MICRO)
    app.store.add_entry(1, "admin:a", "grant", 10_000 * MICRO)
    app.store.add_entry(1, "turn:999", "charge", -10 * MICRO, turn_id=999)
    with pytest.raises(web.TurnRefused) as e:
        app.start_turn(ANA, app.store.new_chat(1, "t"), "x")
    assert e.value.status == 402 and "месечния си таван от 10.00" in e.value.message
    # профилът може да има свой таван
    app.store.set_account(1, monthly_cap=1000 * MICRO)
    assert app.start_turn(ANA, app.store.new_chat(1, "t"), "x") > 0
    app.pool.shutdown(wait=True)


def test_turns_per_day_hold_without_billing(tmp_path) -> None:
    store = Store(tmp_path / "web.db")
    store.add_user("ana@example.com", PASSWORD)
    app = web.App(store, tmp_path / "jobs", runner=_NoRun(), turns_per_day=3)
    chat = store.new_chat(1, "t")
    for _ in range(3):
        app.start_turn(ANA, chat, "x")
        for _ in range(100):
            if not store.busy(user_id=1):
                break
            time.sleep(0.01)
    with pytest.raises(web.TurnRefused) as e:
        app.start_turn(ANA, chat, "x")
    assert e.value.status == 429 and "лимит от 3 задачи" in e.value.message
    store.set_account(1, turns_per_day=4)
    assert app.start_turn(ANA, chat, "x") > 0
    app.pool.shutdown(wait=True)


def test_the_51st_turn_of_the_day_is_a_429(paid) -> None:
    app, _, client = paid
    chat = app.store.new_chat(1, "t")
    for _ in range(web.TURNS_PER_DAY):
        t = app.store.add_turn(chat, "x")
        app.store.finish_turn(t, ok=False, error="", tokens={}, seconds=0)
    cl = client()
    cl.login()
    code, data = _turn(cl, chat=chat)
    assert code == 429 and "50" in data["error"]


def test_twenty_parallel_starts_with_money_for_one_start_one(paid) -> None:
    app, runner, client = paid
    app.store.add_entry(1, "admin:down", "adjust", THRESHOLD - 100 * MICRO)
    runner.gate.clear()
    cl = client()
    cl.login()
    chats = [cl.json("POST", "/api/chats", {})[1]["id"] for _ in range(20)]
    results: list[tuple[int, Any]] = []

    def go(chat: str) -> None:
        results.append(_turn(cl, chat=chat))

    threads = [threading.Thread(target=go, args=(c,)) for c in chats]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(code for code, _ in results) == [202] + [409] * 19
    runner.gate.set()
    (started,) = [data["turn_id"] for code, data in results if code == 202]
    _wait(cl, started)
    # след таксата парите не стигат за втори ход
    assert _turn(cl, chat=chats[0])[0] == 402


def test_a_disabled_account_cannot_start_a_turn(tmp_path) -> None:
    app, _ = _billing_app(tmp_path)
    app.store.set_disabled(1, True)
    with pytest.raises(web.TurnRefused) as e:
        app.start_turn(ANA, app.store.new_chat(1, "t"), "x")
    assert e.value.status == 403


def test_a_402_reaches_the_person_in_bulgarian(paid) -> None:
    app, _, client = paid
    app.store.add_entry(1, "admin:debt", "adjust", -100 * MICRO)
    cl = client()
    cl.login()
    code, data = _turn(cl)
    assert code == 402 and "Зареди от „Сметка“" in data["error"]


# ── S7: подпис на Stripe ─────────────────────────────────────────────────────

WHSEC = "whsec_test"
NOW = 1_760_000_000


def _sign(payload: bytes, secret: str = WHSEC, t: int = NOW) -> str:
    import hmac as _hmac
    sig = _hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def test_a_correct_signature_passes_and_any_change_fails() -> None:
    payload = b'{"id": "evt_1", "type": "x"}'
    v = billing.verify_stripe_signature
    assert v(payload, _sign(payload), WHSEC, now=NOW)
    assert not v(payload[:-2] + b"X}", _sign(payload), WHSEC, now=NOW)
    assert not v(payload, _sign(payload), "whsec_other", now=NOW)
    # същото съдържание, друго подреждане/интервали — подписът е върху байтовете
    redumped = json.dumps(json.loads(payload), separators=(",", ":")).encode()
    assert not v(redumped, _sign(payload), WHSEC, now=NOW)


def test_the_signature_window_is_five_minutes_both_ways() -> None:
    payload = b"{}"
    v = billing.verify_stripe_signature
    assert not v(payload, _sign(payload, t=NOW - 301), WHSEC, now=NOW)
    assert not v(payload, _sign(payload, t=NOW + 301), WHSEC, now=NOW)
    assert v(payload, _sign(payload, t=NOW - 299), WHSEC, now=NOW)


def test_signature_header_forms() -> None:
    payload = b"{}"
    good = _sign(payload).split(",")[1]
    v = billing.verify_stripe_signature
    assert v(payload, f"t={NOW}, v1={'0' * 64}, {good}", WHSEC, now=NOW)        # смяна на тайната
    assert not v(payload, f"t={NOW},v0={good[3:]}", WHSEC, now=NOW)             # само v0
    assert not v(payload, f"t=12a,{good}", WHSEC, now=NOW)
    assert not v(payload, f"t=١٢,{good}", WHSEC, now=NOW)                       # не-ASCII цифри
    assert not v(payload, f"t={NOW},v1=ж{good[3:]}", WHSEC, now=NOW)            # не-ASCII подпис
    assert not v(payload, "", WHSEC, now=NOW)
    assert not v(payload, _sign(payload, secret=""), "", now=NOW)


# ── S8: Stripe webhook ───────────────────────────────────────────────────────

SK = "sk_test_123"


@pytest.fixture
def stripe(tmp_path):
    app, _ = _billing_app(tmp_path, stripe=billing.StripeConfig(SK, WHSEC, "https://genesis.example"))
    srv = _serve(app)
    yield app, lambda: Client(srv.server_address[1])
    srv.shutdown()
    srv.server_close()
    app.pool.shutdown(wait=True)


def _event(etype: str, obj: dict[str, Any], *, live: bool = False, eid: str = "evt_1") -> bytes:
    return json.dumps({"id": eid, "type": etype, "livemode": live,
                       "data": {"object": obj}}).encode()


def _deliver(cl: Client, payload: bytes, sig: str | None = None) -> int:
    headers = {"Stripe-Signature": sig if sig is not None else _sign(payload, t=int(time.time())),
               "Content-Type": "application/json"}
    return cl.req("POST", "/api/stripe/webhook", raw=payload, headers=headers)[0]


def _session(**over: Any) -> dict[str, Any]:
    return {"id": "cs_test_1", "object": "checkout.session", "mode": "payment",
            "payment_status": "paid", "client_reference_id": "1", "amount_total": 500,
            "currency": "eur", "metadata": {"user_id": "1", "pack": "p5"}, **over}


def test_a_paid_session_credits_the_pack_once(stripe) -> None:
    app, client = stripe
    cl = client()
    done = _event("checkout.session.completed", _session())
    assert _deliver(cl, done) == 200
    assert _deliver(cl, done) == 200                       # повторена доставка
    late = _event("checkout.session.async_payment_succeeded", _session(), eid="evt_2")
    assert _deliver(cl, late) == 200
    assert app.store.balance(1) == 500 * MICRO
    assert app.store.balance(2) == 0


@pytest.mark.parametrize("over", [
    {"payment_status": "unpaid"},
    {"amount_total": 499},
    {"currency": "usd"},
    {"client_reference_id": "99"},
    {"client_reference_id": "1 OR 1"},
    {"metadata": {"pack": "nope"}},
])
def test_unpaid_or_mismatched_sessions_credit_nothing(stripe, over) -> None:
    app, client = stripe
    assert _deliver(client(), _event("checkout.session.completed", _session(**over))) == 200
    assert app.store.balance(1) == 0


def test_a_livemode_mismatch_credits_nothing(stripe) -> None:
    app, client = stripe
    assert _deliver(client(), _event("checkout.session.completed", _session(), live=True)) == 200
    assert app.store.balance(1) == 0


def test_a_bad_signature_is_a_400_and_touches_nothing(stripe) -> None:
    app, client = stripe
    payload = _event("checkout.session.completed", _session())
    assert _deliver(client(), payload, sig=_sign(payload, secret="whsec_evil")) == 400
    assert _deliver(client(), payload, sig="") == 400
    assert app.store.ledger_entries(1) == []


def test_a_huge_webhook_body_is_a_413(stripe) -> None:
    _, client = stripe
    try:
        code = _deliver(client(), b"x" * (300 * 1024), sig="t=1,v1=0")
    except ConnectionError:
        return   # Windows: отговорът 413 и затварянето изпреварват тялото (виж test_cloud_web)
    assert code == 413


def test_invoice_before_the_subscription_checkout_is_retried(stripe) -> None:
    app, client = stripe
    cl = client()
    invoice = _event("invoice.paid", {"id": "in_1", "customer": "cus_1", "status": "paid",
                                      "amount_paid": 1500, "currency": "eur"}, eid="evt_inv")
    assert _deliver(cl, invoice) == 503
    sub = _session(id="cs_sub", mode="subscription", customer="cus_1",
                   metadata={"user_id": "1", "plan": "basic"}, amount_total=1500)
    assert _deliver(cl, _event("checkout.session.completed", sub, eid="evt_sub")) == 200
    assert app.store.balance(1) == 0                       # кредитите идват с фактурата
    assert _deliver(cl, invoice) == 200
    assert _deliver(cl, invoice) == 200
    assert app.store.balance(1) == 1500 * MICRO
    assert app.store.account(1)["plan"] == "basic"
    # фактура под цената на плана не зарежда
    low = _event("invoice.paid", {"id": "in_2", "customer": "cus_1", "status": "paid",
                                  "amount_paid": 0, "currency": "eur"}, eid="evt_low")
    assert _deliver(cl, low) == 200 and app.store.balance(1) == 1500 * MICRO
    gone = _event("customer.subscription.deleted", {"id": "sub_1", "customer": "cus_1"})
    assert _deliver(cl, gone) == 200 and app.store.account(1)["plan"] == ""


def test_the_webhook_does_not_exist_without_stripe(paid) -> None:
    _, _, client = paid
    payload = _event("checkout.session.completed", _session())
    assert _deliver(client(), payload) == 404


# ── S9: Stripe Checkout ──────────────────────────────────────────────────────

def test_stripe_config_comes_only_from_the_environment() -> None:
    assert billing.StripeConfig.from_env({}) is None
    assert billing.StripeConfig.from_env({"STRIPE_SECRET_KEY": " ", "STRIPE_WEBHOOK_SECRET": ""}) is None
    for env in ({"STRIPE_SECRET_KEY": SK}, {"STRIPE_WEBHOOK_SECRET": WHSEC},
                {"STRIPE_SECRET_KEY": "pk_test_1", "STRIPE_WEBHOOK_SECRET": WHSEC},
                {"STRIPE_SECRET_KEY": SK, "STRIPE_WEBHOOK_SECRET": "secret"}):
        with pytest.raises(ValueError):
            billing.StripeConfig.from_env(env)
    cfg = billing.StripeConfig.from_env({"STRIPE_SECRET_KEY": SK, "STRIPE_WEBHOOK_SECRET": WHSEC})
    assert cfg is not None and not cfg.live
    assert billing.StripeConfig("sk_live_x", WHSEC).live


def _serve_args(tmp_path: Path) -> list[str]:
    keys = tmp_path / "keys.env"
    keys.write_text("GATEWAY_SECRET=" + "s" * 40 + "\nGROQ_API_KEY=x\n", encoding="utf-8")
    return ["serve", "--data", str(tmp_path), "--jobs", str(tmp_path / "jobs"), "--billing",
            "--gateway-usage", str(tmp_path / "u.jsonl"), "--env-file", str(keys)]


def test_serve_refuses_half_a_stripe_setup(tmp_path, monkeypatch, capsys) -> None:
    started: list[bool] = []
    monkeypatch.setattr(web, "make_server", lambda *a, **k: started.append(True))
    monkeypatch.setenv("STRIPE_SECRET_KEY", SK)
    assert web.main(_serve_args(tmp_path)) == 2
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WHSEC)
    assert web.main(_serve_args(tmp_path)) == 2                     # без --public-url
    assert web.main([*_serve_args(tmp_path), "--public-url", "http://x.example"]) == 2
    assert web.main(["serve", "--data", str(tmp_path), "--public-url", "https://x.example"]) == 2
    assert started == []
    assert "STRIPE_WEBHOOK_SECRET" in capsys.readouterr().err


class FakeStripe:
    def __init__(self, url: str = "https://checkout.stripe.com/c/pay/cs_test_1",
                 status: int = 200) -> None:
        self.url, self.status = url, status
        self.seen: list[tuple[Any, float]] = []

    def __call__(self, req: Any, timeout: float = 0) -> Any:
        self.seen.append((req, timeout))
        if self.status != 200:
            import io
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, self.status, "err", {},  # type: ignore[arg-type]
                                         io.BytesIO(b'{"error": {"message": "sk_test_123 bad"}}'))
        return _Resp(json.dumps({"id": "cs_test_1", "url": self.url}).encode())


def _checkout_app(tmp_path: Path, fake: FakeStripe) -> tuple[web.App, Any]:
    app, _ = _billing_app(tmp_path, stripe=billing.StripeConfig(SK, WHSEC, "https://genesis.example"),
                          stripe_opener=fake)
    srv = _serve(app)
    return app, srv


def test_checkout_sends_exactly_what_stripe_needs(tmp_path) -> None:
    from urllib.parse import parse_qsl
    fake = FakeStripe()
    app, srv = _checkout_app(tmp_path, fake)
    try:
        cl = Client(srv.server_address[1])
        cl.login()
        code, data = cl.json("POST", "/api/checkout", {"pack": "p5"},
                             headers={"Host": "evil.example"})
        # Host е чужд — _body() го приема (няма Origin), но адресът за връщане
        # е от --public-url, никога от Host.
        assert code == 200 and data == {"url": fake.url}
        req, timeout = fake.seen[0]
        assert req.full_url == "https://api.stripe.com/v1/checkout/sessions"
        assert req.get_method() == "POST" and timeout == 20
        assert req.get_header("Authorization") == f"Bearer {SK}"
        assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
        assert len(req.get_header("Idempotency-key")) == 36
        assert dict(parse_qsl(req.data.decode())) == {
            "mode": "payment", "line_items[0][price]": "price_p5", "line_items[0][quantity]": "1",
            "success_url": "https://genesis.example/?view=account&paid=1",
            "cancel_url": "https://genesis.example/?view=account",
            "client_reference_id": "1", "metadata[user_id]": "1", "metadata[pack]": "p5",
            "customer_email": "ana@example.com"}
        # абонамент: метаданни и на абонамента; известен клиент вместо имейл
        app.store.set_account(1, stripe_customer="cus_9")
        assert cl.json("POST", "/api/checkout", {"plan": "basic"})[0] == 200
        form = dict(parse_qsl(fake.seen[1][0].data.decode()))
        assert form["mode"] == "subscription" and form["customer"] == "cus_9"
        assert "customer_email" not in form
        assert form["subscription_data[metadata][plan]"] == "basic"
        assert form["subscription_data[metadata][user_id]"] == "1"
        app.store.set_account(1, plan="basic")
        assert cl.json("POST", "/api/checkout", {"plan": "basic"})[0] == 409
        assert cl.json("POST", "/api/checkout", {"pack": "nope"})[0] == 400
        assert cl.json("POST", "/api/checkout", {"pack": "p5"},
                       headers={"Origin": "https://evil.example"})[0] == 403
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("fake", [FakeStripe(url="https://evil.example/pay"), FakeStripe(status=400)])
def test_a_bad_stripe_answer_is_a_502_without_details(tmp_path, fake, capsys) -> None:
    _, srv = _checkout_app(tmp_path, fake)
    try:
        cl = Client(srv.server_address[1])
        cl.login()
        code, data = cl.json("POST", "/api/checkout", {"pack": "p5"})
        assert code == 502 and data == {"error": "плащането не отговаря — опитай пак след малко"}
    finally:
        srv.shutdown()
        srv.server_close()


def test_checkout_without_stripe_or_billing(paid, plain) -> None:
    _, _, client = paid
    cl = client()
    cl.login()
    assert cl.json("POST", "/api/checkout", {"pack": "p5"})[0] == 503
    _, client2 = plain
    cl2 = client2()
    cl2.login()
    assert cl2.json("POST", "/api/checkout", {"pack": "p5"})[0] == 404
    assert Client(cl.port).json("POST", "/api/checkout", {"pack": "p5"})[0] == 401


# ── S12: командите на оператора ──────────────────────────────────────────────

def _cli(tmp_path: Path, *args: str) -> int:
    return web.main([args[0], "--data", str(tmp_path), *args[1:]])


def test_grant_is_exact_and_each_grant_is_its_own_row(tmp_path, capsys) -> None:
    s = Store(tmp_path / "web.db")
    uid = s.add_user("a@x.y", PASSWORD)
    assert _cli(tmp_path, "grant", "A@x.y", "12.5") == 0
    assert s.balance(uid) == 12_500_000
    assert _cli(tmp_path, "grant", "a@x.y", "12.5", "--note", "бонус") == 0
    assert len(s.ledger_entries(uid)) == 2
    assert _cli(tmp_path, "grant", "a@x.y", "-5") == 0
    assert s.ledger_entries(uid)[0]["kind"] == "adjust" and s.balance(uid) == 20 * MICRO
    assert _cli(tmp_path, "grant", "a@x.y", "0.0000001") == 2
    assert _cli(tmp_path, "grant", "a@x.y", "NaN") == 2
    assert _cli(tmp_path, "grant", "a@x.y", "0") == 2
    assert _cli(tmp_path, "grant", "nobody@x.y", "1") == 1
    assert s.balance(uid) == 20 * MICRO
    assert "20.00" in capsys.readouterr().out


def test_disable_from_the_cli_ends_the_live_session(tmp_path) -> None:
    store = Store(tmp_path / "web.db")
    store.add_user("ana@example.com", PASSWORD)
    app = web.App(store, tmp_path / "jobs", runner=_NoRun(), secure_cookie=False)
    srv = _serve(app)
    try:
        cl = Client(srv.server_address[1])
        assert cl.login() == 200
        assert _cli(tmp_path, "disable", "ana@example.com") == 0
        assert cl.req("GET", "/api/me")[0] == 401
        assert Client(cl.port).login() == 401
        assert _cli(tmp_path, "enable", "ana@example.com") == 0
        assert Client(cl.port).login() == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_invite_codes_from_the_cli_work_once(tmp_path, capsys) -> None:
    assert _cli(tmp_path, "invite", "--count", "3", "--credits", "5",
                "--public-url", "https://genesis.example/") == 0
    lines = capsys.readouterr().out.strip().splitlines()
    codes = [line.split()[0] for line in lines]
    assert len(set(codes)) == 3
    assert lines[0].endswith(f"https://genesis.example/?invite={codes[0]}")
    s = Store(tmp_path / "web.db")
    for i, code in enumerate(codes):
        uid = s.signup(code, f"u{i}@x.y", PASSWORD)
        assert s.balance(uid) == 5 * MICRO
        with pytest.raises(ValueError):
            s.signup(code, f"again{i}@x.y", PASSWORD)


def test_users_balance_and_limits(tmp_path, capsys) -> None:
    s = Store(tmp_path / "web.db")
    uid = s.add_user("a@x.y", PASSWORD)
    s.add_entry(uid, "admin:x", "grant", 7_250_000)
    assert _cli(tmp_path, "users") == 0
    out = capsys.readouterr().out
    assert "a@x.y" in out and "7.25" in out
    assert _cli(tmp_path, "balance", "a@x.y") == 0
    assert "7.25 кредита" in capsys.readouterr().out
    assert _cli(tmp_path, "limits", "a@x.y", "--turns-per-day", "3", "--max-tokens", "5000",
                "--monthly-cap", "10") == 0
    acc = s.account(uid)
    assert (acc["turns_per_day"], acc["max_tokens_per_turn"], acc["monthly_cap"]) == \
        (3, 5000, 10 * MICRO)
    assert _cli(tmp_path, "limits", "a@x.y", "--clear") == 0
    assert s.account(uid)["turns_per_day"] is None and s.account(uid)["monthly_cap"] is None
    assert _cli(tmp_path, "balance", "x@x.y") == 1


def test_reconcile_from_the_cli(tmp_path, capsys) -> None:
    s = Store(tmp_path / "web.db")
    uid = s.add_user("a@x.y", PASSWORD)
    turn = s.add_turn(s.new_chat(uid, "t"), "x")
    s.set_job(turn, "genesis-task-1")
    s.finish_turn(turn, ok=True, error="", tokens={}, seconds=1)
    usage = tmp_path / "u.jsonl"
    gw.Ledger(usage).add("genesis-task-1", "groq", "x", {"prompt_tokens": 100})
    assert _cli(tmp_path, "reconcile", "--gateway-usage", str(usage)) == 0
    assert _cli(tmp_path, "reconcile", "--gateway-usage", str(usage)) == 0
    assert capsys.readouterr().out.split()[-1] == "0"
    assert s.balance(uid) == -(2 * MICRO + 100 * 80)
