"""Поток дума по дума в облака (2026-11-23): уеб чатът показваше отговора чак в
края. Контейнерът пише "delta" редове; уеб сървърът държи текста на живо в
паметта (не в базата и не в тавана от 2000 събития) и го дава като "live" в
/api/turns/<id>/events, докато ходът тече. Крайният отговор е "assistant"."""
from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from cloud.runner import launch
from cloud.web import server as web
from cloud.web.store import Store
from tests.test_cloud_web import PASSWORD, Client


class _FakeProc:
    def __init__(self, lines: list[str]) -> None:
        self.stdout = io.BytesIO("".join(line + "\n" for line in lines).encode("utf-8"))

    def wait(self) -> int:
        return 0


def test_the_runner_passes_deltas_on_but_does_not_keep_or_count_them(monkeypatch, tmp_path) -> None:
    lines = [json.dumps({"kind": "delta", "text": "Пра", "start": True}),
             json.dumps({"kind": "delta", "text": "вя го"}),
             json.dumps({"kind": "assistant", "text": "Правя го"}),
             json.dumps({"kind": "done", "ok": True, "tokens": {"total_tokens": 5}})]
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: _FakeProc(lines))
    seen: list[dict] = []
    res = launch.run_task("задача", tmp_path / "ws", on_event=seen.append)
    assert [e["kind"] for e in seen] == ["delta", "delta", "assistant", "done"]
    assert [e["kind"] for e in res.events] == ["assistant", "done"]
    assert res.ok


def test_a_flood_of_deltas_does_not_cut_the_real_events(monkeypatch, tmp_path) -> None:
    """Частите не изяждат тавана на хода: иначе дълъг отговор отрязваше
    инструментите и крайния текст („изходът е отрязан“)."""
    flood = [json.dumps({"kind": "delta", "text": "дума "})] * (launch._MAX_EVENTS + 100)
    lines = [*flood, json.dumps({"kind": "assistant", "text": "край"}),
             json.dumps({"kind": "done", "ok": True, "tokens": {}})]
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: _FakeProc(lines))
    res = launch.run_task("задача", tmp_path / "ws", on_event=lambda e: None)
    assert [e["kind"] for e in res.events] == ["assistant", "done"]


def test_the_app_keeps_the_live_text_in_memory_only(tmp_path) -> None:
    store = Store(tmp_path / "web.db")
    app = web.App(store, tmp_path / "jobs", runner=lambda *a, **k: None, recover=False)
    try:
        app._event(7, {"kind": "delta", "text": "закъсняла"})       # без start — нищо
        assert app.live_text(7) == ""
        app._event(7, {"kind": "delta", "text": "Първи", "start": True})
        app._event(7, {"kind": "delta", "text": "ят модел"})
        assert app.live_text(7) == "Първият модел"
        app._event(7, {"kind": "delta", "text": "Втори", "start": True})   # нов опит
        assert app.live_text(7) == "Втори"
        app._event(7, {"kind": "assistant", "text": "Втори път"})
        assert app.live_text(7) == ""
        assert [e["kind"] for e in store.events(7)] == ["assistant"]
    finally:
        app.pool.shutdown(wait=True)


def test_many_fast_deltas_stay_bounded_and_keep_the_tail(tmp_path) -> None:
    """Ревю 2026-11-23: `текст + част` под общата ключалка — квадратично копиране,
    с което чуждият код в контейнера държеше ключалката на всички ходове. Сега:
    части в ограничена опашка, орязана отпред; низът се сглобява само при четене."""
    store = Store(tmp_path / "web.db")
    app = web.App(store, tmp_path / "jobs", runner=lambda *a, **k: None, recover=False)
    try:
        parts = [f"{i:07d}|" for i in range(30_000)]        # 240 000 знака
        app._event(1, {"kind": "delta", "text": parts[0], "start": True})
        for p in parts[1:]:
            app._event(1, {"kind": "delta", "text": p})
        live = app._live[1]
        assert live.size <= web.LIVE_MAX_CHARS
        assert len(live.chunks) <= web.LIVE_MAX_CHUNKS
        text = app.live_text(1)
        assert text.startswith("…") and len(text) == web.LIVE_MAX_CHARS + 1
        # Отвъд тавана на частите на хода нищо не се добавя.
        whole = "".join(parts[:web.LIVE_MAX_DELTAS])
        assert text[1:] == whole[-web.LIVE_MAX_CHARS:]
        # Една огромна част — само краят ѝ.
        app._event(2, {"kind": "delta", "text": "x" * (3 * web.LIVE_MAX_CHARS) + "КРАЙ", "start": True})
        assert app._live[2].size == web.LIVE_MAX_CHARS and app.live_text(2).endswith("КРАЙ")
    finally:
        app.pool.shutdown(wait=True)


class _StreamingRunner:
    def __init__(self) -> None:
        self.gate = threading.Event()
        self.streamed = threading.Event()

    def __call__(self, text: str, workspace: Path, *, on_event: Any = None, **kw: Any) -> launch.Result:
        workspace.mkdir(parents=True, exist_ok=True)
        on_event({"kind": "delta", "text": "Пиша", "start": True})
        on_event({"kind": "delta", "text": " отговора"})
        self.streamed.set()
        self.gate.wait(10)
        on_event({"kind": "assistant", "text": "Пиша отговора."})
        return launch.Result(True, False, 0, 1.0, tokens={"total_tokens": 1})


@pytest.fixture
def served(tmp_path):
    store = Store(tmp_path / "web.db")
    store.add_user("ana@example.com", PASSWORD)
    runner = _StreamingRunner()
    app = web.App(store, tmp_path / "jobs", runner=runner, secure_cookie=False)
    srv = web.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield runner, Client(srv.server_address[1])
    runner.gate.set()
    srv.shutdown()
    srv.server_close()
    app.pool.shutdown(wait=True)


def test_the_browser_sees_the_live_text_then_the_final_answer(served) -> None:
    runner, cl = served
    cl.login()
    _, chat = cl.json("POST", "/api/chats", {"title": "x"})
    _, data = cl.json("POST", f"/api/chats/{chat['id']}/turns", {"text": "напиши"})
    turn = data["turn_id"]
    assert runner.streamed.wait(10)
    _, now = cl.json("GET", f"/api/turns/{turn}/events")
    assert now["status"] == "running" and now["live"] == "Пиша отговора"
    assert now["events"] == []          # частите не са събития в базата
    runner.gate.set()
    done = cl.wait(turn)
    assert "live" not in done
    assert [(e["kind"], e["text"]) for e in done["events"]] == [("assistant", "Пиша отговора.")]


def test_the_web_page_shows_the_live_text() -> None:
    """Страницата чете "live" и го показва в отделен балон (без него нищо не расте)."""
    js = (Path(web.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "renderLive(box, data.live" in js


def test_the_container_task_streams_the_model_text(capsys, monkeypatch, tmp_path) -> None:
    """cloud/runner/task.py: UI-ят на контейнера има `delta` — run_turn пуска
    потока и редовете "delta" вървят преди крайния "assistant"."""
    import genesis_terminal_agent as gta
    from cloud.runner import task
    from genesis_agent import brain as brain_mod
    from genesis_agent import sandbox
    monkeypatch.setattr(sandbox, "_POLICY", sandbox._POLICY)
    monkeypatch.setenv("GENESIS_WORKSPACE", str(tmp_path))

    def fake_model(messages, tools=None):
        write = brain_mod._delta_writer()
        assert write is not None, "контейнерът не пуска потока"
        write("Здравей")
        return "Здравей", None
    monkeypatch.setattr(gta, "_ask_genesis", fake_model)
    assert task.main(["здрасти"]) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]
    kinds = [e["kind"] for e in lines]
    assert kinds.index("delta") < kinds.index("assistant")
    assert lines[kinds.index("delta")] == {"kind": "delta", "text": "Здравей", "start": True}

