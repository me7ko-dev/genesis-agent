"""
genesis_agent.edit_history — `/undo`: the files a chat turn changed, back to
how they were before it (Claude Code's checkpoints / rewind).

Before the first WRITE_FILE / EDIT_FILE of a path in a turn, its content (or
that it did not exist) is kept in memory. `/undo` puts back the last turn
that changed anything; again → the turn before. What RUN_CMD changed (a
`rm`, a `pip install`, a generated file) is not tracked — /undo says so.

In memory only, for this chat: the size is bounded by what the turns write,
and a file over _MAX_BYTES is not kept (its undo is refused, not faked).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_MAX_BYTES = 2_000_000
_MAX_TURNS = 30


@dataclass
class _Turn:
    label: str
    before: dict[Path, bytes | None] = field(default_factory=dict)
    too_big: list[Path] = field(default_factory=list)


_turns: list[_Turn] = []
_current: _Turn | None = None


def begin_turn(label: str) -> None:
    """A new operator turn starts; its first touch of each file is recorded."""
    global _current
    _current = _Turn(label=label.strip().splitlines()[0][:80] if label.strip() else "")


def end_turn() -> None:
    global _current
    if _current is not None and (_current.before or _current.too_big):
        _turns.append(_current)
        del _turns[:-_MAX_TURNS]
    _current = None


def record(path: Path) -> None:
    """Keep `path` as it is now, once per turn, before it is changed."""
    global _current
    if _current is None:
        _current = _Turn(label="")
    try:
        path = Path(path).resolve()
    except OSError:
        return
    if path in _current.before or path in _current.too_big:
        return
    if not path.exists():
        _current.before[path] = None
        return
    if not path.is_file():
        return
    try:
        if path.stat().st_size > _MAX_BYTES:
            _current.too_big.append(path)
            return
        _current.before[path] = path.read_bytes()
    except OSError:
        return


def pending() -> list[str]:
    """What /undo would do now, one line per file."""
    turn = _last()
    if turn is None:
        return []
    lines = []
    for path, data in turn.before.items():
        if data is None:
            lines.append(f"изтрий {path} (създаден в този ход)" if path.exists() else "")
        else:
            lines.append(f"върни {path}")
    lines += [f"не може: {p} (над {_MAX_BYTES // 1_000_000} MB)" for p in turn.too_big]
    return [ln for ln in lines if ln]


def _last() -> _Turn | None:
    if _current is not None and (_current.before or _current.too_big):
        end_turn()
    return _turns[-1] if _turns else None


def undo() -> tuple[str, list[str]]:
    """Restore the last turn that changed files. (its label, what was done)."""
    turn = _last()
    if turn is None:
        return "", []
    _turns.pop()
    done = []
    for path, data in turn.before.items():
        try:
            if data is None:
                if path.is_file():
                    path.unlink()
                    done.append(f"изтрит {path}")
            elif not path.exists() or path.read_bytes() != data:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                done.append(f"върнат {path}")
        except OSError as e:
            done.append(f"❌ {path}: {e}")
    done += [f"❌ не е пазен (твърде голям): {p}" for p in turn.too_big]
    return turn.label, done


def clear() -> None:
    global _current
    _turns.clear()
    _current = None
