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

import uuid
import threading
import time
import sys
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable, Dict, List
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


@dataclass
class DelegatedTask:
    id: str
    goal: str
    agent: str
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    thread: Optional[threading.Thread] = field(default=None, repr=False)

    @property
    def elapsed(self) -> Optional[float]:
        if self.started_at is None:
            return None
        end = self.finished_at or time.time()
        return round(end - self.started_at, 2)

    def summary(self) -> str:
        icon = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌", "timeout": "⏰"}.get(self.status, "?")
        return f"{icon} [{self.id[:8]}] {self.agent}:{self.goal[:60]}  ({self.elapsed}s)"


# ─── Глобален регистър ────────────────────────────────────────────────────────

_TASKS: Dict[str, DelegatedTask] = {}
_LOCK = threading.Lock()


def get_status(task_id: str) -> Optional[DelegatedTask]:
    return _TASKS.get(task_id)


def list_tasks() -> List[DelegatedTask]:
    with _LOCK:
        return list(_TASKS.values())


# ─── Изпълнители (agent backends) ─────────────────────────────────────────────

def _run_shell(goal: str) -> str:
    """Изпълнява shell команда през genesis_agent.sandbox и връща stdout."""
    from genesis_agent import sandbox
    res = sandbox.run_shell(goal, timeout=120)
    if res.blocked:
        raise RuntimeError(res.stderr)
    if not res.ok:
        raise RuntimeError(f"Shell грешка (code {res.returncode}):\n{res.stderr[:1000]}")
    return res.stdout.strip()


def _run_skill(skill_name: str) -> str:
    """Изпълнява умение от skills/ директорията."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from genesis_agent.skill_loader import run_skill
    return run_skill(skill_name)


def _run_autonomous(goal: str) -> str:
    """Изпълнява задача чрез Genesis autonomous_loop."""
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from genesis_agent.autonomous_loop import run_autonomous_loop
    out = run_autonomous_loop(goal, operator_id="DELEGATE")
    if out.success:
        return f"✅ Готово за {out.rounds} рунда → {Path(out.skill_path).name}"
    raise RuntimeError(f"Autonomous loop неуспешен след {out.rounds} опита.")


def _worker(task: DelegatedTask, timeout: int, on_done: Optional[Callable]):
    """Вътрешен worker — изпълнява задачата в отделна нишка."""
    task.status = TaskStatus.RUNNING
    task.started_at = time.time()
    log.info(f"[delegate] Стартиран task {task.id[:8]} ({task.agent}): {task.goal[:60]}")

    try:
        if task.agent == "shell":
            result = _run_shell(task.goal)
        elif task.agent == "skill":
            result = _run_skill(task.goal)
        elif task.agent == "autonomous":
            result = _run_autonomous(task.goal)
        else:
            raise ValueError(f"Непознат agent тип: '{task.agent}'. Използвай: shell, skill, autonomous")

        task.result = result
        task.status = TaskStatus.DONE
        log.info(f"[delegate] ✅ Task {task.id[:8]} завършен ({task.elapsed}s)")

    except Exception as e:
        task.error = str(e)
        task.status = TaskStatus.FAILED
        log.error(f"[delegate] ❌ Task {task.id[:8]} провален: {e}")
    finally:
        task.finished_at = time.time()
        if on_done:
            try:
                on_done(task)
            except Exception as cb_err:
                log.error(f"[delegate] Callback грешка: {cb_err}")


# ─── Публичен интерфейс ───────────────────────────────────────────────────────

def delegate_task(
    goal: str,
    *,
    agent: str = "autonomous",
    timeout: int = 300,
    on_done: Optional[Callable[[DelegatedTask], None]] = None,
) -> DelegatedTask:
    """
    Делегира задача към под-агент и я изпълнява в отделна нишка.

    Args:
        goal:    Описание на задачата или shell команда.
        agent:   'autonomous' | 'shell' | 'skill'
        timeout: Максимално изчакване в секунди (засега не прекъсва force).
        on_done: Callback функция извикана при завършване.

    Returns:
        DelegatedTask обект (следи се статусът с .status).
    """
    task_id = uuid.uuid4().hex
    task = DelegatedTask(id=task_id, goal=goal, agent=agent)

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


def wait_all(tasks: List[DelegatedTask], timeout: int = 300) -> List[DelegatedTask]:
    """
    Изчаква всички задачи да приключат (или да изтече timeout).

    Returns:
        Списъкът от задачи с попълнени status, result, error.
    """
    deadline = time.time() + timeout
    for task in tasks:
        remaining = max(0, deadline - time.time())
        if task.thread and task.thread.is_alive():
            task.thread.join(timeout=remaining)
            if task.thread.is_alive():
                task.status = TaskStatus.TIMEOUT
                log.warning(f"[delegate] ⏰ Timeout за task {task.id[:8]}")
    return tasks


def run_parallel(goals: List[str], agent: str = "shell", timeout: int = 120) -> List[DelegatedTask]:
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
