"""Командите на чата от телефона (2026-10-09): `/plan`, `/undo`, `/todos`,
`/agents` стигаха до модела като обикновен текст, а под-агентите работеха
невидимо — телефонът показваше само „мисли…“."""
from __future__ import annotations

import json
import threading
import time
from collections import deque

import pytest

pytest.importorskip("cryptography")

from genesis_agent import agents, edit_history, hooks, plan_mode, todos
from genesis_agent import remote_server as rs


class UI:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warns: list[str] = []

    def info(self, text: str) -> None:
        self.infos.append(text)

    def warn(self, text: str) -> None:
        self.warns.append(text)


def _session() -> rs.RemoteSession:
    return rs.RemoteSession(lambda text, ui: None)


def _phone(text: str, ws, session=None, messages=None):
    ui = UI()
    msgs = deque([{"role": "system", "content": "s"}], maxlen=50) if messages is None else messages
    out = rs.phone_command(text, ui, session or _session(), messages=msgs, workspace=ws)
    return ui, out[0], out[1]


def test_plan_from_the_phone_switches_plan_mode_without_the_model(tmp_path) -> None:
    ui, _, prompt = _phone("/plan", tmp_path)
    assert prompt is None and plan_mode.active()
    assert any("Режим план" in t for t in ui.infos)
    # Втори /plan — изпълнение на плана: това вече е ход за модела.
    _, _, prompt = _phone("/plan", tmp_path)
    assert not plan_mode.active()
    assert prompt and "Планът е одобрен" in prompt


def test_plan_with_a_task_runs_the_task_in_plan_mode(tmp_path) -> None:
    _, _, prompt = _phone("/plan оправи вход", tmp_path)
    assert prompt == "оправи вход" and plan_mode.active()


def _changed_file(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("старо", encoding="utf-8")
    edit_history.begin_turn("промени a.txt")
    edit_history.record(f)
    f.write_text("ново", encoding="utf-8")
    edit_history.end_turn()
    return f


def _answer_undo(session: rs.RemoteSession, allow: bool, tmp_path) -> tuple[UI, object]:
    result: dict = {}

    def go() -> None:
        result["out"] = _phone("/undo", tmp_path, session)

    t = threading.Thread(target=go)
    t.start()
    deadline = time.time() + 5
    card = None
    while card is None and time.time() < deadline:
        card = next((e for e in session.events_after(0)["events"] if e["type"] == "confirm"), None)
        time.sleep(0.01)
    assert card is not None, "/undo трябва да пита на телефона"
    assert "a.txt" in card["operation"]          # какво ще се върне — на картата
    assert session.answer(card["id"], allow)
    t.join(5)
    ui, msgs, prompt = result["out"]
    assert prompt is None
    return ui, msgs


def test_undo_asks_on_the_phone_and_puts_the_file_back(tmp_path) -> None:
    f = _changed_file(tmp_path)
    session = _session()
    ui, msgs = _answer_undo(session, True, tmp_path)
    assert f.read_text(encoding="utf-8") == "старо"
    assert any("Върнат" in t for t in ui.infos)
    assert "/undo" in msgs[-1]["content"]        # моделът научава за връщането


def test_undo_refused_on_the_phone_changes_nothing(tmp_path) -> None:
    f = _changed_file(tmp_path)
    ui, _ = _answer_undo(_session(), False, tmp_path)
    assert f.read_text(encoding="utf-8") == "ново"
    assert any("Нищо не е върнато" in t for t in ui.infos)


def test_todos_and_agents_answer_without_the_model(tmp_path) -> None:
    todos.write([{"content": "тестове", "status": "in_progress"}])
    ui, _, prompt = _phone("/todos", tmp_path)
    assert prompt is None and "тестове" in ui.infos[0]
    ui, _, prompt = _phone("/agents", tmp_path)
    assert prompt is None and ui.infos


def test_trust_is_refused_from_the_phone(tmp_path) -> None:
    hook_file = hooks.project_file(tmp_path)
    hook_file.parent.mkdir(parents=True, exist_ok=True)
    hook_file.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "echo PWNED"}]}]}}), encoding="utf-8")
    session = _session()
    ui, _, prompt = _phone("/hooks trust", tmp_path, session)
    assert prompt is None and ui.warns
    active, untrusted = hooks.configured(tmp_path)
    assert untrusted is not None and not active
    # Нито карта за одобрение — няма какво да се докосне по погрешка.
    assert not [e for e in session.events_after(0)["events"] if e["type"] == "confirm"]


@pytest.mark.parametrize("cmd", ["/model", "/history", "/backup", "/update"])
def test_terminal_only_commands_do_not_reach_the_model(tmp_path, cmd) -> None:
    ui, _, prompt = _phone(cmd, tmp_path)
    assert prompt is None and "терминала" in ui.warns[0]


def test_help_lists_what_the_phone_can_do(tmp_path) -> None:
    ui, _, prompt = _phone("/help", tmp_path)
    assert prompt is None and "/undo" in ui.infos[0] and "/plan" in ui.infos[0]


def test_plain_text_and_unknown_commands_still_go_to_the_model(tmp_path) -> None:
    assert _phone("здрасти", tmp_path)[2] == "здрасти"
    assert _phone("/няма-такава", tmp_path)[2] == "/няма-такава"


def test_a_mention_attaches_the_file_from_the_phone(tmp_path) -> None:
    (tmp_path / "notes.txt").write_text("тайната съставка", encoding="utf-8")
    ui, _, prompt = _phone("виж @notes.txt", tmp_path)
    assert prompt and "тайната съставка" in prompt
    assert any(t.startswith("📎") for t in ui.infos)


def test_events_carry_plan_mode_and_the_todo_list() -> None:
    session = rs.RemoteSession(lambda text, ui: None, state=rs.phone_state)
    plan_mode.set_active(True)
    todos.write([{"content": "стъпка", "status": "pending"}])
    state = session.events_after(0)["state"]
    assert state == {"plan": True, "todos": [{"content": "стъпка", "status": "pending"}]}


def test_a_broken_state_does_not_break_the_events() -> None:
    def boom() -> dict:
        raise RuntimeError("x")
    reply = rs.RemoteSession(lambda text, ui: None, state=boom).events_after(0)
    assert "state" not in reply and reply["events"] == []


def test_sub_agent_progress_reaches_the_phone() -> None:
    session = _session()
    rs.show_agent_progress(session)
    agents._say("↳ reviewer: прегледай diff-а")
    events = session.events_after(0)["events"]
    assert events[-1]["type"] == "progress" and "reviewer" in events[-1]["text"]


# ── одит 2026-10-09 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["/plan\nоправи входа", "/plan\tоправи входа", "/PLAN\n\n оправи входа"])
def test_plan_then_a_new_line_still_turns_plan_mode_on(tmp_path, text) -> None:
    # Многоредовото поле на телефона: `/plan`, Enter, задачата. Отиваше при
    # модела като текст, без режим план — с разрешен запис.
    _, _, prompt = _phone(text, tmp_path)
    assert plan_mode.active() and prompt == "оправи входа"


def test_plan_then_a_new_line_in_the_terminal_too(tmp_path) -> None:
    from genesis_agent import chat_commands
    res = chat_commands.handle('/plan\nоправи входа', messages=None, workspace=tmp_path,
                               out=lambda _t: None, ask=lambda _q: "")
    assert res is not None and res.prompt == "оправи входа" and plan_mode.active()


@pytest.mark.parametrize("text", ["/hooks\ntrust", "/hooks\ttrust", "/mcp\n TRUST"])
def test_trust_split_by_a_new_line_is_still_refused(tmp_path, text) -> None:
    session = _session()
    ui, _, prompt = _phone(text, tmp_path, session)
    assert prompt is None and "терминала" in ui.warns[0]
    assert not [e for e in session.events_after(0)["events"] if e["type"] == "confirm"]


def test_the_todo_list_is_never_read_half_old_half_new() -> None:
    # Телефонът чете списъка от HTTP нишката, докато ходът го подменя. Тук
    # подмяната става точно насред копирането — както при превключване на нишка.
    class Switching(dict):
        # Свой __iter__ изключва бързия път на dict(): тогава той вика keys().
        def __iter__(self):
            return iter(self.keys())

        def keys(self):
            todos.write([{"content": "ново", "status": "pending"}])
            return super().keys()

    todos._items[:] = [Switching(content="старо 1", status="pending"),
                       {"content": "старо 2", "status": "pending"}]
    assert [i["content"] for i in todos.items()] == ["старо 1", "старо 2"]
