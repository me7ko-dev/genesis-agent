"""Голямата проверка 2026-10-09: телефонът (`genesis serve`), облакът и главният
цикъл. Всеки тест пада без поправката си (пуснато срещу стария код)."""
from __future__ import annotations

import contextlib
import io
import json
import socket
import threading
import time
import urllib.request
from collections import deque
from types import SimpleNamespace

import pytest

import genesis_skills as gs
import genesis_terminal_agent as gta
from genesis_agent import agent_core, hooks, sandbox, todos
from genesis_agent import remote_server as rs

KEY = bytes(range(32))


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(gs, "_WORKSPACE", root)
    monkeypatch.setattr(gta, "WORKSPACE", root)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    gs._SEEN_PATHS.clear()
    hooks.workspace = root
    (root / "app.py").write_text("PRICE = 42\n", encoding="utf-8")
    yield root
    hooks.workspace = None
    gs._set_agent_scope(None)
    gs._set_cancel(None)


def _call(name, args, cid="c1", raw=None):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": raw if raw is not None else json.dumps(args)}}


def _script(monkeypatch, replies):
    it = iter(replies)
    seen: list[list[dict]] = []

    def fake(messages, tools=None):
        seen.append([dict(m) for m in messages])
        r = next(it)
        if isinstance(r, BaseException):
            raise r
        return r
    monkeypatch.setattr(gta, "ask_genesis", fake)
    return seen


class _UI(gta.TurnUI):
    def __init__(self):
        self.log: list = []

    def thinking(self, label, spinner="dots"):
        return contextlib.nullcontext()

    def assistant(self, text): self.log.append(("assistant", text))
    def tool(self, name, result): self.log.append(("tool", name, result))
    def asked(self, q): self.log.append(("asked", q))
    def spinning(self, n): self.log.append(("spin", n))
    def warn(self, t): self.log.append(("warn", t))
    def info(self, t): self.log.append(("info", t))


def _fresh():
    return deque([{"role": "system", "content": "SYS"}], maxlen=gta._HISTORY_MAXLEN)


def _wait(session, timeout=15):
    deadline = time.time() + timeout
    time.sleep(0.05)
    while session.busy and time.time() < deadline:
        time.sleep(0.02)


# ── телефонът ──────────────────────────────────────────────────────────────

def test_the_phone_sees_the_whole_command_it_approves(tmp_path, monkeypatch):
    (tmp_path / "build").mkdir()
    (tmp_path / "src").mkdir()
    session = rs.RemoteSession(lambda t, ui: None)
    monkeypatch.setattr(sandbox, "_POLICY", sandbox.SandboxPolicy(
        mode="interactive", confirm_fn=lambda op, v: session.confirm(op, list(v.reasons))))

    def phone():  # внимателен оператор: одобрява само ако `src` не се пипа
        for _ in range(500):
            ev = [e for e in session.events_after(0)["events"] if e["type"] == "confirm"]
            if ev:
                session.answer(ev[0]["id"], "src" not in ev[0]["operation"])
                return
            time.sleep(0.01)
    threading.Thread(target=phone, daemon=True).start()
    sandbox.run_shell('rm -r build && echo "' + "cleaning " * 120 + '" && rm -r src',
                      cwd=tmp_path, timeout=20)
    assert (tmp_path / "src").exists()


def test_an_operation_too_long_to_show_is_refused():
    session = rs.RemoteSession(lambda t, ui: None)
    assert session.confirm("x" * (sandbox.MAX_SHOWN_OPERATION + 1), ["r"], timeout=5) is False
    assert not [e for e in session.events_after(0)["events"] if e["type"] == "confirm"]


def test_confirm_after_stop_does_not_wait():
    session = rs.RemoteSession(lambda t, ui: None)
    session.request_stop()
    started = time.monotonic()
    assert session.confirm("rm -r x", ["r"], timeout=3) is False
    assert time.monotonic() - started < 1


def test_stop_reaches_the_rest_of_the_round(ws, monkeypatch):
    holder = {"msgs": _fresh()}
    session = rs.RemoteSession(lambda text, ui: holder.__setitem__(
        "msgs", gta.run_turn(holder["msgs"], text, ui)))
    _script(monkeypatch, [("пиша 5", [_call("WRITE_FILE", {"path": f"f{i}.txt", "content": "x"}, f"c{i}")
                                      for i in range(5)]), ("готово", None)])
    real = gs.dispatch_tool_call

    def dispatch(name, args):
        out = real(name, args)
        session.request_stop()      # „Стоп“ идва по време на първия запис
        return out
    monkeypatch.setattr(gs, "dispatch_tool_call", dispatch)
    session.send("направи файлове")
    _wait(session)
    assert sorted(p.name for p in ws.glob("f*.txt")) == ["f0.txt"]
    tool_msgs = [m for m in holder["msgs"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 5 and "Спряно от оператора" in tool_msgs[-1]["content"]


def test_stop_reaches_a_sub_agent(ws, monkeypatch):
    d = ws / ".genesis" / "agents"
    d.mkdir(parents=True)
    (d / "writer.md").write_text("---\nname: writer\ndescription: пише\ntools: WRITE_FILE\n---\nПиши.\n",
                                 encoding="utf-8")
    holder = {"msgs": _fresh()}
    session = rs.RemoteSession(lambda text, ui: holder.__setitem__(
        "msgs", gta.run_turn(holder["msgs"], text, ui)))
    rounds = {"n": 0}

    class FakeBrain:
        def __init__(self, *a, **k):
            pass

        def complete(self, messages, tools=None):
            rounds["n"] += 1
            if rounds["n"] == 1:
                session.request_stop()
            return SimpleNamespace(raw_text="", tool_calls=[_call(
                "WRITE_FILE", {"path": f"sub{rounds['n']}.txt", "content": "x"}, f"s{rounds['n']}")])
    monkeypatch.setattr("genesis_agent.brain.Brain", FakeBrain)
    _script(monkeypatch, [("възлагам", [_call("AGENT", {"agent": "writer", "task": "пиши"}, "a")]),
                          ("готово", None)])
    session.send("go")
    _wait(session, 20)
    assert rounds["n"] == 1 and not list(ws.glob("sub*.txt"))


def _drive_serve(monkeypatch, scenario):
    """Истинският `rs.serve()` с фалшив HTTP цикъл."""
    monkeypatch.setattr(rs, "_print_qr", lambda *a: None)
    cap: dict = {}
    real_init = rs.RemoteServer.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        cap["server"] = self

    class FakeHTTP:
        def serve_forever(self, poll_interval=0.5):
            scenario(cap["server"])
            raise KeyboardInterrupt

        def server_close(self):
            pass
    monkeypatch.setattr(rs.RemoteServer, "__init__", init)
    monkeypatch.setattr(rs.RemoteServer, "make_http", lambda self, h, p: FakeHTTP())
    assert rs.serve(["--port", "0"]) == 0


def test_clear_from_the_phone_keeps_the_previous_conversation(ws, tmp_path, monkeypatch):
    monkeypatch.setattr(gta, "build_system_prompt", lambda: ("SYS", ""))
    monkeypatch.setattr(gta, "ask_genesis", lambda messages, tools=None: ("ок", None))

    def scenario(srv):
        srv.session.send("първи разговор: план А")
        _wait(srv.session)
        srv._dispatch({"op": "send", "text": "/clear"})
        srv.session.send("втори разговор")
        _wait(srv.session)
    _drive_serve(monkeypatch, scenario)
    texts = [f.read_text(encoding="utf-8") for f in gta.HISTORY_DIR.glob("session_*.json")]
    assert len(texts) == 2 and any("план А" in t for t in texts)


def test_the_terminal_shows_phone_text_as_text(monkeypatch, capsys):
    monkeypatch.setattr(gta, "build_system_prompt", lambda: ("SYS", ""))

    def scenario(srv):
        srv.session.emit("user", text="изтрий [/] папката build")
        srv.session.emit("confirm", id="x", operation="rm -r [bold]build[/bold] src", reasons=[])
    _drive_serve(monkeypatch, scenario)
    out = capsys.readouterr().out
    assert "папката build" in out and "rm -r [bold]build[/bold] src" in out


def _serve(web_root=None):
    session = rs.RemoteSession(lambda text, ui: ui.assistant("hi"))
    server = rs.RemoteServer(KEY, session, name="pc", web_root=web_root)
    httpd = server.make_http("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return server, httpd


def test_half_sent_requests_time_out_and_are_capped(monkeypatch):
    monkeypatch.setattr(rs._QuietServer, "_MAX_CONNECTIONS", 4)
    server, httpd = _serve()
    httpd.RequestHandlerClass.timeout = 1
    port = httpd.server_address[1]
    socks = []
    try:
        for _ in range(8):
            s = socket.create_connection(("127.0.0.1", port))
            s.sendall(b"POST /api/v1 HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\n\r\n{")
            socks.append(s)
        time.sleep(0.3)
        extra = socks[-1]
        extra.settimeout(2)
        try:                                  # над тавана — затворена веднага
            closed = extra.recv(10) == b""
        except ConnectionError:               # Windows: RST вместо FIN
            closed = True
        assert closed
        first = socks[0]
        first.settimeout(5)
        with contextlib.suppress(OSError):
            first.recv(10)                    # след таймаута — затворена
        time.sleep(0.3)
        assert len(server._failures.get("127.0.0.1", [])) >= 1
    finally:
        for s in socks:
            s.close()
        httpd.shutdown()


def test_bad_static_paths_get_the_app(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>app</html>")
    _server, httpd = _serve(web_root=web)
    port = httpd.server_address[1]
    try:
        for path in (b"/a\x00b", b"/" + b"a" * 5000):
            s = socket.create_connection(("127.0.0.1", port))
            s.sendall(b"GET " + path + b" HTTP/1.1\r\nHost: x\r\n\r\n")
            s.settimeout(3)
            assert s.recv(200).startswith(b"HTTP/1.0 200")
            s.close()
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("payload", [{"op": "events", "after": 1e400, "rid": "abcdef0123456789"},
                                     {"op": "status", "rid": "ключ-ключ-ключ"}])
def test_malformed_authenticated_fields_get_an_answer(payload):
    _server, httpd = _serve()
    port = httpd.server_address[1]
    body = json.dumps(rs.Cipher(KEY).seal({"ts": int(time.time() * 1000), **payload},
                                          rs._REQ_AAD)).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 401
    finally:
        httpd.shutdown()


# ── облакът ────────────────────────────────────────────────────────────────

def test_one_request_cannot_spend_past_the_budget(tmp_path):
    from cloud.gateway import gateway as gw

    class Upstream:   # доставчик, който таксува колкото е поискано
        def __call__(self, req, timeout=0):
            body = json.loads(req.data)
            prompt = len(req.data) // 4
            usage = {"prompt_tokens": prompt, "completion_tokens": body.get("max_tokens", 0),
                     "total_tokens": prompt + body.get("max_tokens", 0)}

            class R(io.BytesIO):
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return R(json.dumps({"choices": [], "usage": usage}).encode())
    g = gw.Gateway("s" * 40, {"GROQ_API_KEY": "k"}, gw.Ledger(tmp_path / "u.jsonl"), opener=Upstream())
    grant = gw.Grant("job1", 10 ** 12, 300_000)
    body = json.dumps({"model": "m", "max_tokens": 64_000,
                       "messages": [{"role": "user", "content": "x" * 600_000}]}).encode()
    status, _ = g.forward("groq", body, grant)
    assert status == 200 and g.ledger.used("job1") <= 300_000
    huge = json.dumps({"model": "m", "messages": [{"role": "user", "content": "x" * 1_900_000}]}).encode()
    assert g.forward("groq", huge, gw.Grant("job2", 10 ** 12, 300_000))[0] == 429


def test_a_container_event_cannot_change_its_number(tmp_path):
    from cloud.web.store import Store
    st = Store(tmp_path / "w.db")
    uid = st.add_user("a@b.cc", "x" * 12)
    turn = st.add_turn(st.new_chat(uid, "t"), "hi")
    st.add_event(turn, {"kind": "log", "text": "1"})
    st.add_event(turn, {"kind": "log", "text": "2", "seq": 0})
    assert [e["seq"] for e in st.events(turn, 0)] == [1, 2]


# ── главният цикъл ─────────────────────────────────────────────────────────

def test_broken_json_arguments_do_not_run_the_tool_with_nothing(ws, monkeypatch):
    todos.write([{"content": "планът", "status": "in_progress"}])
    _script(monkeypatch, [("", [_call("TODO_WRITE", {}, raw='{"todos": [{"content": "a"')]),
                          ("", [_call("TODO_WRITE", {}, "c2", raw='[{"content": "a"}]')]),
                          ("ок", None)])
    ui = _UI()
    gta.run_turn(_fresh(), "x", ui)
    assert todos.items() == [{"content": "планът", "status": "in_progress"}]
    results = [entry[2] for entry in ui.log if entry[0] == "tool"]
    assert len(results) == 2 and all("Невалидни аргументи" in r for r in results)


def test_ctrl_c_keeps_the_done_work_and_does_not_end_the_chat(ws, monkeypatch):
    _script(monkeypatch, [("", [_call("WRITE_FILE", {"path": "made.py", "content": "x = 1\n"}, "w1")]),
                          KeyboardInterrupt()])
    ui = _UI()
    msgs = gta.run_turn(_fresh(), "първа задача", ui)    # не хвърля
    assert (ws / "made.py").exists()
    roles = [m["role"] for m in msgs]
    assert roles[:4] == ["system", "user", "assistant", "tool"]
    assert any("Прекъснато" in str(e) for e in ui.log)
    assert list(gta.HISTORY_DIR.glob("session_*.json"))


def test_ctrl_c_mid_round_answers_every_open_tool_call(ws, monkeypatch):
    _script(monkeypatch, [("", [_call("READ_FILE", {"path": "app.py"}, "r1"),
                                _call("WRITE_FILE", {"path": "n.py", "content": "1"}, "w1")]), ("ок", None)])
    real = gs.dispatch_tool_call

    def dispatch(name, args):
        if name == "WRITE_FILE":
            raise KeyboardInterrupt
        return real(name, args)
    monkeypatch.setattr(gs, "dispatch_tool_call", dispatch)
    msgs = gta.run_turn(_fresh(), "x", _UI())
    tools = [m for m in msgs if m["role"] == "tool"]
    assert [t["tool_call_id"] for t in tools] == ["r1", "w1"] and "прекъснато" in tools[1]["content"]


def test_tests_never_write_the_operators_chat_history(tmp_path):
    assert str(gta.HISTORY_DIR).startswith(str(tmp_path.parent))


def test_parallel_code_search_that_may_ask_runs_one_by_one(monkeypatch):
    monkeypatch.setattr(sandbox, "_POLICY", sandbox.SandboxPolicy(mode="interactive"))
    assert gta._may_ask("SEARCH_CODE", {"pattern": "KEY"})
    monkeypatch.setattr(sandbox, "_POLICY", sandbox.SandboxPolicy(mode="deny"))
    assert not gta._may_ask("SEARCH_CODE", {"pattern": "KEY"})


def test_text_tag_results_are_kept_when_the_model_asks(ws, monkeypatch):
    _script(monkeypatch, [("[READ_FILE: app.py] [ASK_USER: коя цена?]", None)])
    msgs = gta.run_turn(_fresh(), "x", _UI())
    assert any("PRICE = 42" in str(m.get("content")) for m in msgs)


def test_undo_on_a_full_history_keeps_the_system_prompt(ws, monkeypatch):
    from genesis_agent import chat_commands, edit_history
    full = deque([{"role": "system", "content": "SYS"}] +
                 [{"role": "user", "content": str(i)} for i in range(29)], maxlen=30)
    edit_history.begin_turn("x")
    gs.dispatch_tool_call("WRITE_FILE", {"path": "n.py", "content": "1"})
    edit_history.end_turn()
    res = chat_commands.handle("/undo", messages=full, workspace=ws, out=lambda t: None,
                               ask=lambda p: "")
    assert res is not None and res.messages[0]["content"] == "SYS"
    assert "/undo" in res.messages[-1]["content"]


def test_restored_history_keeps_text_mode_results():
    saved = [{"role": "system", "content": "стар промпт"}, {"role": "user", "content": "цена?"},
             {"role": "assistant", "content": "[READ_FILE: app.py]"},
             {"role": "system", "content": "[Резултат]:\nPRICE = 42"},
             {"role": "assistant", "content": "42"}]
    out = list(agent_core.restored_history(saved, "нов промпт"))
    assert out[0]["content"] == "нов промпт" and "стар промпт" not in str(out)
    assert any("PRICE = 42" in str(m["content"]) for m in out)


def test_a_prompt_blocked_by_a_hook_is_reported(ws, monkeypatch):
    monkeypatch.setattr(hooks, "fire", lambda event, data, *a, **k: hooks.Outcome(blocked=True, message="не")
                        if event == "UserPromptSubmit" else hooks.Outcome())
    gta.run_turn(_fresh(), "x", _UI())
    assert gta.LAST_TURN_BLOCKED is True
    monkeypatch.setattr(hooks, "fire", lambda *a, **k: hooks.Outcome())
    _script(monkeypatch, [("ок", None)])
    gta.run_turn(_fresh(), "x", _UI())
    assert gta.LAST_TURN_BLOCKED is False


def test_duplicate_tool_call_ids_are_made_unique(ws, monkeypatch):
    _script(monkeypatch, [("", [_call("READ_FILE", {"path": "app.py"}, "dup"),
                                _call("LIST_DIR", {"path": "."}, "dup")]), ("ок", None)])
    msgs = gta.run_turn(_fresh(), "x", _UI())
    ids = [m["tool_call_id"] for m in msgs if m["role"] == "tool"]
    assert len(set(ids)) == 2
    assistant = next(m for m in msgs if m.get("tool_calls"))
    assert [tc["id"] for tc in assistant["tool_calls"]] == ids
