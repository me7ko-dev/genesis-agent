"""
genesis_agent.sqlite_store — връзка към SQLite хранилище на паметта, която
преживява повреден файл.

Защо (одит 2026-10-22, възпроизведено): knowledge_graph слага нечетимия си
файл настрани (.corrupt) и започва на чисто, а SQLite хранилищата — не.
Повредена workspace_memory.db връщаше „file is not a database“ на всеки
REMEMBER/TASK_ADD във всяка следваща сесия, а брифингът тихо изчезваше.
Повредена persistent_memory.db (KV, в която кодът не пише нищо) гърмеше още
при внасяне на memory.py и сваляше със себе си паметта на разговора и
записа на епизоди — без нито дума към оператора.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("genesis.sqlite_store")

# Само повреден ФАЙЛ. „database is locked“ е зает файл, а не развален — той
# не се мести никога, иначе втори процес би изхвърлил живата памет.
_UNREADABLE = ("file is not a database", "database disk image is malformed")


def _unreadable(e: sqlite3.DatabaseError) -> bool:
    return any(m in str(e) for m in _UNREADABLE)


def _set_wal(conn: sqlite3.Connection, timeout: float) -> None:
    """PRAGMA journal_mode=WAL, като чака заета база до timeout.

    Ревю 2026-10-22, мерено: на база, която още не е WAL (нова или стара),
    прагмата вдига четене до запис, а там SQLite не вика busy_timeout и връща
    „database is locked“ веднага — 8 процеса върху нова база губеха 1–3
    REMEMBER/TASK_ADD в 4 от 30 пускания. Щом е WAL, прагмата не пише нищо."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def _open(path: Any, schema: str, timeout: float, wal: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=timeout)
    try:
        if wal:
            _set_wal(conn, timeout)
        if schema:
            conn.executescript(schema)
    except BaseException:
        conn.close()
        raise
    return conn


def _free_name(path: Path) -> Path:
    """Номерирано, както skills_manager._free_corrupt_name: втора повреда не
    презаписва първата."""
    kept = path.with_name(path.name + ".corrupt")
    for n in range(2, 100):
        if not kept.exists():
            break
        kept = path.with_name(f"{path.name}.corrupt.{n}")
    return kept


def set_aside(path: Path) -> Path:
    """Мести нечетимата база на свободно *.corrupt[.N] име и връща него.
    -wal/-shm тръгват с нея и ПЪРВИ: стар WAL до нова база SQLite би
    приложил върху нея при отваряне."""
    kept = _free_name(path)
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            side.replace(kept.with_name(kept.name + suffix))
    path.replace(kept)
    return kept


def connect(path: Any, schema: str = "", *, timeout: float = 5.0,
            wal: bool = False) -> sqlite3.Connection:
    """sqlite3.connect + схемата. Нечетим файл отива настрани като
    *.corrupt[.N] с едно предупреждение и базата се създава наново; ако и
    това не стане (права, зает файл на Windows) — грешката е същата като
    досега."""
    try:
        return _open(path, schema, timeout, wal)
    except sqlite3.DatabaseError as e:
        if not _unreadable(e):
            raise
        first = e
    from genesis_agent.file_lock import locked
    db = Path(path)
    with locked(db):
        # Под заключване и с нова проба: друг процес може вече да я е
        # сложил настрани и да е създал нова — нея не местим.
        try:
            return _open(path, schema, timeout, wal)
        except sqlite3.DatabaseError as e:
            if not _unreadable(e):
                raise
        try:
            kept = set_aside(db)
        except OSError:
            log.warning("%s е нечетима (%s) и не можа да се премести — паметта в нея "
                        "не работи", db.name, first)
            raise first from None
        log.warning("%s беше нечетима (%s) — запазена като %s, започвам нова",
                    db.name, first, kept.name)
        return _open(path, schema, timeout, wal)
