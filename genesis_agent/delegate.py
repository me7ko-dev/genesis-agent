#!/usr/bin/env python3
"""
genesis_agent/delegate.py — Sub-agent Orchestration за Genesis Agent.

Sub-agent delegation:
  - Изпълнява задачи в паралелни независими процеси
  - Поддържа timeout, callbacks и статус tracking
  - Може да се делегира към: autonomous_loop, external skill, shell команда

Употреба:
    from genesis_agent.delegate import delegate_task, wait_all, get_status

    # Паралелно изпълнение на 3 задачи
    t1 = delegate_task("напиши функция за сортиране", agent="autonomous")
    t2 = delegate_task("ls -la /tmp", agent="shell")
    t3 = delegate_task("backup_files", agent="skill")

    results = wait_all([t1, t2, t3], timeout=300)
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

log = logging.getLogger("genesis.delegate")

# ─── Статус на задача ─────────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    DONE     = "done"
    FAILED   = "failed"
    TIMEOUT  = "timeout"
    CANCELLED = "cancelled"   # Стоп или Ctrl-C на оператора (одит 2026-10-31)


_HALTED = (TaskStatus.TIMEOUT, TaskStatus.CANCELLED)

# Какво вижда моделът, когато операторът спре DELEGATE.
CANCELLED_NOTE = ("⏹ Спряно от оператора — под-агентът спря: не вика модела, не пуска код "
                  "и не записва умение.")

# Чакащият поглежда за Стоп и за въпрос на под-агента на парчета по 0.1 s, не с
# едно join(600). Одит 2026-10-31, измерено: Стоп на 0.5 s — DELEGATE върна на
# 3.4 s, след още 7 извиквания на модела; Ctrl-C през _thread.interrupt_main
# (така стига Ctrl-C на Windows до 3.14 — не буди дълго чакане) — чак когато
# мисията свърши, след като тя записа умението.
_POLL_S = 0.1
# Колко въпросът на задачата чака да се появи wait_all, преди да се зададе в
# нишката ѝ (задача, която никой не чака).
_WAITER_GRACE_S = 1.0


@dataclass
class _Ask:
    """Въпрос за потвърждение от нишката на задачата към чакащата нишка."""
    question: Callable[[], bool | str]
    answer: bool | str = False
    done: threading.Event = field(default_factory=threading.Event)


@dataclass
class DelegatedTask:
    id: str
    goal: str
    agent: str
    status: TaskStatus = TaskStatus.PENDING
    result: str | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    thread: threading.Thread | None = field(default=None, repr=False)
    # Спирането на задачата (одит 2026-10-31): `cancel` го вдига wait_all при
    # Стоп, Ctrl-C и таймаут; `should_stop` е проверката за Стоп на хода, който
    # я е пуснал — хваната при старта, защото finally на run_turn маха
    # глобалната, а под-агентът може още да работи.
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    should_stop: Callable[[], bool] | None = field(default=None, repr=False)
    _asks: queue.SimpleQueue[_Ask] = field(default_factory=queue.SimpleQueue, repr=False)
    _waited: threading.Event = field(default_factory=threading.Event, repr=False)

    def stopped(self) -> bool:
        if self.cancel.is_set():
            return True
        check = self.should_stop
        try:
            return bool(check is not None and check())
        except Exception:
            log.debug("проверката за спиране се провали", exc_info=True)
            return False

    def _ask(self, question: Callable[[], bool | str]) -> bool | str:
        """Въпросът на sandbox-а от нишката на задачата: задава се в нишката,
        която я чака (wait_all), не тук — input() тук не получава Ctrl-C и
        оставаше да чака след края на хода (одит 2026-10-31). Без чакащ —
        направо, както досега. След спиране — отказ без въпрос."""
        if self.stopped():
            return False
        # wait_all идва веднага след delegate_task, но нишката е пусната преди
        # него и може да стигне до въпроса първа (ревю 2026-11-05: под товар 7
        # от 30 пускания питаха в нишката на задачата) — затова кратко чакане.
        if not self._waited.wait(_WAITER_GRACE_S):
            return question()
        ask = _Ask(question)
        self._asks.put(ask)
        while not ask.done.wait(_POLL_S):
            if self.stopped():
                return False
        return ask.answer

    def _serve_asks(self) -> None:
        """В чакащата нишка: задава въпросите на задачата, ако има такива."""
        while True:
            try:
                ask = self._asks.get_nowait()
            except queue.Empty:
                return
            try:
                if not self.stopped():
                    ask.answer = ask.question()
            except Exception:
                log.debug("въпросът на под-агента се провали — отказ", exc_info=True)
            finally:
                ask.done.set()

    @property
    def elapsed(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or time.time()
        return round(end - self.started_at, 2)

    def summary(self) -> str:
        icon = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌", "timeout": "⏰",
                "cancelled": "⏹"}.get(self.status, "?")
        return f"{icon} [{self.id[:8]}] {self.agent}:{self.goal[:60]}  ({self.elapsed}s)"


# ─── Глобален регистър ────────────────────────────────────────────────────────

_TASKS: dict[str, DelegatedTask] = {}
_LOCK = threading.Lock()


def get_status(task_id: str) -> DelegatedTask | None:
    return _TASKS.get(task_id)


def list_tasks() -> list[DelegatedTask]:
    with _LOCK:
        return list(_TASKS.values())


# ─── Изпълнители (agent backends) ─────────────────────────────────────────────

def _run_shell(goal: str, timeout: int | None = None) -> str:
    """Изпълнява shell команда през genesis_agent.sandbox и връща stdout."""
    from genesis_agent import sandbox
    res = sandbox.run_shell(goal, timeout=min(timeout or 120, 120))
    if res.blocked:
        raise RuntimeError(res.stderr)
    if not res.ok:
        raise RuntimeError(f"Shell грешка (code {res.returncode}):\n{res.stderr[:1000]}")
    return res.stdout.strip()


def _run_skill(skill_name: str) -> str:
    """Изпълнява умение от skills/ директорията."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from genesis_agent.skill_loader import run_skill
    return run_skill(skill_name)


def _run_autonomous(goal: str, timeout: int | None = None,
                    should_stop: Callable[[], bool] | None = None) -> str:
    """Изпълнява задача чрез Genesis autonomous_loop — до `timeout` секунди
    или докато `should_stop()` не каже да спре."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from genesis_agent.autonomous_loop import rounds_text, run_autonomous_loop
    deadline = time.time() + timeout if timeout else None
    out = run_autonomous_loop(goal, operator_id="DELEGATE", deadline=deadline,
                              should_stop=should_stop)
    if out.success:
        name = Path(out.skill_path).name if out.skill_path else "(без файл)"
        return f"✅ Готово за {rounds_text(out.rounds)} → {name}"
    # Причината, не само броят (одит 2026-10-31): „неуспешен след 8 опита.“
    # беше и при веригата без ключове след 1 извикване, и при отказ на
    # GENE-ETHICS/GENE-AUTHORITY след 0 — моделът нямаше какво да поправи и
    # пускаше DELEGATE пак, по 8 платени рунда. Опашката: изключението на
    # traceback-а е накрая.
    cause = (out.reason or out.last_stderr or "").strip()
    if len(cause) > 800:
        cause = "…" + cause[-800:]
    raise RuntimeError(f"Под-агентът не успя след {rounds_text(out.rounds)}. "
                       f"Причина: {cause or 'без подробности'}")


def _worker(task: DelegatedTask, timeout: int, on_done: Callable | None):
    """Вътрешен worker — изпълнява задачата в отделна нишка."""
    from genesis_agent import sandbox
    # Командите на задачата спират с нея, а въпросите ѝ отиват при чакащия —
    # и след като ходът е свършил (одит 2026-10-31, виж sandbox.bind_thread).
    sandbox.bind_thread(task.stopped, task._ask)
    if task.status == TaskStatus.PENDING:   # wait_all може вече да я е спрял
        task.status = TaskStatus.RUNNING
    task.started_at = time.time()
    log.info(f"[delegate] Стартиран task {task.id[:8]} ({task.agent}): {task.goal[:60]}")

    try:
        if task.agent == "shell":
            result = _run_shell(task.goal, timeout)
        elif task.agent == "skill":
            result = _run_skill(task.goal)
        elif task.agent == "autonomous":
            result = _run_autonomous(task.goal, timeout, should_stop=task.stopped)
        else:
            raise ValueError(f"Непознат agent тип: '{task.agent}'. Използвай: shell, skill, autonomous")

        task.result = result
        # Изтекла/спряна задача остава такава: викащият вече е получил този отговор.
        if task.status not in _HALTED:
            task.status = TaskStatus.DONE
        log.info(f"[delegate] ✅ Task {task.id[:8]} завършен ({task.elapsed}s)")

    except Exception as e:  # noqa: BLE001 — вече се записва с log.error по-долу
        # Мисията сама видя Стоп и свърши, преди wait_all да я спре (Windows CI
        # 2026-11-06, PR #111): той проверява на _POLL_S, а свършила нишка вече
        # не гледа — статусът оставаше failed с „Причина: спряно от оператора“.
        if task.status not in _HALTED and task.stopped():
            _halt(task, TaskStatus.CANCELLED)
        if task.status != TaskStatus.CANCELLED:   # CANCELLED_NOTE остава
            task.error = str(e)
        if task.status not in _HALTED:
            task.status = TaskStatus.FAILED
        log.error(f"[delegate] ❌ Task {task.id[:8]} провален: {e}")
    finally:
        sandbox.bind_thread(None, None)
        # `_TASKS` не се чисти, а проверката държи UI-я на хода (на телефона —
        # RemoteSession) до края на процеса (прегледът, 2026-11-05).
        task.should_stop = None
        task.finished_at = time.time()
        if on_done:
            try:
                on_done(task)
            except Exception as cb_err:  # noqa: BLE001 — вече се записва с log.error
                log.error(f"[delegate] Callback грешка: {cb_err}")


# ─── Публичен интерфейс ───────────────────────────────────────────────────────

def delegate_task(
    goal: str,
    *,
    agent: str = "autonomous",
    timeout: int = 300,
    on_done: Callable[[DelegatedTask], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> DelegatedTask:
    """
    Делегира задача към под-агент и я изпълнява в отделна нишка.

    Args:
        goal:    Описание на задачата или shell команда.
        agent:   'autonomous' | 'shell' | 'skill'
        timeout: Срок в секунди: след него мисията не пуска и не записва нищо
                 ново (нишката не се убива принудително).
        on_done: Callback функция извикана при завършване.
        should_stop: Стопът на викащия (бутонът на телефона) — задачата спира
                 с него, и когато вече никой не я чака.

    Returns:
        DelegatedTask обект (следи се статусът с .status).
    """
    task_id = uuid.uuid4().hex
    task = DelegatedTask(id=task_id, goal=goal, agent=agent, should_stop=should_stop)

    with _LOCK:
        _TASKS[task_id] = task

    t = threading.Thread(
        target=_worker,
        args=(task, timeout, on_done),
        daemon=True,
        name=f"delegate-{task_id[:8]}"
    )
    task.thread = t
    t.start()
    return task


def _halt(task: DelegatedTask, status: TaskStatus) -> None:
    """Викащият спира да чака: задачата спира и тя — нищо повече не пуска и не
    записва (одит 2026-10-31), — а статусът казва защо."""
    task.cancel.set()
    if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
        task.status = status
        if status == TaskStatus.CANCELLED and task.error is None:
            task.error = CANCELLED_NOTE


def cancel_tasks(tasks: list[DelegatedTask]) -> None:
    """Спира недовършените: статус „cancelled“ и сигнал към под-агента."""
    for task in tasks:
        _halt(task, TaskStatus.CANCELLED)


def wait_all(tasks: list[DelegatedTask], timeout: int = 300, *,
             cancelled: Callable[[], bool] | None = None) -> list[DelegatedTask]:
    """
    Изчаква всички задачи да приключат (или да изтече timeout, или Стоп на
    задачата). Междувременно задава въпросите им за потвърждение в ТАЗИ нишка.
    При timeout, Стоп (на задачата или `cancelled()` на викащия) и Ctrl-C
    (KeyboardInterrupt) задачата спира, не остава да работи на заден план.

    Returns:
        Списъкът от задачи с попълнени status, result, error.
    """
    deadline = time.time() + timeout

    def running() -> list[DelegatedTask]:
        return [t for t in tasks if t.thread is not None and t.thread.is_alive()
                and t.status not in _HALTED]

    for task in tasks:
        task._waited.set()
    try:
        while alive := running():
            if cancelled is not None and cancelled():
                cancel_tasks(alive)
                log.warning("[delegate] ⏹ Задачите са спрени от оператора")
                break
            for task in alive:
                task._serve_asks()
                if task.stopped():
                    _halt(task, TaskStatus.CANCELLED)
                    log.warning(f"[delegate] ⏹ Спряна задача {task.id[:8]}")
            remaining = deadline - time.time()
            if remaining <= 0:
                for task in running():
                    _halt(task, TaskStatus.TIMEOUT)
                    log.warning(f"[delegate] ⏰ Timeout за task {task.id[:8]}")
                break
            for task in running()[:1]:
                if task.thread is not None:
                    task.thread.join(min(_POLL_S, remaining))
    except BaseException:
        # Ctrl-C: под-агентът спира с хода, а не продължава в нишката си. Всички,
        # не running(): до 3.13 join(), прекъснат от Ctrl-C, маркира живата нишка
        # като свършила (bpo-45274) и is_alive() връща False — ревю 2026-11-05,
        # измерено на 3.10 и 3.12: мисията вика модела още 2 пъти и записва умението.
        for task in tasks:
            _halt(task, TaskStatus.CANCELLED)
        raise
    finally:
        for task in tasks:
            task._waited.clear()
    return tasks


def run_parallel(goals: list[str], agent: str = "shell", timeout: int = 120) -> list[DelegatedTask]:
    """
    Удобен helper — делегира всички задачи паралелно и изчаква.

    Пример:
        results = run_parallel([
            "ls ~",
            "df -h",
            "free -m",
        ], agent="shell")
        for r in results:
            print(r.summary())
    """
    tasks = [delegate_task(goal, agent=agent, timeout=timeout) for goal in goals]
    return wait_all(tasks, timeout=timeout + 5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("🧪 Тест: Паралелно изпълнение на 3 shell команди")
    results = run_parallel([
        "echo 'Task 1: Hello от под-агент!' && sleep 1",
        "echo 'Task 2: ' && uname -a",
        "echo 'Task 3: ' && date",
    ], agent="shell", timeout=30)

    print("\n📊 Резултати:")
    for task in results:
        print(task.summary())
        if task.result:
            print(f"   Output: {task.result[:100]}")
        if task.error:
            print(f"   Error: {task.error[:100]}")
