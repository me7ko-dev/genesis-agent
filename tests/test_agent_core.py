"""genesis_agent.agent_core — the one turn loop, plus the helpers around it.

env_facts, restored_history and bounded_history are pure helpers. The loop
(run_tool_loop) runs with a scripted `ask` and genesis_skills patched — no
model, no real tool dispatch. The same loop through the terminal (knowledge,
compaction, the saved session) is in test_run_turn.py."""
from __future__ import annotations

import pytest

import genesis_skills
from genesis_agent import agent_core as ac
from genesis_agent.config import TOOL_ROUND_CAP
from genesis_agent.repeat_guard import STOP_AT


class TestEnvFacts:
    def test_includes_home_directory_and_user(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USER", "testuser")
        out = ac.env_facts()
        assert str(tmp_path) in out
        assert "testuser" in out

    def test_missing_standard_dir_is_flagged(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        out = ac.env_facts()
        assert "(НЕ съществува)" in out

    def test_existing_standard_dir_is_not_flagged_for_that_line(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "Desktop").mkdir()
        out = ac.env_facts()
        desktop_line = next(line for line in out.splitlines() if "Десктоп" in line)
        assert "НЕ съществува" not in desktop_line

    def test_todays_date_is_a_fact_not_a_guess(self, monkeypatch, tmp_path) -> None:
        """На живо моделът кръсти архив с измислена дата отпреди месец."""
        from datetime import date
        monkeypatch.setenv("HOME", str(tmp_path))
        assert f"Днес: {date.today().isoformat()}" in ac.env_facts()

    def test_workspace_line_included_only_when_given(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert "Работна директория" not in ac.env_facts()
        assert "Работна директория" in ac.env_facts("/some/workspace")

    def test_the_user_name_is_found_on_windows_too(self, monkeypatch, tmp_path) -> None:
        """Windows sets USERNAME, not USER, so this line read literally
        "(потребител: unknown)" in every session there — precisely the kind of
        machine fact this function exists to stop the model guessing at."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("USER", raising=False)
        monkeypatch.setenv("USERNAME", "roika")
        assert "roika" in ac.env_facts()

    def test_neither_variable_falls_back_to_the_home_directory_name(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("USER", raising=False)
        monkeypatch.delenv("USERNAME", raising=False)
        assert tmp_path.name in ac.env_facts()

    def test_xdg_user_dirs_override_the_default(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".config").mkdir()
        custom = tmp_path / "MyDesktopFolder"
        custom.mkdir()
        (tmp_path / ".config" / "user-dirs.dirs").write_text(
            f'XDG_DESKTOP_DIR="{custom}"\n', encoding="utf-8"
        )
        out = ac.env_facts()
        assert str(custom) in out


class _UI(ac.TurnUI):
    """Записва какво ходът показва, в реда на показване."""

    def __init__(self) -> None:
        self.shown: list[tuple] = []

    def assistant(self, text: str) -> None:
        self.shown.append(("assistant", text))

    def tool(self, name: str, result: str) -> None:
        self.shown.append(("tool", name, result))

    def asked(self, question: str) -> None:
        self.shown.append(("asked", question))

    def spinning(self, note: str) -> None:
        self.shown.append(("spinning", note))

    def warn(self, text: str) -> None:
        self.shown.append(("warn", text))

    def of(self, kind: str) -> list:
        return [e[1] if len(e) == 2 else e[1:] for e in self.shown if e[0] == kind]


class _Skills:
    """genesis_skills с подготвени резултати: `dispatch` за native извикванията,
    `parse` за текстовите тагове."""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.dispatch = lambda name, args, n: "ok"
        self.parse = lambda text: []
        monkeypatch.setattr(genesis_skills, "dispatch_tool_call", self._dispatch)
        monkeypatch.setattr(genesis_skills, "parse_and_execute_tools", lambda text: self.parse(text))

    def _dispatch(self, name, args):
        self.calls.append((name, args))
        return self.dispatch(name, args, len(self.calls))


@pytest.fixture
def skills(monkeypatch) -> _Skills:
    return _Skills(monkeypatch)


def _loop(replies: list, *, request: str = "hi", history: list | None = None, **kw):
    """Един ход с подготвени отговори на модела; връща (messages, ui, заявките)."""
    script = list(replies)
    asked: list[list[dict]] = []

    def ask(messages):
        asked.append(list(messages))
        assert script, "моделът е попитан повече пъти от подготвените отговори"
        return script.pop(0)

    messages = history if history is not None else [{"role": "user", "content": request}]
    ui = _UI()
    ac.run_tool_loop(messages, request, ui, ask, **kw)
    return messages, ui, asked


def _call(name: str, arguments: str = "{}", cid: str = "1") -> list[dict]:
    return [{"id": cid, "function": {"name": name, "arguments": arguments}}]


class TestAPlainReply:
    def test_it_ends_the_turn(self, skills) -> None:
        messages, ui, _ = _loop([("hello there", None)])
        assert ui.of("assistant") == ["hello there"]
        assert ui.of("tool") == []
        assert messages[-1] == {"role": "assistant", "content": "hello there"}

    def test_both_replies_are_remembered(self, skills) -> None:
        remembered: list = []
        _loop([("", _call("RUN_CMD")), ("готово", None)],
              remember=lambda role, text: remembered.append((role, text)))
        assert remembered == [("assistant", "[повикани 1 tool(-а)]"), ("assistant", "готово")]


class TestNativeToolCalls:
    def test_a_call_is_dispatched_and_the_turn_continues(self, skills) -> None:
        skills.dispatch = lambda name, args, n: "file1\nfile2"
        messages, ui, _ = _loop([("", _call("RUN_CMD", '{"cmd": "ls"}')), ("done", None)])
        assert ui.of("tool") == [("RUN_CMD", "file1\nfile2")]
        assert skills.calls == [("RUN_CMD", {"cmd": "ls"})]
        assert messages[-1] == {"role": "assistant", "content": "done"}

    def test_ask_user_stops_the_turn_and_cleans_the_marker(self, skills) -> None:
        skills.dispatch = lambda name, args, n: f"{genesis_skills.ASK_USER_MARKER}Which file?"
        _, ui, asked = _loop([("", _call("ASK_USER"))])
        assert ui.of("asked") == ["Which file?"]
        assert len(asked) == 1, "въпросът връща контрола на човека, ходът не продължава сам"

    def test_the_round_cap_stops_an_endless_turn(self, skills) -> None:
        skills.dispatch = lambda name, args, n: f"ok {n}"
        _, ui, asked = _loop([("", _call("RUN_CMD"))] * 2, round_cap=2)
        assert len(asked) == 2
        assert "таван" in ui.of("warn")[-1]


class TestSpinningInPlace:
    """Таванът ограничава ЦЕНАТА на въртенето на място, не го разпознава.
    Откакто е 25 (беше 8), един повтарян извик изгаря три пъти повече рундове
    и завършва с "достигнат таван" — най-скъпото съобщение, защото пристига
    последно и не носи нито резултат, нито причина."""

    def test_identical_call_and_result_stops_long_before_the_cap(self, skills) -> None:
        skills.dispatch = lambda name, args, n: "няма такова умение"
        _, ui, _ = _loop([("", _call("USE_SKILL", '{"name_or_query": "foo"}'))] * 60)
        assert len(skills.calls) == STOP_AT
        assert "USE_SKILL" in ui.of("spinning")[-1]

    def test_a_changing_result_is_progress_and_runs_to_the_cap(self, skills) -> None:
        """Обратната страна: предпазителят не бива да реже истинска работа."""
        skills.dispatch = lambda name, args, n: f"резултат {n}"
        _, ui, _ = _loop([("", _call("USE_SKILL", '{"name_or_query": "foo"}'))] * 60)
        assert len(skills.calls) == TOOL_ROUND_CAP
        assert "таван" in ui.of("warn")[-1]

    def test_text_tag_mode_stops_spinning_too(self, skills) -> None:
        skills.parse = lambda text: ["[RUN_CMD: ls]\nсъщият изход"]
        _, ui, asked = _loop([("[RUN_CMD: ls]", None)] * 60)
        assert len(asked) == STOP_AT
        assert len(ui.of("spinning")) == 1
        assert skills.calls == [], "native пътят не бива да тръгва тук"


class TestTextTags:
    def test_they_are_parsed_executed_and_shown(self, skills) -> None:
        skills.parse = lambda text: ["[RUN_CMD: ls]\nfile1\nfile2"] if "[RUN_CMD" in text else []
        messages, ui, _ = _loop([("[RUN_CMD: ls]", None), ("all done", None)])
        assert ui.of("tool") == [("RUN_CMD", "[RUN_CMD: ls]\nfile1\nfile2")]
        assert messages[-1] == {"role": "assistant", "content": "all done"}

    def test_no_parseable_tool_ends_the_turn(self, skills) -> None:
        messages, ui, _ = _loop([("just plain text, no tags", None)])
        assert ui.of("tool") == []
        assert messages[-1] == {"role": "assistant", "content": "just plain text, no tags"}

    def test_the_round_cap_is_announced_here_too_not_only_in_the_native_path(self, skills) -> None:
        """Native клонът казва „достигнат таван“; текстовият спираше нямо.
        Последното, което човекът вижда, е репликата с tool таговете — разказ
        за започната работа — така прекъснатата работа изглежда като
        завършена. И точно този клон обслужва моделите без native tool-calling,
        тоест безплатните: там таванът се удря най-често."""
        skills.parse = lambda text: [text + "\nok"] if "[RUN_CMD" in text else []
        _, ui, _ = _loop([("[RUN_CMD: стъпка 1]", None), ("[RUN_CMD: стъпка 2]", None)],
                         round_cap=2)
        assert "таван" in ui.of("warn")[-1]


class TestToolResultsAreClippedBeforeEnteringHistory:
    """A tool result enters `messages` and is then re-sent on every later
    round until it falls out of the window, so one noisy `cat`/`pip install`
    is paid for repeatedly. The loop must clip what it stores while the
    operator still sees the full output.
    """

    def test_a_huge_native_tool_result_is_clipped_in_messages_but_not_on_screen(
        self, skills, monkeypatch
    ) -> None:
        monkeypatch.setattr("genesis_agent.budget.TOOL_RESULT_MAX_CHARS", 500)
        huge = "".join(f"log line {i}\n" for i in range(3000))
        skills.dispatch = lambda name, args, n: huge
        messages, ui, _ = _loop([("", _call("RUN_CMD", '{"cmd": "cat big.log"}')), ("done", None)])
        assert ui.of("tool") == [("RUN_CMD", huge)], "операторът трябва да вижда пълния изход"
        stored = next(m for m in messages if m.get("role") == "tool")["content"]
        # Близо до зададения таван, не просто "по-малко от огромното" — иначе
        # тестът минава и когато клипването изобщо не се е приложило.
        assert len(stored) < 800
        assert stored.startswith("log line 0")
        assert stored.rstrip().endswith("log line 2999")

    def test_text_tag_results_are_clipped_too(self, skills, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.budget.TOOL_RESULT_MAX_CHARS", 400)
        skills.parse = lambda text: ["x" * 40_000] if "[RUN_CMD" in text else []
        messages, _, _ = _loop([("[RUN_CMD: cat big.log]", None), ("done", None)])
        injected = [m for m in messages
                    if m.get("role") == "system" and "[Резултат]" in m.get("content", "")]
        assert injected, "текстовият път трябва да инжектира резултата"
        assert len(injected[0]["content"]) < 1200


class TestSimulatedWorkIsCaughtMidTurn:
    """The old check only fired on round 0, so any harmless tool call bought
    the model a free pass for the rest of the turn. These assert the check
    now follows what was actually executed, whatever the round."""

    def test_a_claim_after_an_unrelated_tool_call_is_challenged(self, skills) -> None:
        skills.dispatch = lambda name, args, n: "file1\nfile2"
        messages, _, _ = _loop([("", _call("LIST_DIR", '{"path": "/home/user"}')),
                                ("Готово — инсталирах пакета.", None),
                                ("Не съм. Ето какво остава.", None)],
                               request="инсталирай пакета")
        nudges = [m for m in messages if m.get("role") == "system"
                  and "нито един изпълнен инструмент" in str(m.get("content", ""))]
        assert nudges, "неподкрепеното твърдение трябваше да бъде оспорено"

    def test_a_claim_backed_by_the_matching_command_passes_untouched(self, skills) -> None:
        skills.dispatch = lambda name, args, n: "Successfully installed ruff"
        messages, _, _ = _loop([("", _call("RUN_CMD", '{"cmd": "pip install ruff"}')),
                                ("Инсталирах ruff.", None)], request="инсталирай ruff")
        nudges = [m for m in messages if m.get("role") == "system"
                  and "нито един изпълнен инструмент" in str(m.get("content", ""))]
        assert not nudges, "истинската инсталация не бива да се оспорва"


class TestALongTurnKeepsItsTask:
    """2026-09-30: deque(maxlen=30) изхвърляше посред хода системния промпт и
    заявката; ollama после отговаряше празно (done_reason=load) до края."""

    def test_every_request_of_a_20_round_turn_has_the_system_prompt_and_the_task(
        self, skills
    ) -> None:
        rounds = 20
        skills.dispatch = lambda name, args, n: f"[READ_FILE: f{n}.py]\nx = {n}"
        replies = [("", _call("READ_FILE", f'{{"path": "f{i}.py"}}', cid=f"c{i}"))
                   for i in range(rounds)] + [("готово", None)]
        history = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "ЗАДАЧАТА"}]
        _, _, asked = _loop(replies, request="ЗАДАЧАТА", history=history)
        assert len(asked) == rounds + 1
        for sent in asked:
            assert sent[0] == {"role": "system", "content": "SYS"}
            assert any(m.get("content") == "ЗАДАЧАТА" for m in sent)


class TestRestoredHistory:
    """A bounded deque evicts from the FRONT, which is where the system
    message lives (bug fix, 2026-08-12).

    `deque([system_msg] + saved, maxlen=30)` reads as obviously correct and
    behaves correctly for short sessions — then silently drops the system
    message on any restored session with 30+ turns, taking env_facts and the
    workspace briefing with it, so the model spends the rest of the session
    with no instructions. It also disables compaction entirely, since
    Brain.compact_chat_history bails out unless messages[0] is the system
    role, so the history then grows uncompacted up to the hard cap.
    """

    def test_returns_a_bounded_deque(self) -> None:
        out = ac.restored_history([{"role": "user", "content": "hi"}], "sys")
        assert out.maxlen == ac.HISTORY_MAXLEN

    def test_system_message_survives_an_oversized_session(self) -> None:
        saved = [{"role": "user", "content": f"m{i}"} for i in range(100)]
        out = ac.restored_history(saved, "THE SYSTEM PROMPT")
        assert len(out) == ac.HISTORY_MAXLEN
        assert out[0] == {"role": "system", "content": "THE SYSTEM PROMPT"}
        assert out[-1]["content"] == "m99"  # kept the most recent, not the oldest

    def test_current_prompt_replaces_the_saved_one(self) -> None:
        """The system prompt is passed in rather than read from the file on
        purpose: it carries fresh env_facts and briefing for THIS session,
        not whatever was true a week ago."""
        saved = [
            {"role": "system", "content": "STALE PROMPT FROM LAST WEEK"},
            {"role": "user", "content": "hi"},
        ]
        out = ac.restored_history(saved, "FRESH PROMPT")
        assert out[0]["content"] == "FRESH PROMPT"
        assert sum(1 for m in out if m.get("role") == "system") == 1

    def test_short_session_keeps_every_turn(self) -> None:
        saved = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        out = ac.restored_history(saved, "sys")
        assert list(out)[1:] == saved

    def test_empty_session_is_just_the_system_message(self) -> None:
        out = ac.restored_history([], "sys")
        assert list(out) == [{"role": "system", "content": "sys"}]

    def test_custom_maxlen_is_respected(self) -> None:
        saved = [{"role": "user", "content": f"m{i}"} for i in range(20)]
        out = ac.restored_history(saved, "sys", maxlen=5)
        assert len(out) == 5
        assert out[0]["role"] == "system"
        assert out[-1]["content"] == "m19"


def test_bounded_history_keeps_the_system_prompt_and_whole_rounds() -> None:
    msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "go"}]
    for i in range(20):
        msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]},
                 {"role": "tool", "tool_call_id": f"c{i}", "content": str(i)}]
    out = ac.bounded_history(msgs, 8)  # the newest 7 start with a tool result → it goes
    assert out.maxlen == 8 and out[0]["content"] == "SYS"
    assert out[1]["role"] == "assistant" and len(out) == 7
