"""Одит на цикъла на агента 2026-10-22: инструменти извън MISSION_TOOLS в
мисия, червен изход на пускач с rc=0, честите форми на „тестовете минават“
и успешен запис между две еднакви пускания в таг-режим.
Всеки тест пада без поправката си (пуснато срещу стария код)."""
from __future__ import annotations

import json
from typing import ClassVar

import pytest

from genesis_agent import autonomous_loop as al
from genesis_agent import claim_check
from genesis_agent.brain import Brain as RealBrain
from genesis_agent.repeat_guard import RepeatGuard
from genesis_agent.storage_monitor import StorageReport

# ── 1. Мисия не изпълнява инструмент, който не ѝ е предложен ────────────────


class _Reply:
    def __init__(self, raw_text: str = "", code: str | None = None, tool_calls=None) -> None:
        self.raw_text = raw_text
        self.code = code
        self.tool_calls = tool_calls
        self.usage = None


class _MissionBrain:
    """Опашка с отговори, както FakeBrain в tests/test_autonomous_loop.py."""

    trim_round_history = staticmethod(RealBrain.trim_round_history)
    replies: ClassVar[list[_Reply]] = []
    offered: ClassVar[list[list[str]]] = []
    calls: ClassVar[list[list[dict]]] = []
    local = None
    chain: ClassVar[list[dict]] = [{"provider": "fake", "model": "fake"}]
    current = None

    def __init__(self, *a, **kw) -> None:
        pass

    def route_for_goal(self, goal: str) -> None:
        pass

    def build_context(self, goal: str) -> str:
        return ""

    def system_prompt_base(self) -> str:
        return "system prompt"

    def escalate(self) -> bool:
        return True

    def generate_local_candidates(self, messages, n=2):
        return []

    def complete(self, messages, tools=None, avoid=None):
        assert _MissionBrain.replies, "complete извикан повече пъти от опашката"
        _MissionBrain.offered.append([t["function"]["name"] for t in (tools or [])])
        _MissionBrain.calls.append(list(messages))
        return _MissionBrain.replies.pop(0)


@pytest.fixture
def mission(monkeypatch, tmp_path):
    _MissionBrain.replies = []
    _MissionBrain.offered = []
    _MissionBrain.calls = []
    monkeypatch.setattr(al, "Brain", _MissionBrain)
    monkeypatch.setattr(
        al, "check_storage",
        lambda: StorageReport(total_bytes=0, threshold_bytes=1, compression_required=False,
                              log_path=None, candidates_path=None))
    monkeypatch.setattr("genesis_agent.telemetry.STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr("genesis_agent.reflection.lessons_for_prompt", lambda *a, **kw: "")
    dispatched: list[str] = []
    monkeypatch.setattr("genesis_skills.dispatch_tool_call",
                        lambda name, args: dispatched.append(name) or f"[{name}] изпълнено")
    return dispatched


def _tc(name: str, args: dict, cid: str) -> list[dict]:
    return [{"id": cid, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]


def test_a_mission_never_runs_a_tool_it_was_not_offered(mission) -> None:
    """Native клонът подаваше всяко име от модела на dispatch_tool_call:
    мисия (DELEGATE във фонова нишка, без никой да гледа) с предложени само
    read-only инструменти + USE_SKILL записваше файл и пускаше shell команда
    (измерено 2026-10-22: new_module.py презаписан, marker.txt създаден)."""
    _MissionBrain.replies = [
        _Reply(tool_calls=_tc("WRITE_FILE", {"path": "new_module.py", "content": "PRICE = 0\n"}, "c1")),
        _Reply(tool_calls=_tc("RUN_CMD", {"command": "echo pwned > marker.txt"}, "c2")),
        _Reply(tool_calls=_tc("READ_FILE", {"path": "app.py"}, "c3")),
        _Reply(raw_text="Error: stop"),
    ]

    al.run_autonomous_loop("изчисли сбора на 2 и 3", max_rounds=4)

    assert "RUN_CMD" not in _MissionBrain.offered[0]
    assert "WRITE_FILE" not in _MissionBrain.offered[0]
    # Предложеното още се изпълнява; непредложеното — не.
    assert mission == ["READ_FILE"]
    tool_msgs = [m for m in _MissionBrain.calls[-1] if m.get("role") == "tool"]
    refusals = {m["tool_call_id"]: m["content"] for m in _MissionBrain.calls[2]
                if m.get("role") == "tool"}
    assert "c2" in refusals and "не е достъпен в мисия" in refusals["c2"]
    assert any(m["tool_call_id"] == "c3" and "изпълнено" in m["content"] for m in tool_msgs)


def test_an_orchestrated_mission_never_runs_a_tool_it_was_not_offered(monkeypatch, tmp_path) -> None:
    """Ревю 2026-10-22: orchestrator.py (Planner/Coder/Tester, `benchmark.py`)
    има същия native клон — предлага MISSION_TOOLS и подаваше всяко върнато име
    на dispatch_tool_call. Без проверката тук WRITE_FILE и RUN_CMD се изпълняваха."""
    from types import SimpleNamespace

    from genesis_agent import orchestrator as orch

    def reply(raw_text: str = "", code: str = "", tool_calls=None) -> SimpleNamespace:
        return SimpleNamespace(raw_text=raw_text, code=code, tool_calls=tool_calls)

    replies = [
        reply(raw_text="1. събери 2 и 3"),                                       # planner
        reply(tool_calls=_tc("WRITE_FILE", {"path": "new_module.py", "content": "X = 0\n"}, "c1")
              + _tc("RUN_CMD", {"command": "echo pwned > marker.txt"}, "c2")
              + _tc("READ_FILE", {"path": "app.py"}, "c3")),
        reply(code="assert 2 + 3 == 5\nprint('OK')"),
    ]
    calls: list[list[dict]] = []

    class _Brain:
        trim_round_history = staticmethod(RealBrain.trim_round_history)

        def __init__(self, **kw) -> None:
            pass

        def route_for_goal(self, goal: str) -> None:
            pass

        def build_context(self, goal: str) -> str:
            return ""

        def system_prompt_base(self) -> str:
            return "sys"

        def complete(self, messages, tools=None):
            calls.append(list(messages))
            return replies.pop(0)

    monkeypatch.setattr(orch, "Brain", _Brain)
    monkeypatch.delenv("GENESIS_TDD", raising=False)
    monkeypatch.setattr(orch.dna, "validate_goal_ethics", lambda goal: None)
    monkeypatch.setattr(orch.dna, "assert_operator_if_strict", lambda operator_id: None)
    monkeypatch.setattr("genesis_agent.reflection.lessons_for_prompt", lambda *a, **kw: "")
    monkeypatch.setattr(orch, "SKILLS_ROOT", tmp_path)
    monkeypatch.setattr(orch, "run_python_subprocess",
                        lambda code: SimpleNamespace(ok=True, stdout="OK", stderr="", returncode=0))
    monkeypatch.setattr(orch, "verify_skill",
                        lambda code: SimpleNamespace(verified=True, method="self_test_passed", detail=""))
    monkeypatch.setattr(orch, "save_skill", lambda **kw: tmp_path / "sum.md")
    dispatched: list[str] = []
    monkeypatch.setattr("genesis_skills.dispatch_tool_call",
                        lambda name, args: dispatched.append(name) or f"[{name}] изпълнено")

    out = orch.run_orchestrated("изчисли сбора на 2 и 3", max_rounds=3)

    assert out.success
    assert dispatched == ["READ_FILE"]
    tool_msgs = {m["tool_call_id"]: m["content"] for m in calls[-1] if m.get("role") == "tool"}
    assert "не е достъпен в мисия" in tool_msgs["c1"] and "не е достъпен в мисия" in tool_msgs["c2"]
    assert "изпълнено" in tool_msgs["c3"]


# ── 2. Червен итог на пускача с rc=0 не е зелено пускане ────────────────────

def _run(cmd: str, rc: object, out: str = "") -> tuple[str, str] | None:
    return claim_check.counts_as_executed("RUN_CMD", cmd, f"[RUN_CMD: {cmd}]  (rc={rc})\n{out}")


_UNITTEST_EXIT_FALSE = (
    ".F\n" + "=" * 70 + "\nFAIL: test_vat_round (__main__.T.test_vat_round)\n" + "-" * 70 +
    "\nTraceback (most recent call last):\n  File \"test_app.py\", line 6, in test_vat_round\n"
    "AssertionError: 2.11 != 2.1100000000000003\n\n" + "-" * 70 +
    "\nRan 2 tests in 0.001s\n\nFAILED (failures=1)\n")
_NODE_TEST_OR_TRUE = (
    "> x@1.0.0 test\n> node --test || true\n\nTAP version 13\n# Subtest: vat\nnot ok 1 - vat\n"
    "  ---\n  code: 'ERR_ASSERTION'\n  ...\n1..1\n# tests 1\n# suites 0\n# pass 0\n# fail 1\n")


@pytest.mark.parametrize(("cmd", "out"), [
    ("python test_app.py", _UNITTEST_EXIT_FALSE),
    ("npm test", _NODE_TEST_OR_TRUE),
    ("npm test", "> jest || true\nTests:       1 failed, 4 passed, 5 total"),
    ("npm test", "ℹ tests 5\nℹ pass 4\nℹ fail 1"),
    ("python -m unittest", "FAILED (failures=1)"),
    ("pytest -q", ".F...\n1 failed, 4 passed in 0.12s"),
    ("cargo test", "test result: FAILED. 3 passed; 1 failed; 0 ignored"),
    ("npx mocha", "  4 passing (12ms)\n  1 failing\n"),
])
def test_a_red_summary_with_rc_zero_is_a_red_run(cmd, out) -> None:
    """rc на последния пускач във веригата се вземаше на доверие и изходът не
    се гледаше: `python test_app.py` с unittest.main(exit=False) и `npm test`
    със `node --test || true` (rc=0, червен итог) доказваха „Всички тестове
    минават“ и `genesis -p` излизаше с 0 (измерено 2026-10-22). Същият изход
    зад `| tail` вече беше червен."""
    entry = _run(cmd, 0, out)
    assert entry == ("RUN_CMD", f"{cmd} (rc=1)")
    claims = claim_check.unsupported_claims("Готово! Всички тестове минават.", [entry])
    assert [c.kind for c in claims] == ["минаващи тестове"]
    assert claim_check.green_claim_after_red(claims, [entry]) == 1


@pytest.mark.parametrize(("cmd", "out"), [
    ("pytest -q", "WARNING FAILED to connect, retrying\n3 passed, 1 xfailed\nXFAIL test_x"),
    ("pytest -q", ("tests/test_log.py::test_logs_error PASSED\nTraceback (most recent call last):\n"
                   "ValueError: 3 errors in input\n5 passed in 0.12s")),
    ("python -m unittest", "Ran 4 tests in 0.01s\n\nOK"),
    ("npm test", "# tests 35\n# pass 35\n# fail 0"),
    # Броячът на собствен скрипт (ревю 2026-10-22): „FAIL: 0“ не е червен итог.
    ("./run_tests.sh", "PASS: 5\nFAIL: 0\n"),
    ("python run_tests.py", "PASS=5\nFAIL=0\n"),
])
def test_a_green_run_with_red_words_in_its_log_stays_green(cmd, out) -> None:
    """Обратната страна: доверен rc=0 с „FAILED“/„3 errors“ в лога, но зелен
    итог, остава зелен — червени са само итоговите редове на пускача."""
    assert _run(cmd, 0, out) == ("RUN_CMD", cmd)


# ── 3. Честите форми на „тестовете минават“ след червено пускане ────────────

_RED = [("RUN_CMD", "python -m pytest -q (rc=1)")]


@pytest.mark.parametrize("text", [
    "Done — the tests pass.", "Done — tests pass.", "The tests pass.", "Tests pass now.",
    "All tests are passing.", "I fixed the bug and the tests pass.", "Tests are now green.",
    "I just fixed it and the tests pass.", "I only changed one line and the tests pass.",
    "Тестовете са зелени.", "Всички тестове са зелени.", "Тестовете вече са зелени.",
    "✅ 2/2 теста минават.", "Готово: 5/5 теста минаха.", "2/2 tests pass.",
    "All tests passing."])
def test_common_green_wordings_are_caught_after_a_red_run(text) -> None:
    """Шаблонът хващаше само „тестовете минават“, „всички … тестове“ и „all
    tests pass“: след pytest rc=1 „Done — the tests pass.“ даваше exit 0 без
    подкана (измерено 2026-10-22, e2e), докато „Всички тестове минават.“ — 1."""
    claims = claim_check.unsupported_claims(text, _RED)
    assert [c.kind for c in claims] == ["минаващи тестове"]
    assert claim_check.green_claim_after_red(claims, _RED) == 1


@pytest.mark.parametrize("text", [
    "3 of 5 tests pass.", "4 tests pass, 1 fails.", "Some of the tests pass.",
    "Only the unit tests pass.", "Make sure the tests pass.", "Do the tests pass?",
    "If the tests pass, merge it.", "This should make the tests pass.",
    "Run pytest to confirm the tests pass.", "1/2 теста минават.", "3/5 tests pass.",
    "Тестовете са зелени, освен test_vat.", "Не всички тестове са зелени.",
    "Тестът минава.", "The test passes now.",
    # Ревю 2026-10-22: цел, условие във времето, заповед, отрицание, сегашно
    # причастие — с голото „tests pass“ всяко от тези ставаше зелено твърдение.
    "Once the tests pass, merge it.", "After the tests pass, deploy it.",
    "I changed the rounding to make the tests pass.", "So that the tests pass, I mocked the clock.",
    "Make the tests pass by mocking the clock.", "Hopefully the tests pass now.",
    "No tests pass yet.", "Keep the tests passing.",
    "The tests passing locally doesn't mean CI is green."])
def test_counted_reports_and_hedges_are_still_not_green_claims(text) -> None:
    """Броените и смесените отчети, въпросите и условията остават извън
    шаблона (виж tests/test_e2e_wave2_2026_10_11.py) — и единственото число:
    „Тестът минава.“ е честен отчет за един тест, не за пакета."""
    claims = claim_check.unsupported_claims(text, _RED)
    assert claim_check.green_claim_after_red(claims, _RED) == 0


@pytest.mark.parametrize("text", [
    "I fixed the bug that would crash on empty input and all tests pass now.",
    "I fixed the parser which will now accept empty input, and all tests pass.",
    "Поправих функцията, която би гърмяла при празен вход, и всички тестове минават.",
    "I will summarize: I fixed X and all tests pass."])
def test_a_modal_far_from_the_claim_does_not_hide_it(text) -> None:
    """Ревю 2026-10-22: първата поправка сложи should/would/will/би в
    _NOT_A_CLAIM_RE — навсякъде в изречението. Тези твърдения на main се
    хващаха след червено; с поправката минаваха без подкана и с exit 0.
    Предположението отменя твърдението само точно пред него (_HEDGED_RE)."""
    claims = claim_check.unsupported_claims(text, _RED)
    assert [c.kind for c in claims] == ["минаващи тестове"]
    assert claim_check.green_claim_after_red(claims, _RED) == 1


def test_a_counted_report_with_nothing_run_still_needs_a_run() -> None:
    """Броеният отчет е смесен, не празен: твърди пускане и без пускане
    иска такова (както „4 tests passed, 1 failed.“ във вълна 5)."""
    nothing = [("RUN_CMD", "ls")]
    for text in ("3 of 5 tests pass.", "4 tests passed.", "Only the unit tests pass."):
        assert [c.kind for c in claim_check.unsupported_claims(text, nothing)] \
            == ["пускане на тестове"], text


# ── 4. Успешен запис между две еднакви пускания не е въртене ────────────────

def test_a_successful_edit_between_identical_test_runs_is_progress() -> None:
    """Таг-режим (локални и безплатни модели): RUN_CMD → EDIT ✓ → RUN_CMD →
    друг EDIT ✓ → RUN_CMD със същия червен изход ставаше „RUN_CMD 3 пъти
    подред“ — ходът спираше и третата поправка не се пускаше (измерено
    2026-10-22, headless). Native пътят не спира: аргументите на EDIT се
    различават и поредицата се нулира."""
    run = ("[RUN_CMD: python test_app.py]  (rc=1)\nF\nFAIL: test_total (__main__.T.test_total)\n"
           "AssertionError: 3 != 4\n\nFAILED (failures=1)")
    edits = [
        ("[EDIT_FILE: /ws/app.py] ✓ /ws/app.py — 1 замяна(и), +4/-1 реда\n-    return sum(items)\n"
         "+    s = 0\n+    for i in items:\n+        s += i\n+    return s"),
        ("[EDIT_FILE: /ws/app.py] ✓ /ws/app.py — 1 замяна(и), +1/-4 реда\n-    s = 0\n"
         "+    return sum(list(items))"),
        "[WRITE_FILE: /ws/app.py] ✓ записани 48 символа",
    ]
    g = RepeatGuard()
    verdicts = [g.observe_text_result(run)]
    for edit in edits:
        assert not g.observe_text_result(edit).stop
        verdicts.append(g.observe_text_result(run))
    assert not any(v.stop for v in verdicts), [v.note[:60] for v in verdicts]
    # Нито „N пъти подред“: между пусканията има промяна. (Бележката за кръг
    # RUN_CMD ↔ EDIT_FILE след първата редакция е същата като на native пътя.)
    assert not any("подред" in v.note for v in verdicts), [v.note[:60] for v in verdicts]
    # Без запис между тях три еднакви пускания подред още спират хода.
    g = RepeatGuard()
    assert [g.observe_text_result(run).stop for _ in range(3)] == [False, False, True]


# ── E2E през `genesis -p` с фалшив модел (без ключове, без истински shell) ──

@pytest.fixture
def ws(tmp_path, monkeypatch):
    import genesis_skills as gs
    import genesis_terminal_agent as gta
    from genesis_agent import headless, hooks
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(gs, "_WORKSPACE", root)
    monkeypatch.setattr(gta, "WORKSPACE", root)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    monkeypatch.setattr(gta, "build_system_prompt", lambda: ("SYS", ""))
    monkeypatch.setattr(headless, "_read_stdin", lambda: "")
    gs._SEEN_PATHS.clear()
    hooks.workspace = root
    yield root
    hooks.workspace = None
    gs._set_agent_scope(None)
    gs._set_cancel(None)


def _call(name: str, args: dict, cid: str) -> dict:
    return _tc(name, args, cid)[0]


def _script(monkeypatch, replies) -> list:
    import genesis_terminal_agent as gta
    it = iter(replies)
    seen: list = []

    def fake(messages, tools=None):
        seen.append([dict(m) for m in messages])
        return next(it)
    monkeypatch.setattr(gta, "ask_genesis", fake)
    return seen


def _headless(ws, capsys) -> tuple[int, dict]:
    from genesis_agent import headless
    code = headless.run(["--json", "--cwd", str(ws), "оправи тестовете"])
    return code, json.loads(capsys.readouterr().out)


def _fake_run_cmd(monkeypatch, outputs) -> None:
    """RUN_CMD и по двата пътя (native и таг) връща подадените резултати."""
    import genesis_skills as gs
    it = iter(outputs)
    monkeypatch.setattr(gs, "_tool_run_cmd", lambda arg: next(it))


@pytest.mark.parametrize(("cmd", "out"), [
    ("python test_app.py", _UNITTEST_EXIT_FALSE),
    ("npm test", _NODE_TEST_OR_TRUE),
])
def test_headless_fails_on_a_green_claim_after_a_red_rc_zero_run(ws, monkeypatch, capsys,
                                                                cmd, out) -> None:
    _fake_run_cmd(monkeypatch, [f"[RUN_CMD: {cmd}]  (rc=0)\n{out}"])
    claim = "Готово! Всички тестове минават."
    _script(monkeypatch, [("", [_call("RUN_CMD", {"command": cmd}, "t1")]),
                          (claim, None), (claim, None), (claim, None)])
    code, data = _headless(ws, capsys)
    assert code == 1 and data["ok"] is False
    assert "червено" in (data["error"] or "")


def test_headless_fails_on_a_plain_green_wording_after_a_red_run(ws, monkeypatch, capsys) -> None:
    red = "[RUN_CMD: python -m pytest -q]  (rc=1)\n.F\n1 failed, 1 passed in 0.02s"
    _fake_run_cmd(monkeypatch, [red])
    claim = "Done — the tests pass."
    _script(monkeypatch, [("", [_call("RUN_CMD", {"command": "python -m pytest -q"}, "t1")]),
                          (claim, None), (claim, None), (claim, None)])
    code, data = _headless(ws, capsys)
    assert code == 1 and data["ok"] is False
    assert "червено" in (data["error"] or "")


def test_headless_text_mode_lets_the_third_fix_run(ws, monkeypatch, capsys) -> None:
    """Таг-режим: три различни поправки, всяка последвана от същия тест със
    същия червен изход до третата. Ходът спираше на третото пускане със
    „`RUN_CMD` беше извикан 3 пъти подред“ и третата поправка не се пускаше."""
    (ws / "app.py").write_text("def total(items):\n    return sum(items)\n", encoding="utf-8")
    red = ("[RUN_CMD: python test_app.py]  (rc=1)\nF\nFAIL: test_total (__main__.T.test_total)\n"
           "AssertionError: 3 != 4\n\nFAILED (failures=1)")
    green = "[RUN_CMD: python test_app.py]  (rc=0)\n.\nRan 1 test in 0.001s\n\nOK"
    _fake_run_cmd(monkeypatch, [red, red, red, green])
    sep = "---GENESIS-REPLACE-WITH---"

    def edit(old: str, new: str) -> str:
        return f"[EDIT_FILE: app.py]\n{old}\n{sep}\n{new}\n[END_EDIT]\n[RUN_CMD: python test_app.py]"

    seen = _script(monkeypatch, [
        ("[RUN_CMD: python test_app.py]", None),
        ("Опит 1.\n" + edit("    return sum(items)", "    return sum(list(items))"), None),
        ("Опит 2.\n" + edit("    return sum(list(items))", "    return sum(tuple(items))"), None),
        ("Опит 3.\n" + edit("    return sum(tuple(items))", "    return sum(items) + 1"), None),
        ("Готово: app.py е поправен.", None),
        # Бележката „пробвай го и извън примерите“ след написан код иска още един отговор.
        ("Готово: app.py е поправен.", None),
    ])
    code, data = _headless(ws, capsys)
    assert "подред" not in json.dumps(data, ensure_ascii=False)
    assert "sum(items) + 1" in (ws / "app.py").read_text(encoding="utf-8")
    assert code == 0 and data["tools"].count("RUN_CMD") == 4
    assert len(seen) == 6
