"""Одит, седми кръг (2026-11-10) — облакът. Всеки тест пада без поправката си."""
from __future__ import annotations

import http.client
import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from cloud.gateway import gateway as gw
from cloud.runner import launch
from cloud.web import billing
from cloud.web import server as web
from cloud.web.store import MICRO, Store

PASSWORD = "correct-horse-battery"
SECRET = "s" * 40
_PRICES: dict[str, Any] = {
    "default": {"input": 300, "cached": 75, "output": 1200},
    "models": {"groq/*": {"input": 80, "cached": 40, "output": 100}},
    "per_turn": 2, "min_turn_tokens": 20000,
    "packs": {"p5": {"credits": 500, "amount_cents": 500, "currency": "eur",
                     "stripe_price": "price_p5"}},
    "plans": {"basic": {"credits": 1500, "amount_cents": 1500, "currency": "eur",
                        "stripe_price": "price_basic"}},
}


def _prices(**over: Any) -> billing.Prices:
    return billing.parse_prices({**json.loads(json.dumps(_PRICES)), **over})


def _app(tmp_path: Path, runner: Any = None, **kw: Any) -> web.App:
    store = Store(tmp_path / "web.db")
    store.add_user("ana@example.com", PASSWORD)
    gateway = launch.Gateway(SECRET, tmp_path / "usage.jsonl", ("GROQ_API_KEY",))
    return web.App(store, tmp_path / "jobs",
                   runner=runner or (lambda *a, **k: launch.Result(True, False, 0, 0.1)),
                   secure_cookie=False, gateway=gateway, cleanup=lambda: 0,
                   prices=kw.pop("prices", None) or _prices(), **kw)


def _serve(server: Any) -> Any:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _post(port: int, path: str, body: bytes, headers: dict[str, str]) -> int:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        c.request("POST", path, body=body, headers=headers)
        r = c.getresponse()
        r.read()
        return r.status
    finally:
        c.close()


def _idle(app: web.App, uid: int) -> None:
    for _ in range(500):
        if not app.store.busy(user_id=uid):
            return
        time.sleep(0.02)
    raise AssertionError("ходът не завърши")


# ── шлюзът: отказ с тяло над MAX_BODY ────────────────────────────────────────

def test_the_gateway_refusal_survives_a_body_over_the_limit(tmp_path) -> None:
    """Поправката от 05.11 изхвърляше тялото само до MAX_BODY: 6 MB → BrokenPipe в
    29 от 30 проби вместо 413, и 24 от 30 вместо 429."""
    g = gw.Gateway(SECRET, {"GROQ_API_KEY": "gsk_real"}, gw.Ledger(tmp_path / "usage.jsonl"),
                   opener=lambda *a, **k: None)
    srv = _serve(gw.make_server(g, "127.0.0.1", 0))
    port = srv.server_address[1]
    big = b'{"model": "m", "messages": "' + b"a" * (3 * gw.MAX_BODY) + b'"}'
    ok = gw.make_token(SECRET, "job1", budget=1000, ttl=60)
    spent = gw.make_token(SECRET, "job2", budget=100, ttl=60)
    g.ledger.add("job2", "groq", "m", {"total_tokens": 100})
    try:
        for _ in range(4):
            assert _post(port, "/groq/chat/completions", big, {"Authorization": f"Bearer {ok}"}) == 413
            assert _post(port, "/groq/chat/completions", big,
                         {"Authorization": f"Bearer {spent}"}) == 429
    finally:
        srv.shutdown()
        srv.server_close()


# ── BOM в keys.env и prices.json ─────────────────────────────────────────────

def test_a_bom_does_not_hide_the_first_key(tmp_path) -> None:
    keys = tmp_path / "keys.env"
    keys.write_bytes(b"\xef\xbb\xbfGATEWAY_SECRET=" + SECRET.encode() + b"\r\nGROQ_API_KEY=gsk_x\r\n")
    assert gw.read_keys(keys) == {"GATEWAY_SECRET": SECRET, "GROQ_API_KEY": "gsk_x"}
    prices = tmp_path / "prices.json"
    prices.write_bytes(b"\xef\xbb\xbf" + json.dumps(_PRICES).encode())
    assert billing.load_prices(prices).per_turn == 2


# ── уеб сървърът: счупен Origin, отказ с голямо тяло ─────────────────────────

def test_a_malformed_origin_is_a_403_not_a_dead_thread(tmp_path) -> None:
    app = _app(tmp_path)
    srv = _serve(web.make_server(app, "127.0.0.1", 0))
    try:
        s = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=10)
        s.sendall(b"POST /api/login HTTP/1.1\r\nHost: x\r\nOrigin: http://[\r\n"
                  b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
        s.settimeout(10)
        answer = s.recv(200)
        s.close()
        assert answer.startswith((b"HTTP/1.1 403", b"HTTP/1.0 403"))
    finally:
        srv.shutdown()
        srv.server_close()
        app.pool.shutdown(wait=True)


def test_the_web_refusal_survives_a_big_body(tmp_path) -> None:
    """3 MB към /api/login: 413 се губеше (BrokenPipe) в 3 от 20 проби."""
    app = _app(tmp_path)
    srv = _serve(web.make_server(app, "127.0.0.1", 0))
    big = b'{"email":"' + b"a" * 8_000_000 + b'"}'
    try:
        for _ in range(8):
            assert _post(srv.server_address[1], "/api/login", big,
                         {"Content-Type": "application/json"}) == 413
    finally:
        srv.shutdown()
        srv.server_close()
        app.pool.shutdown(wait=True)


# ── „Сметка“ ─────────────────────────────────────────────────────────────────

def test_a_subscriber_is_not_told_card_payments_are_off(tmp_path) -> None:
    stripe = billing.StripeConfig("sk_test_123", "whsec_test", "https://genesis.example")
    no_pack = {"p5": {"credits": 500, "amount_cents": 500, "currency": "eur", "stripe_price": ""}}
    app = _app(tmp_path, stripe=stripe, prices=_prices(packs=no_pack))
    ana = app.store.user_by_email("ana@example.com")
    assert app.account_view(ana)["can_buy"] is True
    event = {"id": "evt_1", "livemode": False, "type": "checkout.session.completed",
             "data": {"object": {"id": "cs_test_1", "mode": "subscription",
                                 "payment_status": "paid", "customer": "cus_1",
                                 "client_reference_id": str(ana["id"]),
                                 "metadata": {"plan": "basic"}}}}
    assert billing.handle_event(app.store, app.prices, event, live=False)[0] == 200
    view = app.account_view(ana)
    assert view["plan"] == "basic" and view["can_buy"] is True
    app.pool.shutdown(wait=True)


def test_the_account_page_does_not_show_a_hold_after_the_token_is_dead(tmp_path) -> None:
    def runner(text: str, workspace: Path, *, gateway: Any, name: str, on_token: Any,
               limits: Any, **kw: Any) -> launch.Result:
        on_token(int(time.time()) + launch.token_ttl(limits))
        gw.Ledger(gateway.usage).add(name, "groq", "x",
                                     {"prompt_tokens": 1000, "completion_tokens": 500})
        return launch.Result(True, False, 0, 0.1)

    app = _app(tmp_path, runner=runner)
    ana = app.store.user_by_email("ana@example.com")
    uid = ana["id"]
    app.store.add_entry(uid, "admin:start", "grant", 100 * MICRO)
    turn = app.start_turn(ana, app.store.new_chat(uid, "t"), "x")
    _idle(app, uid)
    assert app.account_view(ana)["held"] != "0.00"      # жетонът още е жив
    app.store.job_started(turn, time.time() - 2 * 3600)
    assert app.account_view(ana)["held"] == "0.00"
    app.pool.shutdown(wait=True)


# ── лимитът на токените за ход ───────────────────────────────────────────────

def test_a_zero_token_limit_stops_the_person_without_a_charge(tmp_path) -> None:
    started: list[int] = []

    def runner(text: str, workspace: Path, *, gateway: Any, **kw: Any) -> launch.Result:
        started.append(gateway.budget)
        return launch.Result(False, False, 0, 0.1, error="gateway 429")

    app = _app(tmp_path, runner=runner)
    ana = app.store.user_by_email("ana@example.com")
    uid = ana["id"]
    app.store.add_entry(uid, "admin:start", "grant", 100 * MICRO)
    assert web.main(["limits", "ana@example.com", "--data", str(tmp_path), "--max-tokens", "0"]) == 0
    with pytest.raises(web.TurnRefused) as refused:
        app.start_turn(ana, app.store.new_chat(uid, "t"), "x")
    assert refused.value.status == 403
    _idle(app, uid)
    assert started == [] and app.store.balance(uid) == 100 * MICRO
    app.pool.shutdown(wait=True)


def test_a_limit_above_the_default_reaches_the_token(tmp_path) -> None:
    seen: list[int] = []

    def runner(text: str, workspace: Path, *, gateway: Any, **kw: Any) -> launch.Result:
        seen.append(gateway.budget)
        return launch.Result(True, False, 0, 0.1)

    wanted = launch.TASK_BUDGET * 2
    app = _app(tmp_path, runner=runner, max_tokens_per_turn=wanted)
    ana = app.store.user_by_email("ana@example.com")
    uid = ana["id"]
    app.store.add_entry(uid, "admin:start", "grant", 100_000 * MICRO)
    app.start_turn(ana, app.store.new_chat(uid, "t"), "x")
    _idle(app, uid)
    assert seen == [wanted]
    app.pool.shutdown(wait=True)


# ── страницата: „Прати“ след „Нов разговор“ насред задача ────────────────────

_HARNESS = r"""
const fs = require("fs");
const els = {};
function mk(id) {
  const kids = [];
  const e = { id, hidden: false, disabled: false, textContent: "", value: "", dataset: {}, listeners: {},
    className: "", children: kids, tBodies: [], elements: { code: {} },
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(t, f) { this.listeners[t] = f; },
    append(...n) { kids.push(...n); }, replaceChildren(...n) { kids.length = 0; kids.push(...n); },
    insertBefore(n) { kids.push(n); }, setAttribute() {}, focus() {}, requestSubmit() {},
    scrollTop: 0, scrollHeight: 0 };
  e.tBodies = [{ replaceChildren() {}, append() {} }];
  return e;
}
global.document = { getElementById: (id) => (els[id] ||= mk(id)), createElement: (t) => mk(t) };
global.location = { search: "", pathname: "/" };
global.history = { replaceState() {} };
global.FormData = class {};
let turnStatus = "running", chats = [];
global.fetch = async (path, opts = {}) => {
  let data = {};
  if (path === "/api/me") data = { email: "ana@example.com", balance: "100.00" };
  else if (path === "/api/chats" && opts.method === "POST") { chats.push({ id: "c1", title: "t" }); data = { id: "c1" }; }
  else if (path === "/api/chats") data = { chats };
  else if (path === "/api/chats/c1") data = { id: "c1", turns: [] };
  else if (path.endsWith("/turns")) data = { turn_id: 1 };
  else if (path.includes("/events")) data = { status: turnStatus, events: [], tokens: {}, seconds: 1 };
  else if (path.endsWith("/files")) data = { files: [], busy: turnStatus === "running", truncated: false };
  else if (path === "/api/account") data = { billing: true, stripe: false, packs: [], plans: [], history: [], held: "0.00", balance: "100.00", month_spent: "0", monthly_cap: "2000" };
  return { status: 200, ok: true, json: async () => data };
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  new Function(fs.readFileSync(process.argv[2], "utf8"))();
  await sleep(50);
  const send = document.getElementById("send");
  els["text"].value = "csv";
  await els["ask"].listeners.submit({ preventDefault() {} });
  await sleep(50);
  const running = send.disabled;
  await els["new-chat"].listeners.click();
  turnStatus = "ok";
  await sleep(300);
  console.log(JSON.stringify({ running, after: send.disabled, label: send.textContent }));
  process.exit(0);
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="иска node")
def test_send_is_free_again_after_new_chat_during_a_running_turn(tmp_path) -> None:
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    app_js = Path(web.__file__).parent / "static" / "app.js"
    out = subprocess.run(["node", str(harness), str(app_js)], capture_output=True, timeout=60,
                         encoding="utf-8", check=True).stdout
    state = json.loads(out.strip().splitlines()[-1])
    assert state["running"] is True
    assert state["after"] is False and state["label"] == "Прати"
