"""/resume и /rewind (2026-10-15), както в Claude Code.

Досега имаше само `genesis -c` (последният разговор в папката) и /undo (само
последният ход). /resume избира по-ранен разговор на ТАЗИ папка; /rewind
връща разговора до избрано съобщение и файловете, които Genesis промени след
него — през /undo на всеки ход (edit_history), не отделен механизъм. Всеки
тест тук пада срещу кода отпреди (няма /resume, /rewind, session_index.sessions).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from collections import deque
from pathlib import Path

import pytest

import genesis_skills as gs
import genesis_terminal_agent as gta
from genesis_agent import chat_commands, cli, edit_history, session_index

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="няма git (checkpoints)")


# ── помощници ────────────────────────────────────────────────────────────────

class Calls:
    def __init__(self) -> None:
        self.new_files = 0
        self.saved: list = []

    def conversations(self, hist: Path, current: Path | None = None) -> chat_commands.Conversations:
        def new_file() -> None:
            self.new_files += 1
        return chat_commands.Conversations(history_dir=hist, new_file=new_file,
                                           save=self.saved.append, current=lambda: current)


def _run(text: str, *, messages=None, ws: Path, hist: Path | None = None, answer: str = "",
         choose=None, calls: Calls | None = None, current: Path | None = None):
    said: list[str] = []
    asked: list[str] = []

    def ask(q: str) -> str:
        asked.append(q)
        return answer
    calls = calls or Calls()
    msgs = deque([{"role": "system", "content": "SYS"}], maxlen=200) if messages is None else messages
    res = chat_commands.handle(text, messages=msgs, workspace=ws, out=said.append, ask=ask,
                               conversations=calls.conversations(hist or ws, current),
                               choose=choose)
    return res, "\n".join(said), asked


def _session(hist: Path, ws: Path, name: str, convo: list[dict], mtime: float) -> Path:
    path = hist / name
    path.write_text(json.dumps([{"role": "system", "content": "стар промпт"}, *convo],
                               ensure_ascii=False), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    session_index.record(hist, ws, path)
    return path


def _qa(q: str, a: str) -> list[dict]:
    return [{"role": "user", "content": q}, {"role": "assistant", "content": a}]


@pytest.fixture
def dirs(tmp_path):
    hist, ws, other = tmp_path / "hist", tmp_path / "ws", tmp_path / "other"
    for d in (hist, ws, other):
        d.mkdir()
    return hist, ws, other


# ── /resume ──────────────────────────────────────────────────────────────────

def test_resume_lists_only_this_folders_conversations_newest_first(dirs) -> None:
    hist, ws, other = dirs
    _session(hist, ws, "session_1.json", _qa("оправи входа", "готово"), 1_700_000_000)
    _session(hist, ws, "session_2.json", _qa("добави тестове за " + "x" * 200, "ок") +
             _qa("и README", "ок"), 1_700_100_000)
    _session(hist, other, "session_3.json", _qa("ЧУЖДА ПАПКА", "ок"), 1_700_200_000)
    res, out, _ = _run("/resume", ws=ws, hist=hist)
    assert res is not None and res.messages is None
    assert "ЧУЖДА" not in out
    assert out.index("добави тестове") < out.index("оправи входа")   # най-новият пръв
    assert "4 реплики" in out and "2 реплики" in out
    assert "…" in out and "x" * 100 not in out                       # съкратено
    assert "/resume <номер>" in out                                  # без choose: номер след командата


def test_resume_with_a_number_loads_it_with_the_current_system_prompt(dirs) -> None:
    hist, ws, _ = dirs
    _session(hist, ws, "session_1.json", _qa("стара задача", "стар отговор"), 1_700_000_000)
    _session(hist, ws, "session_2.json", _qa("нова задача", "нов отговор"), 1_700_100_000)
    calls = Calls()
    res, out, _ = _run("/resume 2", ws=ws, hist=hist, calls=calls)
    assert res is not None and res.messages is not None
    contents = [m["content"] for m in res.messages]
    assert contents == ["SYS", "стара задача", "стар отговор"]
    assert res.messages.maxlen == 200
    assert calls.new_files == 1                      # продължението — в нов файл
    assert "стар отговор" in out and "✓" in out


def test_resume_without_saved_conversations_says_so(dirs) -> None:
    hist, ws, other = dirs
    _session(hist, other, "session_1.json", _qa("чужда", "ок"), 1_700_000_000)
    res, out, _ = _run("/resume", ws=ws, hist=hist)
    assert res is not None and res.messages is None
    assert "няма запазени разговори в тази папка" in out.lower()


def test_resume_skips_broken_files_and_says_how_many(dirs) -> None:
    hist, ws, _ = dirs
    _session(hist, ws, "session_1.json", _qa("цял", "ок"), 1_700_000_000)
    for name, raw in (("session_2.json", b"{not json"), ("session_3.json", b'{"role": "user"}'),
                      ("session_4.json", "[{\"role\": \"user\", \"content\": \"Привет\"}]"
                       .encode("cp1251"))):
        (hist / name).write_bytes(raw)
        session_index.record(hist, ws, hist / name)
    _, out, _ = _run("/resume", ws=ws, hist=hist)
    assert "цял" in out and "3 запазени разговора не се четат" in out
    assert out.count("реплик") == 1


def test_resume_with_only_broken_files_still_says_there_is_nothing(dirs) -> None:
    hist, ws, _ = dirs
    (hist / "session_1.json").write_text("[1, 2]", encoding="utf-8")
    session_index.record(hist, ws, hist / "session_1.json")
    _, out, _ = _run("/resume", ws=ws, hist=hist)
    assert "Няма запазени разговори в тази папка" in out and "не се чете" in out


def test_resume_a_file_broken_after_the_listing_is_reported_not_loaded(dirs, monkeypatch) -> None:
    hist, ws, _ = dirs
    path = _session(hist, ws, "session_1.json", _qa("цял", "ок"), 1_700_000_000)
    real = session_index.read
    calls = {"n": 0}

    def flaky(p):
        calls["n"] += 1
        if calls["n"] > 1:            # описанието мина, зареждането — не
            raise ValueError("повреден")
        return real(p)
    monkeypatch.setattr(session_index, "read", flaky)
    res, out, _ = _run("/resume 1", ws=ws, hist=hist)
    assert res is not None and res.messages is None and path.name in out


@pytest.mark.parametrize("pick", ["9", "0", "абв", "-1"])
def test_resume_with_a_wrong_number_changes_nothing(dirs, pick) -> None:
    hist, ws, _ = dirs
    _session(hist, ws, "session_1.json", _qa("цял", "ок"), 1_700_000_000)
    calls = Calls()
    res, out, _ = _run(f"/resume {pick}", ws=ws, hist=hist, calls=calls)
    assert res is not None and res.messages is None and calls.new_files == 0
    assert "от 1 до 1" in out


def test_resume_in_the_terminal_asks_for_the_number(dirs) -> None:
    hist, ws, _ = dirs
    _session(hist, ws, "session_1.json", _qa("задачата", "отговорът"), 1_700_000_000)
    res, _, _ = _run("/resume", ws=ws, hist=hist, choose=lambda q: "1")
    assert res is not None and "задачата" in [m["content"] for m in res.messages]
    res, out, _ = _run("/resume", ws=ws, hist=hist, choose=lambda q: "")
    assert res is not None and res.messages is None and "Нищо не е заредено" in out


def test_resume_does_not_offer_the_conversation_already_open(dirs) -> None:
    hist, ws, _ = dirs
    old = _session(hist, ws, "session_1.json", _qa("старият", "ок"), 1_700_000_000)
    cur = _session(hist, ws, "session_2.json", _qa("текущият", "ок"), 1_700_100_000)
    _, out, _ = _run("/resume", ws=ws, hist=hist, current=cur)
    assert "старият" in out and "текущият" not in out
    assert old.exists()


def test_resume_index_cannot_lead_outside_the_history(dirs, tmp_path) -> None:
    hist, ws, _ = dirs
    (tmp_path / "session_x.json").write_text(json.dumps(_qa("отвън", "ок")), encoding="utf-8")
    (hist / session_index.ALL_NAME).write_text(
        json.dumps({session_index._key(ws): ["../session_x.json", "/etc/passwd"]}), encoding="utf-8")
    _, out, _ = _run("/resume", ws=ws, hist=hist)
    assert "отвън" not in out and "Няма запазени" in out
    (hist / session_index.ALL_NAME).write_text("не е json", encoding="utf-8")
    assert session_index.sessions(hist, ws) == []


def test_the_conversation_from_before_the_list_existed_is_offered(dirs) -> None:
    # Отпреди 2026-10-15 индексът помнеше само последния (by_workspace.json).
    hist, ws, _ = dirs
    path = hist / "session_old.json"
    path.write_text(json.dumps(_qa("отпреди", "ок")), encoding="utf-8")
    (hist / session_index.INDEX_NAME).write_text(
        json.dumps({session_index._key(ws): path.name}), encoding="utf-8")
    assert session_index.sessions(hist, ws) == [path]
    assert session_index.latest(hist, ws) == path


def test_the_list_per_folder_is_bounded(dirs, monkeypatch) -> None:
    hist, ws, _ = dirs
    monkeypatch.setattr(session_index, "_MAX_PER_WORKSPACE", 3)
    for i in range(5):
        p = hist / f"session_{i}.json"
        p.write_text("[]", encoding="utf-8")
        session_index.record(hist, ws, p)
    names = json.loads((hist / session_index.ALL_NAME).read_text(encoding="utf-8"))
    assert names[session_index._key(ws)] == ["session_2.json", "session_3.json", "session_4.json"]
    assert session_index.latest(hist, ws) == hist / "session_4.json"   # -c както досега


def test_empty_conversations_are_not_offered(dirs) -> None:
    hist, ws, _ = dirs
    _session(hist, ws, "session_1.json", [], 1_700_000_000)
    _, out, _ = _run("/resume", ws=ws, hist=hist)
    assert "Няма запазени" in out


def test_resume_through_the_chat_lists_saved_turns_and_genesis_r_loads(tmp_path, monkeypatch) -> None:
    hist, ws = tmp_path / "hist", tmp_path / "ws"
    hist.mkdir()
    ws.mkdir()
    monkeypatch.setattr(gta, "HISTORY_DIR", hist)
    monkeypatch.setattr(gta, "WORKSPACE", ws)
    monkeypatch.setattr(gta, "PERSIST_HISTORY", True)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    monkeypatch.setattr(gta, "ask_genesis", lambda messages, tools=None: ("Отговор.", None))
    gta.run_turn(deque([{"role": "system", "content": "s"}]), "първи разговор", gta.TurnUI())
    first = gta._session_file()
    gta._new_session()
    monkeypatch.setattr(gta, "session_start_time", gta.session_start_time + 5)
    gta.run_turn(deque([{"role": "system", "content": "s"}]), "втори разговор", gta.TurnUI())
    assert gta._session_file() != first
    entries, bad = session_index.entries(hist, ws, exclude=gta._session_file())
    assert [e.first for e in entries] == ["първи разговор"] and bad == 0

    printed: list[str] = []
    monkeypatch.setattr(gta.console, "print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    restored = gta._resume_at_start(deque([{"role": "system", "content": "нов"}], maxlen=200),
                                    ["-r", "1"])
    assert [m["content"] for m in restored][:2] == ["нов", "първи разговор"]
    assert gta._SESSION_FILE is None                # продължението — в нов файл


def test_cli_resume_opens_the_chat(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(cli, "_chat", lambda: called.append(1) or 0)
    assert cli.main(["-r"]) == 0 and cli.main(["--resume", "2"]) == 0 and len(called) == 2
    assert "genesis -r" in cli.USAGE


def test_help_lists_both_commands() -> None:
    import inspect
    src = inspect.getsource(gta.main)
    assert '"/rewind [номер]"' in src and '"/resume [номер]"' in src


# ── /rewind ──────────────────────────────────────────────────────────────────

@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("v0\n", encoding="utf-8")
    (root / "old.txt").write_text("ще бъде изтрит\n", encoding="utf-8")
    return root


def _write(path: Path, data: bytes | str) -> None:
    """WRITE_FILE / EDIT_FILE, както ги вижда edit_history."""
    edit_history.record(path)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    edit_history.forget_if_unchanged(path)


def _command(ws: Path, change) -> None:
    """RUN_CMD, както я вижда _before_tool/_after_tool."""
    edit_history.before_command(ws)
    change()
    edit_history.after_command()


def _turn(messages: deque, text: str, change=lambda: None) -> dict:
    asked = {"role": "user", "content": text}
    messages.append(asked)
    edit_history.begin_turn(text)
    edit_history.mark(asked)
    change()
    edit_history.end_turn()
    messages.append({"role": "assistant", "content": f"готово: {text}"})
    return asked


def _fresh() -> deque:
    return deque([{"role": "system", "content": "SYS"}], maxlen=200)


def test_rewind_lists_only_the_operators_messages(proj) -> None:
    msgs = _fresh()
    _turn(msgs, "първо")
    msgs.append({"role": "user", "content": "[оператор] /undo върна файловете"})
    msgs.append({"role": "system", "content": "[Резултат]: x"})
    _turn(msgs, "второ\n\n## Проверено знание: дълго")
    msgs.append({"role": "user", "content": "[hook Stop на оператора — довърши]"})
    res, out, _ = _run("/rewind", messages=msgs, ws=proj)
    assert res is not None and res.messages is None
    assert "1. „първо“" in out and "2. „второ“" in out
    assert "оператор]" not in out and "hook Stop" not in out and "Проверено" not in out
    assert "/rewind <номер>" in out


@needs_git
def test_rewind_across_several_turns_with_commands_restores_files_and_history(proj) -> None:
    app, old = proj / "app.py", proj / "old.txt"
    msgs = _fresh()
    _turn(msgs, "edit app", lambda: _write(app, "v1\n"))

    def build() -> None:
        app.write_text("v2 от команда\n", encoding="utf-8")
        old.unlink()
        (proj / "gen").mkdir()
        (proj / "gen" / "out.txt").write_text("генерирано\n", encoding="utf-8")
    _turn(msgs, "пусни генератора", lambda: _command(proj, build))
    _turn(msgs, "нов файл", lambda: _write(proj / "new.txt", "нов\n"))

    res, out, asked = _run("/rewind 2", messages=msgs, ws=proj)
    # Потвърждението казва кои файлове ще се променят — преди да ги пипне.
    assert asked and "app.py" in out and "out.txt" in out and "new.txt" in out and "old.txt" in out
    assert res is not None and res.messages is not None
    assert [m["content"] for m in res.messages] == ["SYS", "edit app", "готово: edit app"]
    assert app.read_text(encoding="utf-8") == "v1\n"                 # до преди хода, не до v0
    assert old.read_text(encoding="utf-8") == "ще бъде изтрит\n"
    assert not (proj / "gen" / "out.txt").exists() and not (proj / "new.txt").exists()
    assert "Съобщението беше:\nпусни генератора" in out
    assert edit_history.pending() and "app.py" in edit_history.pending()[0]  # ход 1 остава за /undo

    res2, _, _ = _run("/rewind 1", messages=res.messages, ws=proj)
    assert app.read_text(encoding="utf-8") == "v0\n"
    assert [m["content"] for m in res2.messages] == ["SYS"]
    assert edit_history.pending() == []


def test_rewind_refused_changes_nothing(proj) -> None:
    app = proj / "app.py"
    msgs = _fresh()
    _turn(msgs, "edit", lambda: _write(app, "v1\n"))
    res, out, _ = _run("/rewind 1", messages=msgs, ws=proj, answer="не")
    assert res is not None and res.messages is None and "Нищо не е върнато" in out
    assert app.read_text(encoding="utf-8") == "v1\n" and len(msgs) == 3
    assert edit_history.pending()


def test_a_file_the_operator_edited_since_is_not_overwritten(proj) -> None:
    app = proj / "app.py"
    msgs = _fresh()
    _turn(msgs, "edit", lambda: _write(app, "v1\n"))
    _turn(msgs, "създай", lambda: _write(proj / "made.txt", "от Genesis\n"))
    app.write_text("ръчно от оператора\n", encoding="utf-8")
    (proj / "made.txt").write_text("и тук ръчно\n", encoding="utf-8")
    res, out, _ = _run("/rewind 1", messages=msgs, ws=proj)
    plan = out.split("↶")[0]          # казаното ПРЕДИ потвърждението
    assert plan.count("НЕ се презаписва") == 2
    assert "app.py" in plan and "made.txt" in plan and "изтрий" not in plan
    assert app.read_text(encoding="utf-8") == "ръчно от оператора\n"
    assert (proj / "made.txt").read_text(encoding="utf-8") == "и тук ръчно\n"   # не е изтрит
    # Моделът научава, че тези файлове не са както преди точката.
    assert res is not None and "НЕ са върнати" in res.messages[-1]["content"]


def test_an_edit_between_two_turns_is_kept(proj) -> None:
    notes = proj / "notes.txt"
    msgs = _fresh()
    _turn(msgs, "създай бележки", lambda: _write(notes, "от Genesis\n"))
    notes.write_text("ръчна редакция\n", encoding="utf-8")       # операторът, между ходовете
    _turn(msgs, "допиши", lambda: _write(notes, "ръчна редакция\n+ Genesis\n"))
    _, out, _ = _run("/rewind 1", messages=msgs, ws=proj)
    assert "между ходовете" in out
    assert notes.read_text(encoding="utf-8") == "ръчна редакция\n"   # нито изтрит, нито „от Genesis“


def test_a_file_the_operator_created_after_the_point_stays(proj) -> None:
    msgs = _fresh()
    _turn(msgs, "edit", lambda: _write(proj / "app.py", "v1\n"))
    (proj / "mine.txt").write_text("мое\n", encoding="utf-8")
    _run("/rewind 1", messages=msgs, ws=proj)
    assert (proj / "mine.txt").read_text(encoding="utf-8") == "мое\n"
    assert (proj / "app.py").read_text(encoding="utf-8") == "v0\n"


@needs_git
def test_crlf_and_cp1251_files_get_their_exact_bytes_back(proj) -> None:
    win = proj / "win.txt"
    cyr = proj / "bg.txt"
    win.write_bytes(b"a\r\nb\r\n")
    cyr.write_bytes("Привет, свят\r\n".encode("cp1251"))
    msgs = _fresh()
    _turn(msgs, "edit", lambda: _write(win, b"x\n"))
    _turn(msgs, "команда", lambda: _command(proj, lambda: (cyr.write_bytes(b"utf8 \xd0\x9f\n"),
                                                           win.write_bytes(b"y\r\n"))))
    _run("/rewind 1", messages=msgs, ws=proj)
    assert win.read_bytes() == b"a\r\nb\r\n"
    assert cyr.read_bytes() == "Привет, свят\r\n".encode("cp1251")


def test_rewind_into_a_resumed_conversation_says_files_are_not_tracked(proj) -> None:
    app = proj / "app.py"
    msgs = deque([{"role": "system", "content": "SYS"}, *_qa("от вчера", "ок"),
                  *_qa("и още", "ок")], maxlen=200)
    app.write_text("ръчно\n", encoding="utf-8")
    res, out, _ = _run("/rewind 1", messages=msgs, ws=proj)
    assert "предишно пускане" in out and "Файловете не се пипат" in out
    assert [m["content"] for m in res.messages] == ["SYS"]
    assert app.read_text(encoding="utf-8") == "ръчно\n"


def test_turns_of_an_earlier_conversation_are_not_undone(proj) -> None:
    app = proj / "app.py"
    earlier = _fresh()
    _turn(earlier, "в предишния разговор", lambda: _write(app, "v1\n"))
    msgs = _fresh()                                                 # /clear
    _turn(msgs, "в този", lambda: _write(proj / "b.txt", "b\n"))
    _run("/rewind 1", messages=msgs, ws=proj)
    assert not (proj / "b.txt").exists()
    assert app.read_text(encoding="utf-8") == "v1\n"                # чуждият ход остава
    assert edit_history.pending() == [f"върни {app.resolve()}"]


def test_rewind_saves_the_cut_conversation_in_a_new_file(proj) -> None:
    msgs = _fresh()
    _turn(msgs, "едно")
    _turn(msgs, "две")
    calls = Calls()
    _run("/rewind 2", messages=msgs, ws=proj, calls=calls)
    assert calls.new_files == 1
    assert [m["content"] for m in calls.saved[0]] == ["SYS", "едно", "готово: едно"]


@pytest.mark.parametrize("pick", ["5", "0", "x"])
def test_rewind_with_a_wrong_number_changes_nothing(proj, pick) -> None:
    msgs = _fresh()
    _turn(msgs, "едно", lambda: _write(proj / "app.py", "v1\n"))
    res, out, asked = _run(f"/rewind {pick}", messages=msgs, ws=proj)
    assert res is not None and res.messages is None and not asked and "от 1 до 1" in out
    assert (proj / "app.py").read_text(encoding="utf-8") == "v1\n"


def test_rewind_without_messages_says_so(proj) -> None:
    res, out, _ = _run("/rewind", ws=proj)
    assert res is not None and res.messages is None and "Няма твои съобщения" in out


def test_rewind_in_the_terminal_asks_for_the_number(proj) -> None:
    msgs = _fresh()
    _turn(msgs, "едно", lambda: _write(proj / "app.py", "v1\n"))
    res, _, _ = _run("/rewind", messages=msgs, ws=proj, choose=lambda q: "1")
    assert (proj / "app.py").read_text(encoding="utf-8") == "v0\n"
    assert [m["content"] for m in res.messages] == ["SYS"]


def test_only_the_first_message_of_a_turn_is_its_point() -> None:
    first, nudge = {"role": "user", "content": "a"}, {"role": "user", "content": "b"}
    edit_history.begin_turn("a")
    edit_history.mark(first)
    edit_history.mark(nudge)          # подканата на hook Stop в същия ход
    edit_history.end_turn()
    assert edit_history.tracked(first) and not edit_history.tracked(nudge)


@needs_git
def test_rewind_after_real_turns_through_run_turn(tmp_path, monkeypatch) -> None:
    ws, hist = tmp_path / "proj", tmp_path / "hist"
    ws.mkdir()
    hist.mkdir()
    (ws / "data.txt").write_text("v0\n", encoding="utf-8")
    monkeypatch.setattr(gs, "_WORKSPACE", ws)
    monkeypatch.setattr(gta, "WORKSPACE", ws)
    monkeypatch.setattr(gta, "HISTORY_DIR", hist)
    monkeypatch.setattr(gta, "PERSIST_HISTORY", True)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    py = Path(sys.executable).as_posix()

    def call(name, args, cid):
        return [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]
    replies = iter([
        ("", call("WRITE_FILE", {"path": "notes.txt", "content": "бележки\n"}, "w1")), ("Записах.", None),
        ("", call("RUN_CMD", {"command": f'"{py}" -c "open(\'made.txt\',\'w\').write(\'1\')"'}, "r1")),
        ("Пуснах.", None)])
    monkeypatch.setattr(gta, "ask_genesis", lambda messages, tools=None: next(replies, ("Готово.", None)))
    msgs = gta.run_turn(_fresh(), "запиши data", gta.TurnUI())
    msgs = gta.run_turn(msgs, "пусни скрипта", gta.TurnUI())
    assert (ws / "made.txt").exists() and (ws / "notes.txt").exists()
    before = gta._session_file()

    said: list[str] = []
    res = chat_commands.handle("/rewind 1", messages=msgs, workspace=ws, out=said.append,
                               ask=lambda q: "", conversations=gta._conversations())
    assert res is not None and res.messages is not None
    assert not (ws / "made.txt").exists() and not (ws / "notes.txt").exists()
    assert (ws / "data.txt").read_text(encoding="utf-8") == "v0\n"
    assert [m["role"] for m in res.messages] == ["system"]
    # Отрязаният разговор — в нов файл; старият остава за /resume.
    assert session_index.latest(hist, ws) != before
    assert "пусни скрипта" in before.read_text(encoding="utf-8")


# ── от телефона ──────────────────────────────────────────────────────────────

def _phone_session():
    pytest.importorskip("cryptography")
    from genesis_agent import remote_server as rs
    return rs, rs.RemoteSession(lambda text, ui: None)


class _UI:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warns: list[str] = []

    def info(self, text: str) -> None:
        self.infos.append(text)

    def warn(self, text: str) -> None:
        self.warns.append(text)


def test_resume_from_the_phone_lists_then_loads_by_number(dirs) -> None:
    rs, session = _phone_session()
    hist, ws, _ = dirs
    _session(hist, ws, "session_1.json", _qa("от телефона", "ок"), 1_700_000_000)
    calls = Calls()
    ui = _UI()
    msgs, prompt = rs.phone_command("/resume", ui, session, messages=_fresh(), workspace=ws,
                                    conversations=calls.conversations(hist))
    assert prompt is None and not ui.warns and "/resume <номер>" in "\n".join(ui.infos)
    msgs, prompt = rs.phone_command("/resume 1", ui, session, messages=_fresh(), workspace=ws,
                                    conversations=calls.conversations(hist))
    assert prompt is None and "от телефона" in [m["content"] for m in msgs]
    assert "/rewind" in rs.PHONE_HELP and "/resume" in rs.PHONE_HELP


def test_rewind_from_the_phone_asks_on_a_card_and_restores(proj) -> None:
    rs, session = _phone_session()
    app = proj / "app.py"
    msgs = _fresh()
    _turn(msgs, "edit", lambda: _write(app, "v1\n"))
    result: dict = {}

    def go() -> None:
        result["out"] = rs.phone_command("/rewind 1", _UI(), session, messages=msgs, workspace=proj)
    t = threading.Thread(target=go)
    t.start()
    deadline = time.time() + 5
    card = None
    while card is None and time.time() < deadline:
        card = next((e for e in session.events_after(0)["events"] if e["type"] == "confirm"), None)
        time.sleep(0.01)
    assert card is not None and "app.py" in card["operation"]     # какво ще се върне — на картата
    assert session.answer(card["id"], True)
    t.join(5)
    new, prompt = result["out"]
    assert prompt is None and [m["content"] for m in new] == ["SYS"]
    assert app.read_text(encoding="utf-8") == "v0\n"
