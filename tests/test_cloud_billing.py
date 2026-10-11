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

from cloud.runner import launch
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
    store = Store(tmp_path / "web.db")
    store.add_user("ana@example.com", PASSWORD)
    app = web.App(store, tmp_path / "jobs", runner=_NoRun(), secure_cookie=False)
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
