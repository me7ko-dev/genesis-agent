"""Одитът на /resume, /rewind, /undo и записа на разговора (2026-10-22).

Всеки тест пада срещу кода отпреди поправката си (пуснато срещу стария код).
"""
from __future__ import annotations

import json
import multiprocessing as mp
from collections import deque
from pathlib import Path

import pytest

import genesis_skills as gs
import genesis_terminal_agent as gta
from genesis_agent import chat_commands, edit_history, hooks, session_index, todos


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(gs, "_WORKSPACE", root)
    monkeypatch.setattr(gta, "WORKSPACE", root)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    gs._SEEN_PATHS.clear()
    hooks.workspace = root
    (root / "app.py").write_text("v0\n", encoding="utf-8")
    yield root
    hooks.workspace = None
    gs._set_agent_scope(None)
    gs._set_cancel(None)


def _main_loop(monkeypatch, lines: list[str], answers: list[str], argv=("genesis",)) -> list[str]:
    """Истинският gta.main() с подадени редове и отговори на console.input.
    Връща кои редове чатът е прочел."""
    from genesis_agent import brain, chat_input, model_check
    monkeypatch.setattr(brain, "any_model_reachable", lambda: True, raising=False)
    monkeypatch.setattr(gta, "print_minimal_banner", lambda: None)
    monkeypatch.setattr(gta, "show_status_bar", lambda: None)
    monkeypatch.setattr(gta, "build_system_prompt", lambda: ("SYS", ""))
    monkeypatch.setattr(gta, "_worth_capturing", lambda convo: False)
    monkeypatch.setattr(model_check, "needs_check", lambda: False)
    monkeypatch.setattr(gta.sys, "argv", list(argv))
    queue = iter(lines)
    read: list[str] = []

    def next_line(_read):
        line = next(queue)
        read.append(line)
        return line
    monkeypatch.setattr(chat_input, "read_message", next_line)
    replies = iter(answers)

    def answer(*_a, **_k):
        reply = next(replies, "")
        if isinstance(reply, BaseException) or (isinstance(reply, type)
                                                and issubclass(reply, BaseException)):
            raise reply
        return reply
    monkeypatch.setattr(gta.console, "input", answer)
    gta.main()
    return read


def _older_conversation(ws: Path) -> Path:
    hist = gta.HISTORY_DIR
    old = hist / "session_20200101_000000.json"
    old.write_text(json.dumps([{"role": "system", "content": "x"},
                               {"role": "user", "content": "стар разговор"},
                               {"role": "assistant", "content": "ок"}], ensure_ascii=False),
                   encoding="utf-8")
    session_index.record(hist, ws, old)
    return old


def _turn_that_changed(path: Path) -> None:
    edit_history.begin_turn("направи v1")
    edit_history.record(path)
    path.write_text("v1\n", encoding="utf-8")
    edit_history.forget_if_unchanged(path)
    edit_history.end_turn()
    assert edit_history.pending()


# ── 1. /resume и /history забравят ходовете за /undo и задачите ──────────────

def _desktop(state: dict):
    from genesis_agent import desktop_commands as dc
    return dc, dc.CommandContext(get_messages=lambda: state["m"],
                                 set_messages=lambda m: state.update(m=m),
                                 busy=lambda: False, emit=lambda k, **d: None)


def _load_and_undo(route: str, monkeypatch, old: Path) -> None:
    if route == "desktop":
        # „История“ в Desktop/телефона (desktop_commands.load_history), после /undo.
        state = {"m": [{"role": "system", "content": "SYS"}]}
        dc, ctx = _desktop(state)
        assert dc.run("load_history", {"file": old.name}, ctx)["ok"]
        chat_commands.handle("/undo", messages=state["m"], workspace=gta.WORKSPACE,
                             out=lambda t: None, ask=lambda q: "да")
        return
    lines, picks = {"resume": (["/resume 1"], []), "history": (["/history"], ["1"])}[route]
    read = _main_loop(monkeypatch, [*lines, "/undo", "exit"], [*picks, "да", "да"])
    assert read[-1] == "exit"


@pytest.mark.parametrize("route", ["resume", "history", "desktop"])
def test_loading_another_conversation_forgets_the_undo_turns_and_todos(ws, monkeypatch, route):
    """След /resume (и /history, и „История“ в Desktop) /undo връщаше хода от
    ПРЕДИШНИЯ разговор: app.py ставаше пак „v0“, а в заредения разговор
    отиваше бележка за чужди промени; /todos показваше задачите на онзи
    разговор (одит 2026-10-22)."""
    app = ws / "app.py"
    old = _older_conversation(ws)
    _turn_that_changed(app)
    todos.write([{"content": "мигрирай базата", "status": "in_progress"}])
    _load_and_undo(route, monkeypatch, old)
    assert edit_history.pending() == []
    assert app.read_text(encoding="utf-8") == "v1\n"
    assert todos.items() == []


def test_loading_a_conversation_on_the_desktop_keeps_the_one_before(ws):
    """desktop_commands.load_history сменяше само session_start_time, не файла
    на разговора: следващият ход презаписваше файла на ПРЕДИШНИЯ разговор
    със заредения — онзи изчезваше от /resume и /history (одит 2026-10-22)."""
    current = deque([{"role": "system", "content": "SYS"},
                     {"role": "user", "content": "разговор А"},
                     {"role": "assistant", "content": "ок А"}], maxlen=gta._HISTORY_MAXLEN)
    gta._save_session(current)
    file_a = gta._session_file()
    old = _older_conversation(ws)
    state = {"m": list(current)}
    dc, ctx = _desktop(state)
    assert dc.run("load_history", {"file": old.name}, ctx)["ok"]
    gta._save_session([*state["m"], {"role": "user", "content": "следващ ход"}])
    assert "разговор А" in file_a.read_text(encoding="utf-8")
    assert gta._session_file() not in (file_a, old)


# ── 2. Провален запис не трие запазения разговор ─────────────────────────────

def test_a_failed_session_save_keeps_the_conversation_saved_before(ws, monkeypatch):
    """Файлът се пишеше на място ('w' го изпразва първо): пълен диск по средата
    на json.dump оставяше 60 KB от 84 KB JSON и /resume вече не предлагаше
    разговора — губеше се целият, не само последният ход (одит 2026-10-22)."""
    msgs = deque([{"role": "system", "content": "SYS"}], maxlen=gta._HISTORY_MAXLEN)
    for i in range(5):
        msgs.append({"role": "user", "content": f"задача {i}"})
        msgs.append({"role": "assistant", "content": f"готово {i}"})
    gta._save_session(msgs)
    saved = gta._session_file()
    before = session_index.read(saved)
    assert len(before) == 11
    msgs.append({"role": "user", "content": "още една"})
    real_dumps = json.dumps

    def full_disk(obj, fp, **kw):
        text = real_dumps(obj, **kw)
        fp.write(text[:len(text) // 2])
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(json, "dump", full_disk)
    with pytest.raises(OSError):
        gta._save_session(msgs)
    assert session_index.read(saved) == before
    found, bad = session_index.entries(gta.HISTORY_DIR, ws)
    assert bad == 0 and [e.path for e in found] == [saved]
    assert not [p.name for p in gta.HISTORY_DIR.iterdir() if p.name.endswith(".tmp")]


# ── 3. Ctrl-C в избора на /resume и /rewind само го отказва ──────────────────

def _interrupt(*_a, **_k):
    raise KeyboardInterrupt


def _eof(*_a, **_k):
    raise EOFError


def test_ctrl_c_at_a_picker_cancels_it_and_the_chat_goes_on(ws, monkeypatch):
    """Ctrl-C при „Кой разговор да заредя?“ стигаше до главния цикъл и чатът
    свършваше („Довиждане!“); при `genesis -r` — суров traceback. Ctrl-C при
    „Да върна ли?“ (Enter = да) не бива да се брои за „да“ (одит 2026-10-22)."""
    hist = gta.HISTORY_DIR
    _older_conversation(ws)
    app = ws / "app.py"
    conv = chat_commands.Conversations(history_dir=hist, new_file=lambda: None)

    def run(text, *, ask=lambda q: "", choose=None, messages=None):
        said: list[str] = []
        msgs = messages if messages is not None else deque(
            [{"role": "system", "content": "SYS"}, {"role": "user", "content": "направи v1"},
             {"role": "assistant", "content": "готово"}], maxlen=200)
        try:
            res = chat_commands.handle(text, messages=msgs, workspace=ws, out=said.append,
                                       ask=ask, conversations=conv, choose=choose)
        except (KeyboardInterrupt, EOFError) as e:
            pytest.fail(f"{text}: {type(e).__name__} излезе от командата вместо отказ")
        return res, "\n".join(said)

    for cancel in (_interrupt, _eof):
        res, out = run("/resume", choose=cancel)
        assert res is not None and res.messages is None and "Нищо не е заредено" in out
        res, out = run("/rewind", choose=cancel)
        assert res is not None and res.messages is None and "Нищо не е върнато" in out
        res, out = run("/rewind 1", ask=cancel)
        assert res is not None and res.messages is None and "Нищо не е върнато" in out
    _turn_that_changed(app)
    res, out = run("/undo", ask=_interrupt)
    assert res is not None and "Нищо не е върнато" in out
    assert app.read_text(encoding="utf-8") == "v1\n" and edit_history.pending()

    # Истинският чат: `genesis -r` + Ctrl-C в избора, после /resume + Ctrl-C.
    try:
        read = _main_loop(monkeypatch, ["/resume", "/todos", "exit"],
                          [KeyboardInterrupt, KeyboardInterrupt], argv=("genesis", "-r"))
    except KeyboardInterrupt:
        pytest.fail("Ctrl-C в избора на `genesis -r` излезе от main()")
    assert read == ["/resume", "/todos", "exit"]


# ── 4. Два чата едновременно не губят разговори от индекса ───────────────────

def test_two_chats_recording_at_once_keep_every_conversation(tmp_path):
    """record() четеше, променяше и пишеше индекса без ключалка: два процеса,
    по 60 разговора всеки — /resume показваше 48 и 51 (одит 2026-10-22)."""
    hist = tmp_path / "hist"
    hist.mkdir()
    folders = {"a": tmp_path / "a", "b": tmp_path / "b"}
    jobs = []
    n = session_index._MAX_PER_WORKSPACE
    for i in range(n):
        for tag, folder in folders.items():
            folder.mkdir(exist_ok=True)
            path = hist / f"session_{tag}_{i:03}.json"
            path.write_text("[]", encoding="utf-8")
            jobs.append((hist, folder, path))
    ctx = mp.get_context("spawn")
    start = ctx.Barrier(2)
    # Двата работника тръгват заедно — иначе първият често свършва, преди
    # вторият да се е вдигнал, и надпреварата не се случва.
    with ctx.Pool(2, initializer=start.wait) as pool:
        pool.starmap(session_index.record, jobs, chunksize=1)
    for folder in folders.values():
        assert len(session_index.sessions(hist, folder)) == n
