#!/usr/bin/env python3
"""
genesis_agent/scheduler.py — Cron/Scheduled Jobs за Genesis Agent.

Scheduled jobs:
  - Заявките се описват в cron_jobs.json (или програмно)
  - Изпълнява се в отделна нишка
  - Поддържа cron syntax и interval-based изпълнение

Употреба:
    from genesis_agent.scheduler import start_scheduler, add_job
    add_job("backup", "0 3 * * *", lambda: run_task("backup files"))
    start_scheduler()
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Optional

from genesis_agent.config import DATA_DIR

log = logging.getLogger("genesis.scheduler")
JOBS_FILE = DATA_DIR / "cron_jobs.json"

# ─── Вградена минимална cron интерпретация ───────────────────────────────────

def _parse_interval(schedule: str) -> Optional[int]:
    """
    Опростен parser. Поддържа:
      "@hourly"  → 3600
      "@daily"   → 86400
      "@weekly"  → 604800
      "every 30m" → 1800
      "every 2h"  → 7200
    За пълен cron syntax → използва croniter ако е наличен.
    """
    s = schedule.strip().lower()
    if s == "@hourly":
        return 3600
    if s in ("@daily", "@midnight"):
        return 86400
    if s == "@weekly":
        return 604800
    if s == "@reboot":
        return 0  # Веднъж при старт
    if s.startswith("every "):
        parts = s.split()
        if len(parts) == 2:
            val_str = parts[1]
            if val_str.endswith("m"):
                return int(val_str[:-1]) * 60
            if val_str.endswith("h"):
                return int(val_str[:-1]) * 3600
            if val_str.endswith("s"):
                return int(val_str[:-1])
    # Опит с croniter (ако е инсталиран)
    try:
        from croniter import croniter
        now = datetime.now()
        cron = croniter(schedule, now)
        nxt = cron.get_next(datetime)
        return int((nxt - now).total_seconds())
    except Exception:
        return None


# ─── Job registry ─────────────────────────────────────────────────────────────

class Job:
    def __init__(self, name: str, schedule: str, func: Callable, enabled: bool = True):
        self.name = name
        self.schedule = schedule
        self.func = func
        self.enabled = enabled
        self.last_run: Optional[datetime] = None
        self.next_run: Optional[datetime] = None
        self._set_next_run()

    def _set_next_run(self):
        interval = _parse_interval(self.schedule)
        if interval is not None:
            self.next_run = datetime.now() + timedelta(seconds=interval)
        else:
            log.warning(f"[scheduler] Невалиден schedule за '{self.name}': {self.schedule}")
            self.next_run = None

    def is_due(self) -> bool:
        if not self.enabled or self.next_run is None:
            return False
        return datetime.now() >= self.next_run

    def execute(self):
        log.info(f"[scheduler] Изпълнявам job: {self.name}")
        try:
            self.func()
        except Exception as e:
            log.error(f"[scheduler] Грешка в job '{self.name}': {e}")
        self.last_run = datetime.now()
        self._set_next_run()


_JOBS: Dict[str, Job] = {}
_SCHEDULER_RUNNING = False
_SCHEDULER_THREAD: Optional[threading.Thread] = None


def add_job(name: str, schedule: str, func: Callable, enabled: bool = True) -> Job:
    """Добавя нов scheduled job."""
    job = Job(name, schedule, func, enabled)
    _JOBS[name] = job
    log.info(f"[scheduler] Регистриран job: {name} ({schedule}), следващо изпълнение: {job.next_run}")
    return job


def remove_job(name: str) -> bool:
    """Премахва job по име."""
    return _JOBS.pop(name, None) is not None


def list_jobs() -> list[dict]:
    """Връща списък с всички jobs и техния статус."""
    return [
        {
            "name": j.name,
            "schedule": j.schedule,
            "enabled": j.enabled,
            "last_run": j.last_run.isoformat() if j.last_run else None,
            "next_run": j.next_run.isoformat() if j.next_run else None,
        }
        for j in _JOBS.values()
    ]


def _scheduler_loop(tick_seconds: int = 30):
    """Вътрешен loop — проверява и изпълнява дължимите jobs."""
    global _SCHEDULER_RUNNING
    while _SCHEDULER_RUNNING:
        for job in list(_JOBS.values()):
            if job.is_due():
                threading.Thread(target=job.execute, daemon=True).start()
        time.sleep(tick_seconds)


def start_scheduler(tick_seconds: int = 30) -> threading.Thread:
    """Стартира scheduler в отделна daemon нишка."""
    global _SCHEDULER_RUNNING, _SCHEDULER_THREAD
    if _SCHEDULER_RUNNING:
        log.info("[scheduler] Вече работи.")
        return _SCHEDULER_THREAD  # type: ignore

    # Зареждаме jobs от файл (ако съществува)
    _load_jobs_from_file()

    _SCHEDULER_RUNNING = True
    t = threading.Thread(target=_scheduler_loop, args=(tick_seconds,), daemon=True)
    t.start()
    _SCHEDULER_THREAD = t
    log.info(f"[scheduler] Стартиран с {len(_JOBS)} jobs. Tick: {tick_seconds}s")
    return t


def stop_scheduler():
    """Спира scheduler loop."""
    global _SCHEDULER_RUNNING
    _SCHEDULER_RUNNING = False
    log.info("[scheduler] Спрян.")


def _load_jobs_from_file():
    """Зарежда job дефиниции от cron_jobs.json (само описание, без func)."""
    if not JOBS_FILE.exists():
        return
    try:
        jobs_data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.error(f"[scheduler] Грешка при четене на {JOBS_FILE}: {e}")
        return

    for jd in jobs_data:
        name = jd.get("name")
        schedule = jd.get("schedule")
        command = jd.get("command")
        enabled = jd.get("enabled", True)

        if not (name and schedule and command):
            continue

        # Wrap командата през genesis_agent.sandbox (защитната бариера).
        from genesis_agent import sandbox
        def _make_func(cmd: str):
            def _run():
                result = sandbox.run_shell(cmd, timeout=300)
                if result.blocked:
                    log.error(f"[scheduler] '{name}' отказан от sandbox: {result.stderr[:500]}")
                elif result.returncode != 0:
                    log.error(f"[scheduler] '{name}' грешка: {result.stderr[:500]}")
                else:
                    log.info(f"[scheduler] '{name}' OK: {result.stdout[:200]}")
            return _run

        add_job(name, schedule, _make_func(command), enabled=enabled)


def save_jobs_to_file():
    """Записва текущите jobs в cron_jobs.json (само тези с команди)."""
    # Не можем да сериализираме Python функции, запазваме само метаданни
    jobs_info = list_jobs()
    JOBS_FILE.write_text(json.dumps(jobs_info, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"[scheduler] Запазени {len(jobs_info)} jobs в {JOBS_FILE}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Пример: Job на всеки 10 секунди
    add_job("ping", "every 10s", lambda: print(f"🏓 ping @ {datetime.now().strftime('%H:%M:%S')}"))
    add_job("heartbeat", "@hourly", lambda: print("💓 hourly heartbeat"))

    print("Jobs:", list_jobs())
    start_scheduler(tick_seconds=5)
    time.sleep(25)
    stop_scheduler()
