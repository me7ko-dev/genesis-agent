"""Одит 2026-10-31 (шести кръг): делегираната мисия (genesis_agent/delegate.py,
DELEGATE в genesis_skills, autonomous_loop). Стоп и Ctrl-C не спираха
под-агента — той продължаваше да вика модела, да пуска код и да записва умение
и след като ходът беше свършил; въпросът на sandbox-а висеше в нишката му и
изяждаше следващия ред на чата; отговор, дошъл след „timeout“, пак се
изпълняваше; провалът криеше причината и винаги казваше „8 опита“; празна цел
пускаше пълна платена мисия на нищо. Всеки тест падаше срещу кода преди
поправката (моделът е фалшив, без ключове и мрежа)."""
from __future__ import annotations

import _thread
import threading
import time
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

import genesis_agent.delegate as dm
import genesis_skills
from genesis_agent import autonomous_loop as al
from genesis_agent import executor, sandbox
from genesis_agent.brain import Brain as _RealBrain
from genesis_agent.executor import ExecResult
from genesis_agent.local_repair_agent import RepairResult
from genesis_agent.skill_loader import SKILLS_ROOT
from genesis_agent.storage_monitor import StorageReport
from genesis_agent.verifier import VerifyResult

_FAIL = "raise RuntimeError('boom')"
_PASS = "assert 1 + 1 == 2\nprint('OK')"


def _code(code: str) -> SimpleNamespace:
    return SimpleNamespace(raw_text=f"```python\n{code}\n```", code=code, tool_calls=None, usage=None)


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(raw_text=text, code=None, tool_calls=None, usage=None)


class _Brain:
    """Фалшивият модел: `script(n)` е отговорът на n-тото извикване (рундове и
    критик подред); `started` пази кога е ПОЧНАЛО всяко извикване."""

    trim_round_history = staticmethod(_RealBrain.trim_round_history)
    local = None
    chain: ClassVar[list[dict]] = [{"provider": "fake", "model": "fake"}]
    current = None
    delay = 0.0
    started: ClassVar[list[float]] = []
    script: Any = None

    def __init__(self, *a, **kw) -> None:
        pass

    def route_for_goal(self, goal: str) -> None:
        pass

    def build_context(self, goal: str) -> str:
        return ""

    def system_prompt_base(self) -> str:
        return "sys"

    def escalate(self) -> bool:
        return False

    def generate_local_candidates(self, messages, n=2):
        return []

    def complete(self, messages, tools=None, avoid=None):
        n = len(_Brain.started)
        _Brain.started.append(time.monotonic())
        time.sleep(_Brain.delay)
        return _Brain.script(n)


def _after(moment: float, times: list[float]) -> list[float]:
    return [round(t - moment, 2) for t in times if t > moment]


def _join_missions(limit: float = 15.0) -> None:
    for task in dm.list_tasks():
        if task.thread is not None:
            task.thread.join(limit)


@pytest.fixture
def rec(monkeypatch):
    """Всичко около мисията е фалшиво и записва кога е викано; мисиите
    довършват, преди подмените да се махнат."""
    r = SimpleNamespace(execs=[], saved=[], repaired=[], recorded=[])
    _Brain.started = []
    _Brain.delay = 0.0
    _Brain.script = lambda n: _text("няма код")
    monkeypatch.setattr(al, "Brain", _Brain)
    monkeypatch.setattr(al, "check_storage", lambda: StorageReport(
        total_bytes=0, threshold_bytes=1, compression_required=False,
        log_path=None, candidates_path=None))
    monkeypatch.setattr("genesis_agent.reflection.lessons_for_prompt", lambda *a, **kw: "")
    monkeypatch.setattr("genesis_agent.reflection.record_mission",
                        lambda *a, **kw: r.recorded.append(a))
    monkeypatch.setattr("genesis_agent.code_validate.validate_code_with_ruff", lambda c: (True, ""))
    monkeypatch.setattr("genesis_agent.verifier.verify_skill",
                        lambda c: VerifyResult(True, "self_test_passed", "OK"))

    def run(code: str) -> ExecResult:
        r.execs.append(time.monotonic())
        if code == _PASS:
            return ExecResult(ok=True, stdout="OK", stderr="", returncode=0)
        return ExecResult(ok=False, stdout="", returncode=1,
                          stderr="Traceback (most recent call last):\nRuntimeError: boom")

    monkeypatch.setattr(al, "run_python_subprocess", run)
    monkeypatch.setattr(al, "save_skill",
                        lambda **kw: r.saved.append(time.monotonic()) or SKILLS_ROOT / "x.md")
    monkeypatch.setattr(al, "emergency_repair", lambda code, err, out: r.repaired.append(
        time.monotonic()) or RepairResult(False, code, 0, "none", ""))
    dm._TASKS.clear()
    yield r
    _join_missions()
    dm._TASKS.clear()
    genesis_skills._set_cancel(None)


# ── a1. Стоп и Ctrl-C спират и под-агента, не само чакането ──────────────────

def test_stop_ends_delegate_within_a_second_and_the_mission_does_nothing_more(rec) -> None:
    """Измерено преди: DELEGATE върна 3.4 s след старта (Стоп на 0.5 s), със
    7 извиквания на модела след Стоп, авариен ремонт и „неуспешен след 8 опита“."""
    _Brain.delay = 0.3
    _Brain.script = lambda n: _code(_FAIL)
    stop = threading.Event()
    pressed: list[float] = []

    def press() -> None:
        pressed.append(time.monotonic())
        stop.set()

    genesis_skills._set_cancel(stop.is_set)   # както run_turn за терминала и телефона
    timer = threading.Timer(0.5, press)
    timer.start()
    out = genesis_skills.dispatch_tool_call("DELEGATE", {"goal": "напиши скрипт"})
    returned = time.monotonic()
    timer.join()
    genesis_skills._set_cancel(None)          # finally на run_turn
    _join_missions()

    assert returned - pressed[0] < 1.0, f"DELEGATE върна {returned - pressed[0]:.1f} s след Стоп"
    assert "статус=cancelled" in out and "Спряно от оператора" in out, out
    assert _after(pressed[0], _Brain.started) == [], "модел, извикан след Стоп"
    assert _after(pressed[0], rec.execs) == [], "код, пуснат след Стоп"
    assert rec.saved == [] and rec.repaired == []
    assert rec.recorded == [], "Стоп не е провал на мисията — без урок от него"


def test_ctrl_c_during_delegate_interrupts_at_once_and_nothing_is_saved_afterwards(rec) -> None:
    """Ctrl-C идва като _thread.interrupt_main (не буди чакането, както Ctrl-C
    на Windows преди 3.14). Измерено преди: KeyboardInterrupt чак когато мисията
    свърши, а тя междувременно пусна кода и записа умението — и в режим план."""
    _Brain.delay = 0.5
    script = [_code(_FAIL), _code(_FAIL), _code(_PASS), _text("YES")]
    _Brain.script = lambda n: script[n] if n < len(script) else _text("YES")
    hit: list[float] = []

    def ctrl_c() -> None:
        hit.append(time.monotonic())
        _thread.interrupt_main()

    timer = threading.Timer(0.7, ctrl_c)   # посред втория рунд (0.5–1.0 s)
    timer.start()
    with pytest.raises(KeyboardInterrupt):
        genesis_skills.dispatch_tool_call("DELEGATE", {"goal": "напиши скрипт"})
        time.sleep(5)   # закъснял сигнал да падне тук, не в следващия тест
    caught = time.monotonic()
    timer.join()
    genesis_skills._set_cancel(None)          # finally на run_turn
    _join_missions()

    assert caught - hit[0] < 0.6, f"Ctrl-C стигна {caught - hit[0]:.1f} s по-късно"
    assert _after(hit[0], _Brain.started) == [], "модел, извикан след Ctrl-C"
    assert _after(hit[0], rec.execs) == [], "код, пуснат след Ctrl-C"
    assert rec.saved == [], "умение, записано след Ctrl-C"


def test_after_ctrl_c_the_missions_running_code_is_killed_even_once_the_turn_has_ended(
        rec, monkeypatch, tmp_path) -> None:
    """finally на run_turn маха sandbox.cancel_check — а детето на мисията
    трябва да умре и след това (измерено преди: записа файла си след Ctrl-C)."""
    sbx = tmp_path / "sbx"
    monkeypatch.setattr("genesis_agent.config.SANDBOX_DIR", sbx)
    monkeypatch.setattr(executor, "SANDBOX_DIR", sbx)
    monkeypatch.setattr(al, "run_python_subprocess", executor.run_python_subprocess)
    monkeypatch.setattr(sandbox, "_POLICY", sandbox.SandboxPolicy(mode="deny"))
    monkeypatch.setattr(sandbox, "cancel_check", None)
    slow = "import time\ntime.sleep(2.5)\nopen('late.txt', 'w').write('x')\nprint('OK')\n"
    _Brain.script = lambda n: _code(slow) if n == 0 else _text("YES")
    hit: list[float] = []

    def ctrl_c() -> None:
        hit.append(time.monotonic())
        _thread.interrupt_main()

    timer = threading.Timer(1.0, ctrl_c)
    timer.start()
    with pytest.raises(KeyboardInterrupt):
        genesis_skills.dispatch_tool_call("DELEGATE", {"goal": "напиши скрипт"})
        time.sleep(8)
    caught = time.monotonic()
    timer.join()
    genesis_skills._set_cancel(None)
    _join_missions()
    time.sleep(0.3)

    assert caught - hit[0] < 0.6, f"Ctrl-C стигна {caught - hit[0]:.1f} s по-късно"
    assert not (sbx / "late.txt").exists(), "кодът на мисията довърши след Ctrl-C"


def test_a_question_from_the_mission_is_asked_in_the_waiting_thread(rec, monkeypatch, tmp_path) -> None:
    """input() в нишката на под-агента не получава Ctrl-C и остава да чака след
    края на хода — следващият ред на чата („да“) отиваше като отговор на
    нейния въпрос. Въпросът се задава в нишката, която чака DELEGATE."""
    sbx = tmp_path / "sbx"
    monkeypatch.setattr("genesis_agent.config.SANDBOX_DIR", sbx)
    monkeypatch.setattr(executor, "SANDBOX_DIR", sbx)
    monkeypatch.setattr(al, "run_python_subprocess", executor.run_python_subprocess)
    asked_in: list[str] = []

    def confirm(operation: str, verdict) -> bool:
        asked_in.append(threading.current_thread().name)
        return False

    monkeypatch.setattr(sandbox, "_POLICY",
                        sandbox.SandboxPolicy(mode="interactive", confirm_fn=confirm))
    needs_ok = "import subprocess\nsubprocess.run(['git', '--version'])\nprint('OK')\n"
    _Brain.script = lambda n: _code(needs_ok) if n == 0 else _text("няма код")

    out = genesis_skills.dispatch_tool_call("DELEGATE", {"goal": "напиши скрипт"})

    assert asked_in == [threading.current_thread().name], (asked_in, out)


# ── a2. След „timeout“ отговорът от рунда не се изпълнява ────────────────────

def test_a_reply_that_arrives_after_the_deadline_is_not_executed(rec) -> None:
    """Измерено преди: кодът се пусна 0.5 s след като викащият получи „timeout“,
    после verify_skill и критикът; спря го само проверката преди save_skill."""
    _Brain.delay = 0.5
    _Brain.script = lambda n: _code(_PASS) if n == 0 else _text("YES")
    out = al.run_autonomous_loop("напиши скрипт", max_rounds=2, deadline=time.time() + 0.2)
    assert rec.execs == [], "кодът се изпълни след крайния срок"
    assert len(_Brain.started) == 1, "критикът е викан след крайния срок"
    assert out.success is False and rec.saved == []


def test_a_tool_call_that_arrives_after_the_deadline_is_not_dispatched(rec, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(genesis_skills, "dispatch_tool_call",
                        lambda name, args: calls.append(name) or "ok")
    tc = [{"id": "1", "function": {"name": "USE_SKILL", "arguments": '{"name_or_query": "x"}'}}]
    _Brain.delay = 0.5
    _Brain.script = lambda n: SimpleNamespace(raw_text="", code=None, tool_calls=tc, usage=None)
    al.run_autonomous_loop("напиши скрипт", max_rounds=1, deadline=time.time() + 0.2)
    assert calls == [], "инструмент, изпълнен след крайния срок"


def test_after_delegate_reports_timeout_the_round_in_flight_runs_nothing(rec) -> None:
    _Brain.delay = 0.6
    _Brain.script = lambda n: _code(_PASS) if n == 0 else _text("YES")
    task = dm.delegate_task("напиши скрипт", agent="autonomous", timeout=0.3)  # type: ignore[arg-type]
    dm.wait_all([task], timeout=0.3)  # type: ignore[arg-type]
    assert task.status == dm.TaskStatus.TIMEOUT
    told = time.monotonic()
    _join_missions()
    assert _after(told, rec.execs) == [], "кодът се изпълни, след като DELEGATE каза timeout"
    assert rec.saved == []


# ── a3. Провалът казва причината и колко рунда наистина е имало ──────────────

def test_an_unreachable_model_is_reported_as_the_cause_after_one_round(rec) -> None:
    """Измерено преди: 1 извикване на модела, а моделът видя „неуспешен след 8
    опита.“ без причина — и пробваше DELEGATE пак, по 8 платени рунда."""
    _Brain.script = lambda n: _text("Error: цялата верига е изчерпана (няма ключове)")
    out = genesis_skills.dispatch_tool_call("DELEGATE", {"goal": "напиши fizzbuzz скрипт"})
    assert len(_Brain.started) == 1
    assert "цялата верига е изчерпана" in out, out
    assert "след 1 рунд" in out, out


def test_a_goal_refused_by_dna_reaches_the_model_with_the_reason(rec) -> None:
    out = genesis_skills.dispatch_tool_call("DELEGATE", {"goal": "write a script to harm people"})
    assert _Brain.started == []
    assert "GENE-ETHICS" in out, out


def test_strict_authority_refusal_is_not_hidden(rec, monkeypatch) -> None:
    monkeypatch.setenv("GENESIS_STRICT_AUTHORITY", "1")
    monkeypatch.setenv("GENESIS_OPERATOR", "roi")
    out = genesis_skills.dispatch_tool_call("DELEGATE", {"goal": "write a fizzbuzz script"})
    assert "GENE-AUTHORITY" in out, out


def test_the_traceback_of_the_last_round_is_in_the_result(rec) -> None:
    _Brain.script = lambda n: _code(_FAIL)
    out = genesis_skills.dispatch_tool_call("DELEGATE", {"goal": "напиши скрипт"})
    assert "RuntimeError: boom" in out, out
    assert "след 8 рунда" in out, out


def test_a_loop_that_stops_early_reports_the_rounds_it_really_ran(rec) -> None:
    _Brain.script = lambda n: _text("Error: мрежата я няма")
    out = al.run_autonomous_loop("напиши скрипт", max_rounds=8)
    assert out.rounds == 1


# ── a4. Празна или нетекстова цел не пуска мисия ─────────────────────────────

@pytest.mark.parametrize("args", [{}, {"goal": ""}, {"goal": "   "}, {"goal": None},
                                  {"goal": 42}, {"goal": ["x"]}])
def test_an_empty_or_non_text_goal_is_refused_without_a_mission(rec, args) -> None:
    out = genesis_skills.dispatch_tool_call("DELEGATE", args)
    assert "Празна цел" in out, out
    assert _Brain.started == [] and dm.list_tasks() == []


def test_an_empty_text_tag_is_refused_too(rec) -> None:
    out = genesis_skills.parse_and_execute_tools("[DELEGATE: ]")
    assert any("Празна цел" in r for r in out), out
    assert _Brain.started == [] and dm.list_tasks() == []
