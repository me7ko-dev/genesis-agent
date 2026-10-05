"""genesis_terminal_agent.run_turn — един ход на агента, клон по клон.

Това е цикълът, през който минават и операторът (`genesis`), и телефонът
(`genesis serve`), и bench-ът. Тестовете го карат с подготвени отговори на
модела (`ask_genesis`) и подготвени резултати на инструментите — без мрежа,
без модел — и пазят какво точно влиза в историята, какво вижда човекът и
кога ходът спира. Пишат се срещу поведението, не срещу устройството на
кода: преструктуриране на цикъла не бива да мърда нито един от тях.
"""
from __future__ import annotations

import copy
import json
from collections import deque
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path

import pytest

import genesis_skills
import genesis_terminal_agent as gta
from genesis_agent.brain import Brain
from genesis_agent.config import TOOL_ROUND_CAP
from genesis_agent.page_check import FinalCheck

SYSTEM = {"role": "system", "content": "системен промпт"}
ASK = genesis_skills.ASK_USER_MARKER


class RecordingUI(gta.TurnUI):
    """Записва всичко, което ходът показва, в реда на показване."""

    def __init__(self, cancelled: bool = False) -> None:
        self.events: list[tuple] = []
        self._cancelled = cancelled

    def thinking(self, label: str, spinner: str = "dots"):
        self.events.append(("thinking", label))
        return nullcontext()

    def assistant(self, text: str) -> None:
        self.events.append(("assistant", text))

    def tool(self, name: str, result: str) -> None:
        self.events.append(("tool", name, result))

    def asked(self, question: str) -> None:
        self.events.append(("asked", question))

    def spinning(self, note: str) -> None:
        self.events.append(("spinning", note))

    def warn(self, text: str) -> None:
        self.events.append(("warn", text))

    def info(self, text: str) -> None:
        self.events.append(("info", text))

    def cancelled(self) -> bool:
        return self._cancelled

    def of(self, kind: str) -> list:
        return [e[1] if len(e) == 2 else e[1:] for e in self.events if e[0] == kind]


def call(name: str, args: dict | None = None, cid: str = "c1", raw: str | None = None) -> dict:
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": raw if raw is not None else json.dumps(args or {})}}


class Turn:
    """Един ход с подготвени отговори. `replies` е опашка от (текст, tool_calls);
    свърши ли, моделът казва „Край.“ без инструменти."""

    def __init__(self, monkeypatch, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.replies: list[tuple[str, list | None]] = []
        self.requests: list[list[dict]] = []
        self.dispatched: list[tuple[str, dict]] = []
        self.parsed: list[str] = []
        self.remembered: list[tuple[str, str]] = []
        self.captured: list[list[dict]] = []
        self.tool_result: Callable[[str, dict], str] = lambda name, args: f"[{name}] ok"
        self.text_results: Callable[[str], list[str]] = lambda text: []
        self.knowledge = ""
        self.compact: Callable[[deque], deque] = lambda messages: messages

        monkeypatch.setattr(gta, "HISTORY_DIR", tmp_path)
        monkeypatch.setattr(gta, "_remember", lambda role, text: self.remembered.append((role, text)))
        monkeypatch.setattr("genesis_agent.skill_loader.domain_context", lambda q: self.knowledge)
        monkeypatch.delenv("GENESIS_ACCEPTANCE", raising=False)
        monkeypatch.setattr(gta, "ask_genesis", self._ask)
        monkeypatch.setattr(genesis_skills, "dispatch_tool_call", self._dispatch)
        monkeypatch.setattr(genesis_skills, "parse_and_execute_tools", self._parse)
        monkeypatch.setattr(Brain, "compact_chat_history",
                            staticmethod(lambda messages, **kw: self.compact(messages)))
        monkeypatch.setattr("genesis_agent.workspace_memory.auto_capture", self._capture)

    def _ask(self, messages, tools=None):
        self.requests.append(copy.deepcopy(list(messages)))
        return self.replies.pop(0) if self.replies else ("Край.", None)

    def _dispatch(self, name, args):
        self.dispatched.append((name, args))
        return self.tool_result(name, args)

    def _parse(self, text):
        self.parsed.append(text)
        return self.text_results(text)

    def _capture(self, messages):
        self.captured.append(list(messages))
        return {"threads": 1, "decisions": 0, "preferences": 0}

    def run(self, text: str = "направи задачата", *, ui: RecordingUI | None = None,
            history: list[dict] | None = None, maxlen: int | None = 30) -> deque:
        self.ui = ui or RecordingUI()
        self.out = gta.run_turn(deque(history or [SYSTEM], maxlen=maxlen), text, self.ui)
        return self.out

    def system_notes(self) -> list[str]:
        return [m["content"] for m in self.out if m["role"] == "system"][1:]


@pytest.fixture
def turn(monkeypatch, tmp_path) -> Turn:
    return Turn(monkeypatch, tmp_path)


# ── Обикновен отговор ────────────────────────────────────────────────────────

class TestAPlainAnswer:
    def test_one_call_one_answer(self, turn) -> None:
        turn.replies = [("Здравей!", None)]
        out = turn.run("здрасти")
        assert [m["role"] for m in out] == ["system", "user", "assistant"]
        assert out[1]["content"] == "здрасти" and out[2]["content"] == "Здравей!"
        assert len(turn.requests) == 1
        assert turn.ui.of("assistant") == ["Здравей!"]

    def test_both_sides_are_remembered(self, turn) -> None:
        turn.replies = [("Здравей!", None)]
        turn.run("здрасти")
        assert turn.remembered == [("user", "здрасти"), ("assistant", "Здравей!")]

    def test_the_session_is_saved_to_the_history_folder(self, turn) -> None:
        turn.replies = [("Здравей!", None)]
        turn.run("здрасти")
        files = list(turn.tmp.glob("session_*.json"))
        assert len(files) == 1
        saved = json.loads(files[0].read_text(encoding="utf-8"))
        assert [m["content"] for m in saved] == ["системен промпт", "здрасти", "Здравей!"]

    def test_the_history_keeps_its_cap(self, turn) -> None:
        out = turn.run(maxlen=7)
        assert isinstance(out, deque) and out.maxlen == 7

    def test_an_uncapped_history_stays_uncapped(self, turn) -> None:
        assert turn.run(maxlen=None).maxlen is None

    def test_an_empty_reply_ends_the_turn_instead_of_crashing_it(self, turn) -> None:
        """Празен отговор без инструменти (Brain го връща, когато доставчикът
        отговори с празно съдържание) пускаше `len(None)` при записа в паметта —
        TypeError, и целият ход се губеше."""
        turn.replies = [("", None)]
        out = turn.run("здрасти")
        assert out[-1] == {"role": "assistant", "content": ""}
        assert turn.remembered == [("user", "здрасти")], "празното не се помни"

    def test_the_callers_deque_is_not_mutated(self, turn) -> None:
        history = deque([SYSTEM], maxlen=30)
        gta.run_turn(history, "задача", RecordingUI())
        assert list(history) == [SYSTEM]


class TestVerifiedKnowledge:
    def test_it_goes_to_the_model_with_the_request(self, turn) -> None:
        turn.knowledge = "Проверено знание: ЕГН\nконтролна цифра по модул 11"
        turn.run("провери ЕГН")
        assert turn.requests[0][-1]["content"] == f"провери ЕГН\n\n{turn.knowledge}"
        assert turn.ui.of("info") == ["📚 проверено знание: ЕГН"]

    def test_memory_keeps_only_what_the_operator_typed(self, turn) -> None:
        turn.knowledge = "Проверено знание: ЕГН\nдълъг текст"
        turn.run("провери ЕГН")
        assert turn.remembered[0] == ("user", "провери ЕГН")

    def test_a_broken_knowledge_lookup_does_not_stop_the_turn(self, turn, monkeypatch) -> None:
        def _boom(q):
            raise RuntimeError("нарочно")
        monkeypatch.setattr("genesis_agent.skill_loader.domain_context", _boom)
        turn.replies = [("Добре.", None)]
        assert turn.run("провери ЕГН")[-1]["content"] == "Добре."


# ── Native tool calls ────────────────────────────────────────────────────────

class TestNativeToolCalls:
    def test_a_call_is_dispatched_and_its_result_goes_back(self, turn) -> None:
        turn.replies = [("", [call("LIST_DIR", {"path": "."}, cid="x9")]), ("Има два файла.", None)]
        turn.tool_result = lambda name, args: "a.py\nb.py"
        out = turn.run()
        assert turn.dispatched == [("LIST_DIR", {"path": "."})]
        tool_msg = next(m for m in out if m["role"] == "tool")
        assert tool_msg == {"role": "tool", "tool_call_id": "x9", "name": "LIST_DIR",
                            "content": "a.py\nb.py"}
        assert turn.requests[1][-1] == tool_msg
        assert out[-1] == {"role": "assistant", "content": "Има два файла."}

    def test_the_assistant_message_carries_its_calls(self, turn) -> None:
        c = call("LIST_DIR", {"path": "."})
        turn.replies = [("гледам", [c]), ("Готово.", None)]
        out = turn.run()
        assert out[2] == {"role": "assistant", "content": "гледам", "tool_calls": [c]}

    def test_the_operator_sees_every_result(self, turn) -> None:
        turn.replies = [("", [call("LIST_DIR", {"path": "."})])]
        turn.tool_result = lambda name, args: "a.py"
        turn.run()
        assert turn.ui.of("tool") == [("LIST_DIR", "a.py")]

    def test_broken_json_arguments_become_no_arguments(self, turn) -> None:
        turn.replies = [("", [call("LIST_DIR", raw="{не е json")])]
        turn.run()
        assert turn.dispatched == [("LIST_DIR", {})]

    def test_a_call_without_any_result_is_remembered_by_count(self, turn) -> None:
        turn.replies = [("", [call("LIST_DIR"), call("READ_FILE", cid="c2")])]
        turn.run()
        assert ("assistant", "[повикани 2 tool(-а)]") in turn.remembered

    def test_a_huge_result_is_clipped_for_the_model_not_for_the_operator(self, turn) -> None:
        big = "ред\n" * 20_000
        turn.replies = [("", [call("READ_FILE", {"path": "x"})])]
        turn.tool_result = lambda name, args: big
        out = turn.run()
        tool_msg = next(m for m in out if m["role"] == "tool")
        assert len(tool_msg["content"]) < len(big)
        assert turn.ui.of("tool") == [("READ_FILE", big)]


class TestTheTurnStops:
    def test_when_the_agent_asks_the_operator(self, turn) -> None:
        turn.replies = [("", [call("ASK_USER", {"question": "кой порт?"})])]
        turn.tool_result = lambda name, args: f"{ASK}Кой порт да ползвам?"
        out = turn.run()
        assert turn.ui.of("asked") == ["Кой порт да ползвам?"]
        assert len(turn.requests) == 1, "след въпрос моделът не се пита пак"
        assert out[-1]["role"] == "tool"

    def test_the_question_is_shown_once_without_the_internal_marker(self, turn) -> None:
        """Беше показван два пъти: суровият резултат с `__GENESIS_ASK_USER__`
        в панел 🔧, после самият въпрос."""
        turn.replies = [("", [call("LIST_DIR"), call("ASK_USER", {"question": "кой порт?"}, cid="c2")])]
        turn.tool_result = lambda name, args: f"{ASK}❓ Кой порт?" if name == "ASK_USER" else "a.py"
        turn.run()
        assert turn.ui.of("tool") == [("LIST_DIR", "a.py")]
        assert turn.ui.of("asked") == ["❓ Кой порт?"]

    def test_when_the_same_call_returns_the_same_thing_three_times(self, turn) -> None:
        same = ("", [call("RUN_CMD", {"command": "curl localhost"})])
        turn.replies = [same, same, same, same]
        turn.tool_result = lambda name, args: "connection refused"
        out = turn.run()
        assert len(turn.dispatched) == 3
        assert len(turn.ui.of("spinning")) == 1
        notes = [m["content"] for m in out if m["role"] == "system"][1:]
        assert len(notes) == 1 and "2-рото извикване" in notes[0], "предупреждение на втория път"

    def test_at_the_round_cap(self, turn) -> None:
        turn.replies = [("", [call("RUN_CMD", {"command": f"echo {i}"})])
                        for i in range(TOOL_ROUND_CAP + 5)]
        turn.tool_result = lambda name, args: args["command"]
        turn.run()
        assert len(turn.dispatched) == TOOL_ROUND_CAP
        assert len(turn.requests) == TOOL_ROUND_CAP
        assert any(f"таван от {TOOL_ROUND_CAP}" in w for w in turn.ui.of("warn"))

    def test_when_the_operator_presses_stop(self, turn) -> None:
        turn.replies = [("започвам", [call("RUN_CMD", {"command": "ls"})])]
        out = turn.run(ui=RecordingUI(cancelled=True))
        assert turn.dispatched == []
        assert out[-1] == {"role": "assistant", "content": "започвам"}, (
            "незапочнатите извиквания се махат — иначе историята е невалидна")
        assert any("Спряно" in w for w in turn.ui.of("warn"))


# ── Текстови тагове (модели без native tool calling) ─────────────────────────

class TestTextTags:
    def test_results_go_back_as_one_system_message(self, turn) -> None:
        turn.replies = [("[LIST_DIR: .]", None), ("Два файла.", None)]
        turn.text_results = lambda text: ["[LIST_DIR: .]\na.py\nb.py"] if "[LIST_DIR" in text else []
        out = turn.run()
        result_msg = turn.requests[1][-1]
        assert result_msg["role"] == "system"
        assert result_msg["content"].startswith("[Резултат]:\n[LIST_DIR: .]\na.py\nb.py\n\n")
        assert out[-1]["content"] == "Два файла."

    def test_asking_the_operator_stops_the_turn(self, turn) -> None:
        turn.replies = [("[ASK_USER: кой порт?]", None)]
        turn.text_results = lambda text: [f"{ASK}Кой порт?"] if "ASK_USER" in text else []
        turn.run()
        assert turn.ui.of("asked") == ["Кой порт?"]
        assert len(turn.requests) == 1

    def test_the_operator_sees_every_result_here_too(self, turn) -> None:
        """Моделите без native tool calling са по-слабите — точно там човекът
        трябва да вижда какво реално е изпълнено, не само тага в отговора."""
        turn.replies = [("[LIST_DIR: .]\n[READ_FILE: a.py]", None), ("Готово.", None)]
        turn.text_results = lambda text: (["[LIST_DIR: .]\na.py", "[READ_FILE: a.py]\nprint(1)"]
                                          if "[LIST_DIR" in text else [])
        turn.run()
        assert turn.ui.of("tool") == [("LIST_DIR", "[LIST_DIR: .]\na.py"),
                                      ("READ_FILE", "[READ_FILE: a.py]\nprint(1)")]

    def test_a_result_without_a_tag_name_is_still_shown(self, turn) -> None:
        turn.replies = [("[RUN_CMD: x]", None)]
        turn.text_results = lambda text: ["[Грешка: genesis_skills не е зареден]"] if "RUN" in text else []
        turn.run()
        assert turn.ui.of("tool")[0] == ("инструмент", "[Грешка: genesis_skills не е зареден]")

    def test_a_question_is_shown_once_without_the_internal_marker(self, turn) -> None:
        turn.replies = [("[ASK_USER: кой порт?]", None)]
        turn.text_results = lambda text: [f"{ASK}❓ Кой порт?"] if "ASK_USER" in text else []
        turn.run()
        assert turn.ui.of("tool") == []
        assert turn.ui.of("asked") == ["❓ Кой порт?"]

    def test_spinning_stops_the_turn(self, turn) -> None:
        turn.replies = [("[RUN_CMD: curl x]", None)] * 4
        turn.text_results = lambda text: ["[RUN_CMD: curl x]\nrefused"] if "RUN_CMD" in text else []
        turn.run()
        assert len(turn.ui.of("spinning")) == 1
        assert len(turn.requests) == 3

    def test_the_round_cap_is_announced_here_too(self, turn) -> None:
        turn.replies = [(f"[RUN_CMD: echo {i}]", None) for i in range(TOOL_ROUND_CAP + 5)]
        turn.text_results = lambda text: [text + "\nok"] if text.startswith("[RUN_CMD") else []
        turn.run()
        assert len(turn.requests) == TOOL_ROUND_CAP
        assert any(f"таван от {TOOL_ROUND_CAP}" in w for w in turn.ui.of("warn"))

    def test_a_malformed_tag_gets_two_retries_then_counts_as_the_answer(self, turn) -> None:
        turn.replies = [("[INSTALL: requests]", None)] * 3
        out = turn.run()
        assert len(turn.requests) == 3
        nudges = [n for n in turn.system_notes() if "не намерих валиден tool" in n]
        assert len(nudges) == 2
        assert out[-1]["content"] == "[INSTALL: requests]"


# ── Проверките, когато моделът каже „готово“ ─────────────────────────────────

class TestChecksAtTheEnd:
    def test_written_python_that_never_ran_is_sent_back(self, turn) -> None:
        report = turn.tmp / "report.py"
        report.write_text('print("ОБЩО", 12)\n', encoding="utf-8")
        turn.replies = [("", [call("WRITE_FILE", {"path": str(report)})]), ("Готово.", None)]
        turn.tool_result = lambda name, args: f"[WRITE_FILE: {report}] ✓ записани 19 символа"
        turn.run("report.py печата ред ОБЩО")
        assert any(n.startswith("[проверка на кода] report.py") for n in turn.system_notes())
        assert any("Написа код" in w for w in turn.ui.of("warn"))
        assert len(turn.requests) == 3

    def test_the_code_check_gets_the_request_without_the_knowledge(self, turn) -> None:
        """Буквалните думи се търсят в заявката на оператора, не в знанието."""
        report = turn.tmp / "report.py"
        report.write_text('print("TOTAL", 12)\n', encoding="utf-8")
        turn.knowledge = "Проверено знание: X\nтам пише „ДРУГО“"
        turn.replies = [("", [call("WRITE_FILE", {"path": str(report)})]), ("Готово.", None)]
        turn.tool_result = lambda name, args: f"[WRITE_FILE: {report}] ✓ записани 19 символа"
        turn.run("report.py печата ред ОБЩО")
        note = next(n for n in turn.system_notes() if n.startswith("[проверка на кода]"))
        assert "„ОБЩО“" in note and "„ДРУГО“" not in note

    def test_the_browser_finds_something(self, turn, monkeypatch) -> None:
        monkeypatch.setattr(FinalCheck, "due", lambda self: not getattr(self, "_t", False))

        def _check(self):
            self._t = True
            return "[проверка в браузър] нечетим текст", "1 находка"
        monkeypatch.setattr(FinalCheck, "check", _check)
        turn.replies = [("Сайтът е готов.", None), ("Оправих го.", None)]
        out = turn.run()
        assert ("проверка в браузър", "1 находка") in turn.ui.of("tool")
        assert "[проверка в браузър] нечетим текст" in turn.system_notes()
        assert out[-1]["content"] == "Оправих го."

    def test_a_clean_browser_check_backs_a_browser_claim(self, turn, monkeypatch) -> None:
        monkeypatch.setattr(FinalCheck, "due", lambda self: not getattr(self, "_t", False))

        def _check(self):
            self._t, self.passed = True, True
            return "", "чисто"
        monkeypatch.setattr(FinalCheck, "check", _check)
        turn.replies = [("Проверих го в браузъра, няма грешки.", None)]
        turn.run()
        assert len(turn.requests) == 1, "проверката в браузър доказва твърдението"

    def test_a_promise_without_the_work_is_sent_back_once(self, turn) -> None:
        turn.replies = [("Разгледах. Сега ще създам файла.", None),
                        ("Сега ще създам файла.", None)]
        turn.run()
        assert len(turn.requests) == 2
        assert sum("ходът свърши без това" in n for n in turn.system_notes()) == 1

    def test_a_claim_no_tool_backs_is_challenged_once(self, turn) -> None:
        turn.replies = [("Инсталирах пакета.", None), ("Инсталирах пакета.", None)]
        turn.run()
        assert len(turn.requests) == 2
        assert sum("Твърдиш неща" in n for n in turn.system_notes()) == 1

    def test_a_claim_backed_by_the_command_passes(self, turn) -> None:
        turn.replies = [("", [call("RUN_CMD", {"command": "pip install requests"})]),
                        ("Инсталирах пакета.", None)]
        turn.tool_result = lambda name, args: "Successfully installed requests"
        turn.run()
        assert len(turn.requests) == 2
        assert not any("Твърдиш неща" in n for n in turn.system_notes())

    def test_a_text_tag_command_backs_a_claim_too(self, turn) -> None:
        turn.replies = [("[RUN_CMD: pip install requests]", None), ("Инсталирах пакета.", None)]
        turn.text_results = lambda text: ([f"{text}\nSuccessfully installed"]
                                          if text.startswith("[RUN_CMD") else [])
        turn.run()
        assert not any("Твърдиш неща" in n for n in turn.system_notes())

    def test_a_blocked_command_backs_nothing(self, turn) -> None:
        turn.replies = [("", [call("RUN_CMD", {"command": "pip install requests"})]),
                        ("Инсталирах пакета.", None)]
        turn.tool_result = lambda name, args: "[SANDBOX BLOCKED] отказано"
        turn.run()
        assert any("Твърдиш неща" in n for n in turn.system_notes())


# ── История ──────────────────────────────────────────────────────────────────

class TestHistory:
    def test_every_request_has_the_system_prompt_and_the_task(self, turn) -> None:
        turn.replies = [("", [call("RUN_CMD", {"command": f"echo {i}"})]) for i in range(20)]
        turn.tool_result = lambda name, args: args["command"]
        turn.run("моята задача", maxlen=10)
        for request in turn.requests:
            assert request[0] == SYSTEM
            assert any(m["role"] == "user" and m["content"] == "моята задача" for m in request)

    def test_compaction_saves_what_it_drops(self, turn) -> None:
        turn.compact = lambda messages: deque(list(messages)[:1] + list(messages)[-2:])
        turn.replies = [("Здравей!", None)]
        out = turn.run("здрасти", history=[SYSTEM, {"role": "user", "content": "старо"},
                                           {"role": "assistant", "content": "стар отговор"}])
        assert len(out) == 3
        assert [m["content"] for m in turn.captured[0]] == ["старо", "стар отговор", "здрасти", "Здравей!"]
        infos = turn.ui.of("info")
        assert any("компресирана (5 → 3" in i for i in infos)
        assert any("Запомнено преди компресията: 1т/0р/0п" in i for i in infos)

    def test_no_compaction_no_capture(self, turn) -> None:
        turn.run()
        assert turn.captured == []

    def test_a_failing_capture_does_not_lose_the_turn(self, turn, monkeypatch) -> None:
        def _boom(messages):
            raise RuntimeError("базата е заключена")
        monkeypatch.setattr("genesis_agent.workspace_memory.auto_capture", _boom)
        turn.compact = lambda messages: deque(list(messages)[:1] + list(messages)[-2:])
        turn.replies = [("Здравей!", None)]
        out = turn.run("здрасти", history=[SYSTEM, {"role": "user", "content": "старо"},
                                           {"role": "assistant", "content": "стар отговор"}])
        assert out[-1]["content"] == "Здравей!"
        assert not any("Запомнено" in i for i in turn.ui.of("info"))
        assert list(turn.tmp.glob("session_*.json")), "сесията се записва въпреки това"

    def test_a_long_turn_returns_under_the_cap_with_the_system_prompt(self, turn) -> None:
        turn.replies = [("", [call("RUN_CMD", {"command": f"echo {i}"})]) for i in range(12)]
        turn.tool_result = lambda name, args: args["command"]
        out = turn.run(maxlen=10)
        assert len(out) <= 10 and out[0] == SYSTEM
        assert out[1]["role"] != "tool", "резултат без извикването си е невалидна история"
