"""
genesis_agent.file_lock — an exclusive lock between processes, on a sidecar
`<name>.lock` file.

A chat, a mission, `genesis serve` and a bench run can all write the same JSON
file (skills.json, provider_stats.json) at once; a threading.Lock serializes
only the threads of one process, and a read-modify-write from two processes
loses the other one's entries (audit 2026-10-07: 3 processes × 25 saved skills
→ 75 files, 27 index entries). Best effort: when the lock cannot be taken
(read-only folder, odd filesystem) the block runs unlocked, as before.
"""
from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold `<path>.lock` exclusively for the duration of the block."""
    fh = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path.with_name(path.name + ".lock"), "a+b")  # noqa: SIM115
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except Exception:
        pass
    try:
        yield
    finally:
        if fh is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            fh.close()
