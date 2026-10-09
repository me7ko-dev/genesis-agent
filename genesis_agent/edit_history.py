"""
genesis_agent.edit_history — `/undo`: the files a chat turn changed, back to
how they were before it (Claude Code's checkpoints / rewind).

Before the first WRITE_FILE / EDIT_FILE of a path in a turn, its content (or
that it did not exist) is kept in memory. `/undo` puts back the last turn
that changed anything; again → the turn before. What a command changed in
the project folder (RUN_CMD `rm`, a generator, a skill driver) is tracked by
checkpoints.py (git snapshots, 2026-10-09); outside the folder it is not.

In memory only, for this chat: the size is bounded by what the turns write,
and a file over _MAX_BYTES is not kept (its undo is refused, not faked).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis_agent.checkpoints import Survey

_MAX_BYTES = 2_000_000
_MAX_TURNS = 30


@dataclass
class _FromCommand:
    """A file a command touched first in the turn: its blob before (in the
    checkpoint store), and whether it existed then at all."""
    old: str
    old_mode: str
    existed: bool


@dataclass
class _Window:
    before: str
    survey: Survey


@dataclass
class _Turn:
    label: str
    # Първото докосване на файла в хода носи състоянието отпреди хода:
    # WRITE/EDIT → тук, команда → в `commands` (одит 2026-10-09: снимка за
    # целия ход връщаше съдържанието след командата, ако EDIT идваше после).
    before: dict[Path, bytes | None] = field(default_factory=dict)
    too_big: list[Path] = field(default_factory=list)
    commands: dict[Path, _FromCommand] = field(default_factory=dict)
    # Какво е било накрая (blob id или None = няма файл): друг, който го е
    # пипнал след хода, не губи промяната си при /undo.
    after: dict[Path, str | None] = field(default_factory=dict)
    workspace: Path | None = None
    window: _Window | None = None
    # Снимката се прави чак когато процесът тръгва (sandbox.before_exec), след
    # одобрението; дотогава командата само „чака“ (одит 2026-10-09).
    waiting: Path | None = None
    # Записи от WRITE/EDIT вътре в команда (DELEGATE): снимката ги покрива,
    # освен ако са извън нея (.gitignore, node_modules, извън проекта).
    in_window: set[Path] = field(default_factory=set)
    depth: int = 0
    tried: bool = False
    note: str = ""
    # Команда, чиято снимка след нея не стана: ходът пак е „ход с промени“,
    # иначе /undo тихо връщаше ПРЕДИШНИЯ ход (одит 2026-10-09).
    lost: str = ""

    def changed(self) -> bool:
        return bool(self.before or self.too_big or self.commands or self.lost)


_turns: list[_Turn] = []
_current: _Turn | None = None
_last_note = ""     # защо командите на последния ход с команди не са проследени


def begin_turn(label: str) -> None:
    """A new operator turn starts; its first touch of each file is recorded."""
    global _current
    _current = _Turn(label=label.strip().splitlines()[0][:80] if label.strip() else "")


def end_turn() -> None:
    global _current, _last_note
    turn = _current
    if turn is not None and turn.window is not None:
        turn.depth = 1
        after_command()
    _current = None
    if turn is None:
        return
    if turn.tried:
        _last_note = turn.note
    if turn.changed():
        _turns.append(turn)
        del _turns[:-_MAX_TURNS]


def before_command(workspace: Path, *, at_exec: bool = False) -> None:
    """A tool that can change files nobody named (a command, a skill, an MCP
    tool) is about to run: snapshot the folder before it and again when it is
    done (after_command) — only what changed in between is the tool's. With
    `at_exec` the first snapshot waits for the process to start (after the
    operator approved it). Only inside a chat turn, like record(). A tool
    inside a tool (DELEGATE) is covered by the outer window."""
    turn = _current
    if turn is None:
        return
    turn.depth += 1
    if turn.depth > 1:
        return
    turn.tried = True
    try:
        ws = Path(workspace).resolve()
    except OSError as e:
        turn.note = str(e)
        return
    if turn.workspace is not None and ws != turn.workspace:
        turn.note = "работната папка се смени насред хода"
        return
    turn.workspace = ws
    if at_exec:
        from genesis_agent import sandbox
        turn.waiting = ws
        sandbox.before_exec = _exec_starts
        return
    _open_window(turn, ws)


def _exec_starts() -> None:
    turn = _current
    if turn is not None and turn.waiting is not None and turn.window is None:
        ws, turn.waiting = turn.waiting, None
        _open_window(turn, ws)


def _open_window(turn: _Turn, ws: Path) -> None:
    from genesis_agent import checkpoints
    try:
        why = checkpoints.why_not(ws)
        if why:
            turn.note = why
            return
        found = checkpoints.survey(ws)
        if found.over:
            turn.note = found.over
            return
        tree = checkpoints.snapshot(ws, found.big)
        if tree is None:
            turn.note = "снимката на проекта не стана"
            return
        turn.window = _Window(tree, found)
    except Exception as e:
        turn.note = str(e)


def after_command() -> None:
    """The tool is done: what changed since before_command is its doing."""
    turn = _current
    if turn is None or turn.depth == 0:
        return
    turn.depth -= 1
    if turn.depth > 0:
        return
    turn.waiting = None       # фонова команда / отказана: процес не е тръгнал
    if turn.window is None or turn.workspace is None:
        return
    window, turn.window = turn.window, None
    recorded, turn.in_window = turn.in_window, set()
    from genesis_agent import checkpoints
    try:
        # Наново: какво командата създаде над границите (300 MB файл влизаше
        # в хранилището за 12 s — одит 2026-10-09).
        now = checkpoints.survey(turn.workspace)
        tree = None if now.over else checkpoints.snapshot(
            turn.workspace, sorted(set(window.survey.big) | set(now.big)))
        if tree is None:
            raise OSError(now.over or "снимката след командата не стана")
        found = checkpoints.changes(turn.workspace, window.before, tree)
    except Exception as e:
        turn.note = turn.lost = f"промените от команда не могат да се върнат ({e})"
        return
    existed = window.survey.files
    for ch in found:
        path = turn.workspace / ch.path
        if path in recorded:
            turn.before.pop(path, None)   # снимката отпреди командата е по-рано
        if path not in turn.before and path not in turn.too_big and path not in turn.commands:
            turn.commands[path] = _FromCommand(
                ch.old, ch.old_mode, existed=ch.status != "A" or ch.path in existed)
        turn.after[path] = None if ch.new == checkpoints.ZERO or ch.status == "D" else ch.new


def record(path: Path) -> None:
    """Keep `path` as it is now, once per turn, before it is changed. Only
    inside a chat turn: `genesis fix`, missions and the orchestrator use the
    same tools, and their snapshots piled up for the life of the process with
    nothing to undo them (audit 2026-10-08)."""
    if _current is None:
        return
    try:
        path = Path(path).resolve()
    except OSError:
        return
    if path in _current.before or path in _current.too_big or path in _current.commands:
        return
    if _current.window is not None:
        # Вътре в команда (DELEGATE): ако снимката покрие файла, тя печели в
        # after_command; ако не (.gitignore, извън проекта) — остава този запис.
        _current.in_window.add(path)
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


def forget_if_unchanged(path: Path) -> None:
    """After the tool: a refused or failed write changed nothing — it is not
    a change for /undo (an undo of such a turn did nothing and the operator
    had to /undo again). Otherwise: remember what it left."""
    if _current is None:
        return
    try:
        path = Path(path).resolve()
    except OSError:
        return
    try:
        now = path.read_bytes() if path.is_file() else None
    except OSError:
        return
    if path in _current.before and now == _current.before[path]:
        del _current.before[path]
        return
    if path in _current.before or path in _current.commands:
        from genesis_agent import checkpoints
        _current.after[path] = None if now is None else checkpoints.blob_id(now)


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
    for path, cmd in turn.commands.items():
        if not cmd.existed:
            lines.append(f"изтрий {path} (създаден от команда)" if path.exists() else "")
        else:
            lines.append(f"върни {path} (променен от команда)")
    if turn.lost:
        lines.append(f"не може: {turn.lost}")
    return [ln for ln in lines if ln]


def untracked_note() -> str:
    """Why the commands of the last turn with commands were not tracked, if so."""
    if _current is not None and _current.tried:
        return _current.note
    return _last_note


def _last() -> _Turn | None:
    if _current is not None and _current.changed():
        end_turn()
    return _turns[-1] if _turns else None


def _moved_since(turn: _Turn, path: Path) -> bool:
    """Someone changed the file after the turn — its change stays."""
    if path not in turn.after:
        return False
    from genesis_agent import checkpoints
    try:
        return checkpoints.current_id(path) != turn.after[path]
    except OSError:
        return True


def undo() -> tuple[str, list[str]]:
    """Restore the last turn that changed files. (its label, what was done)."""
    turn = _last()
    if turn is None:
        return "", []
    _turns.pop()
    done = []
    for path, data in turn.before.items():
        try:
            if _led_out(turn, path):
                done.append(f"⚠ {path} (води извън проекта — оставен)")
            elif _moved_since(turn, path):
                done.append(f"⚠ {path} (променен след хода — оставен както е)")
            elif data is None:
                if path.is_file():
                    path.unlink()
                    done.append(f"изтрит {path}")
            elif not path.exists() or path.read_bytes() != data:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                done.append(f"върнат {path}")
        except OSError as e:
            done.append(f"❌ {path}: {e}")
    if turn.commands and turn.workspace is not None:
        done += _undo_commands(turn, turn.workspace)
    done += [f"❌ не е пазен (твърде голям): {p}" for p in turn.too_big]
    if turn.lost:
        done.append(f"❌ {turn.lost}")
    return turn.label, done


def _led_out(turn: _Turn, path: Path) -> bool:
    """Файл в проекта, до който сега се стига през връзка навън (команда е
    сменила `src/` с връзка) — не пишем там (одит 2026-10-09)."""
    ws = turn.workspace
    if ws is None:
        return False
    from genesis_agent import checkpoints
    try:
        path.relative_to(ws)
    except ValueError:
        return False          # извън проекта по замисъл (EDIT на абсолютен път)
    return not checkpoints.inside(ws, path)


def _undo_commands(turn: _Turn, ws: Path) -> list[str]:
    from genesis_agent import checkpoints
    done = []
    for path, cmd in turn.commands.items():
        try:
            if not checkpoints.inside(ws, path):
                done.append(f"⚠ {path} (води извън проекта — оставен)")
            elif _moved_since(turn, path):
                done.append(f"⚠ {path} (променен след хода — оставен както е)")
            elif not cmd.existed:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
                    done.append(f"изтрит {path}")
            elif cmd.old == checkpoints.ZERO or cmd.old_mode == "120000":
                # Съществувал е, но снимката не го е видяла (.gitignore, нечетим):
                # нищо за връщане и в никакъв случай — триене (одит 2026-10-09).
                done.append(f"⚠ {path} (не е бил в снимката — оставен)")
            elif path.is_symlink() or path.is_dir():
                done.append(f"⚠ {path} (сега е папка или връзка — оставен)")
            else:
                data = checkpoints.blob(ws, cmd.old)
                same = path.is_file() and path.read_bytes() == data
                if not same:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                moded = _restore_mode(path, cmd.old_mode)
                if not same or moded:
                    done.append(f"върнат {path}")
        except Exception as e:
            done.append(f"❌ {path}: {e}")
    return done


def _restore_mode(path: Path, mode: str) -> bool:
    """Изпълнимият бит както преди командата (`chmod +x` се листваше, но не
    се връщаше — одит 2026-10-09). На Windows няма такъв бит."""
    import os
    if os.name == "nt":
        return False
    current = path.stat().st_mode
    want_x = mode == "100755"
    if want_x == bool(current & 0o100):
        return False
    path.chmod(current | 0o111 if want_x else current & ~0o111)
    return True


def clear() -> None:
    global _current, _last_note
    _turns.clear()
    _current = None
    _last_note = ""
