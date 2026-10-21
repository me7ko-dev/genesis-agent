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

`/rewind` (2026-10-15) is the same undo, turn after turn, back to the
operator message the turn answered (`mark`): no second mechanism.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from genesis_agent.checkpoints import Survey

log = logging.getLogger("genesis.edit_history")

_MAX_BYTES = 2_000_000
_MAX_TURNS = 30
# Съобщенията, на които е отговорено в този процес (и без промени по файлове):
# /rewind казва честно, ако съобщението е от предишно пускане (/resume, -c).
_MAX_KNOWN = 300


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
    # Съобщението на оператора, на което ходът отговаря (същият dict като в
    # историята — по него /rewind намира ходовете след точката).
    marker: Any = None

    def changed(self) -> bool:
        return bool(self.before or self.too_big or self.commands or self.lost)


_turns: list[_Turn] = []
_current: _Turn | None = None
_last_note = ""     # защо командите на последния ход с команди не са проследени
_known: list[Any] = []
# Ходове, паднали от _turns заради _MAX_TURNS: (съобщение, етикет). /rewind до
# съобщение преди тях казва, че техните промени не се връщат — 32 хода и
# /rewind 1 оставяха g00/g01 без дума (одит 2026-10-21).
_dropped: list[tuple[Any, str]] = []
_cap_note = ""      # бележката за това, сметната от turns_since за rewind_plan
_LEFT = "променен след хода — оставен както е"
# Ходове с промени, записани от началото на процеса. Не len(_turns): той спира
# на _MAX_TURNS, а `genesis -p` пита „записа ли ТОЗИ ход нещо“ (2026-10-15).
_recorded = 0


def begin_turn(label: str) -> None:
    """A new operator turn starts; its first touch of each file is recorded."""
    global _current
    _current = _Turn(label=label.strip().splitlines()[0][:80] if label.strip() else "")


def end_turn() -> None:
    global _current, _last_note, _recorded
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
        _recorded += 1
        _dropped.extend((t.marker, t.label) for t in _turns[:-_MAX_TURNS] if t.marker is not None)
        del _dropped[:-_MAX_KNOWN]
        del _turns[:-_MAX_TURNS]


def recorded() -> int:
    """Колко хода с промени са записани досега (расте, не се връща назад)."""
    return _recorded


def last_changes() -> dict[str, list[str]]:
    """Файловете на последния записан ход: създадени, променени, изтрити —
    от WRITE_FILE/EDIT_FILE и от командите в папката на проекта. За отчета на
    `genesis -p --output-format json`; само чете."""
    out: dict[str, list[str]] = {"created": [], "edited": [], "deleted": []}
    if not _turns:
        return out
    turn = _turns[-1]
    seen: list[tuple[Path, bool]] = [(p, data is not None) for p, data in turn.before.items()]
    seen += [(p, True) for p in turn.too_big]
    seen += [(p, cmd.existed) for p, cmd in turn.commands.items()]
    for path, existed in seen:
        now = path.exists()
        kind = ("edited" if now else "deleted") if existed else ("created" if now else "")
        if kind and str(path) not in out[kind]:
            out[kind].append(str(path))
    return out


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


def background_command() -> None:
    """RUN_BG / RUN_CMD background: процесът пише и след хода, затова не се
    снима. Ходът пак е „с промени“ — /undo казва, че фоновите промени не се
    връщат, вместо тихо да върне предишния ход (трети одит 2026-10-09)."""
    turn = _current
    if turn is not None and not turn.lost:
        turn.lost = "фонова команда — каквото промени, не се връща"


def _exec_starts() -> None:
    turn = _current
    if turn is not None and turn.waiting is not None and turn.window is None:
        ws, turn.waiting = turn.waiting, None
        from genesis_agent import sandbox
        _open_window(turn, ws, stop=sandbox._stop_requested)


def _open_window(turn: _Turn, ws: Path, stop: Callable[[], bool] | None = None) -> None:
    """`stop` — само за снимката точно преди процеса (sandbox.before_exec).
    Стоп по време на нея = sandbox няма да пусне командата (гледа същото
    веднага след куката), значи снимката не трябва: без прозорец и снимката
    „след“ не се прави. Windows CI 2026-10-15 (job 116436795903): Стоп дойде
    по време на тази снимка, тя довърши своите 7 git процеса, после 4 за
    „след“ на команда, която не е тръгнала — 6.9 s до края на хода. Със
    същото на Linux (git по 0.7 s): 6.79 s преди, под 1 s след поправката."""
    from genesis_agent import checkpoints

    def stopped() -> bool:
        return stop is not None and stop()
    try:
        why = checkpoints.why_not(ws)
        if why:
            turn.note = why
            return
        if stopped():
            return
        found = checkpoints.survey(ws)
        if found.over:
            turn.note = found.over
            return
        tree = checkpoints.snapshot(ws, found.big, stop=stop)
        if stopped():
            return
        if tree is None:
            turn.note = "снимката на проекта не стана"
            return
        turn.window = _Window(tree, found)
    except Exception as e:
        log.debug("снимката преди командата не стана", exc_info=True)
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
        # в хранилището за 12 s — одит 2026-10-09). Над границата на папката —
        # поне вече пазените файлове (`add -u`), не загуба на целия ход.
        now = checkpoints.survey(turn.workspace)
        tree = checkpoints.snapshot(turn.workspace, sorted(set(window.survey.big) | set(now.big)),
                                    tracked_only=bool(now.over))
        if tree is None:
            raise OSError("снимката след командата не стана")
        found = checkpoints.changes(turn.workspace, window.before, tree)
    except Exception as e:
        log.debug("снимката след командата не стана", exc_info=True)
        turn.note = turn.lost = f"промените от команда не могат да се върнат ({e})"
        return
    existed = window.survey.files
    # Вложено git хранилище (lib/.git, подмодул): снимката не вижда вътре.
    # Промяна там правеше хода „без промени“ и /undo тихо връщаше ПРЕДИШНИЯ
    # (одит 2026-10-21) — същият клас като файла над 20 MB по-долу: ходът се
    # записва с бележка, нищо не се лъже, че е върнато.
    nested = checkpoints.nested_changed(window.survey, now)
    if nested is not None:
        _lose_nested(turn, turn.workspace / nested)
    for ch in found:
        path = turn.workspace / ch.path
        if ch.is_repo():
            _lose_nested(turn, path)
            continue
        if path in recorded:
            # Снимката отпреди командата е по-рано от записа вътре в нея.
            turn.before.pop(path, None)
            if path in turn.too_big:
                turn.too_big.remove(path)
        if path not in turn.before and path not in turn.too_big and path not in turn.commands:
            turn.commands[path] = _FromCommand(
                ch.old, ch.old_mode, existed=ch.status != "A" or ch.path in existed)
        turn.after[path] = None if ch.new == checkpoints.ZERO or ch.status == "D" else ch.new
    # Над 20 MB не се пази съдържание, но създаденият от командата файл пак
    # се трие при /undo, а промененият — казва се. Без това ходът оставаше
    # „без промени“ и /undo връщаше ПРЕДИШНИЯ ход (одит 2026-10-09).
    for rel in sorted(set(now.big) - set(window.survey.big)):
        path = turn.workspace / rel
        if path in turn.before or path in turn.commands or path in turn.too_big:
            continue
        if rel in existed:
            turn.too_big.append(path)
        else:
            turn.commands[path] = _FromCommand(checkpoints.ZERO, "100644", existed=False)
            turn.after[path] = _big_id(path)


def _lose_nested(turn: _Turn, repo: Path) -> None:
    if not turn.lost:
        turn.lost = f"вложено git хранилище {repo} — промените в него не се връщат"


def _big_id(path: Path) -> str | None:
    """Отпечатък на голям файл без да се чете целият в паметта — за „пипнат
    след хода?“ при /undo: размер и време на промяна."""
    try:
        st = path.stat()
    except OSError:
        return None
    return f"big:{st.st_size}:{st.st_mtime_ns}"


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
            lines.append(f"изтрий {path} (създаден от команда)"
                         if path.exists() or path.is_symlink() else "")
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
    expected = turn.after[path]
    if expected is not None and expected.startswith("big:"):
        return _big_id(path) != expected
    from genesis_agent import checkpoints
    try:
        return checkpoints.current_id(path) != expected
    except OSError:
        return True


def undo() -> tuple[str, list[str]]:
    """Restore the last turn that changed files. (its label, what was done)."""
    turn = _last()
    if turn is None:
        return "", []
    _turns.pop()
    return turn.label, [line for _path, line in _undo_turn(turn)]


def _undo_turn(turn: _Turn, skip: frozenset[Path] = frozenset()) -> list[tuple[Path | None, str]]:
    """Връща един ход; всеки ред — с файла си, за да може /rewind да сглоби
    няколко хода в един отчет по файл. `skip` — файлове, които по-нов ход в
    същия /rewind е оставил („променен след хода“): по-старият ход не ги пипа,
    както обещава rewind_plan (одит 2026-10-21: ход 1 → v1, ход 2 → v2,
    операторът върна v1 на ръка — ход 1 „съвпадаше“ и презаписваше с v0)."""
    done: list[tuple[Path | None, str]] = []
    for path, data in turn.before.items():
        try:
            if _led_out(turn, path):
                done.append((path, f"⚠ {path} (води извън проекта — оставен)"))
            elif path in skip or _moved_since(turn, path):
                done.append((path, f"⚠ {path} ({_LEFT})"))
            elif data is None:
                if path.is_file():
                    path.unlink()
                    done.append((path, f"изтрит {path}"))
            elif not path.exists() or path.read_bytes() != data:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                done.append((path, f"върнат {path}"))
        except OSError as e:
            done.append((path, f"❌ {path}: {e}"))
    if turn.commands and turn.workspace is not None:
        done += _undo_commands(turn, turn.workspace, skip)
    done += [(p, f"❌ не е пазен (твърде голям): {p}") for p in turn.too_big]
    if turn.lost:
        done.append((None, f"❌ {turn.lost}"))
    return done


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


def _undo_commands(turn: _Turn, ws: Path,
                   skip: frozenset[Path] = frozenset()) -> list[tuple[Path | None, str]]:
    from genesis_agent import checkpoints
    done: list[tuple[Path | None, str]] = []
    for path, cmd in turn.commands.items():
        try:
            if not checkpoints.inside(ws, path):
                done.append((path, f"⚠ {path} (води извън проекта — оставен)"))
            elif path in skip or _moved_since(turn, path):
                done.append((path, f"⚠ {path} ({_LEFT})"))
            elif not cmd.existed:
                # Връзка, направена от командата, се маха като връзка (unlink
                # не следва към целта); pending я обещаваше, а тук оставаше
                # (одит 2026-10-21). Папка — не.
                if path.is_symlink() or path.is_file():
                    path.unlink()
                    done.append((path, f"изтрит {path}"))
            elif cmd.old == checkpoints.ZERO or cmd.old_mode == "120000":
                # Съществувал е, но снимката не го е видяла (.gitignore, нечетим):
                # нищо за връщане и в никакъв случай — триене (одит 2026-10-09).
                done.append((path, f"⚠ {path} (не е бил в снимката — оставен)"))
            elif path.is_symlink() or path.is_dir():
                done.append((path, f"⚠ {path} (сега е папка или връзка — оставен)"))
            else:
                data = checkpoints.blob(ws, cmd.old)
                same = path.is_file() and path.read_bytes() == data
                if not same:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                moded = _restore_mode(path, cmd.old_mode)
                if not same or moded:
                    done.append((path, f"върнат {path}"))
        except Exception as e:
            log.debug("/undo не върна %s", path, exc_info=True)
            done.append((path, f"❌ {path}: {e}"))
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


# ── /rewind (2026-10-15) ─────────────────────────────────────────────────────
# Назад до избрано съобщение на оператора = /undo на всеки ход от него насам,
# най-новият пръв. Проверката „пипнат след хода?“ на /undo важи за всеки ход:
# каквото операторът е променил междувременно, остава и се казва.

def mark(message: Any) -> None:
    """Съобщението на оператора, на което текущият ход отговаря. Само първото:
    подканата на hook Stop в същия ход не е нова точка за връщане."""
    turn = _current
    if turn is None or turn.marker is not None:
        return
    turn.marker = message
    _known.append(message)
    del _known[:-_MAX_KNOWN]


def tracked(message: Any) -> bool:
    """Отговорено ли е на това съобщение в този процес — тогава промените по
    файлове след него са известни. Не — съобщението е от /resume или -c."""
    return any(m is message for m in _known)


def turns_since(cut: Sequence[Any]) -> int:
    """Колко от последните ходове /rewind връща: от първия, който отговаря на
    някое от `cut` (махнатите съобщения), до най-новия. Ход от друг разговор
    (преди /clear или /resume) не е след точката и не се пипа."""
    global _cap_note
    _last()
    _cap_note = ""
    if any(m is dropped for dropped, _label in _dropped for m in cut):
        _cap_note = (f"⚠ ходовете преди „{_turns[0].label}“ са над лимита от {_MAX_TURNS}"
                     if _turns else
                     f"⚠ ходовете до „{_dropped[-1][1]}“ са над лимита от {_MAX_TURNS}"
                     ) + " — промените им не се връщат"
    for i, turn in enumerate(_turns):
        if turn.marker is not None and any(turn.marker is m for m in cut):
            return len(_turns) - i
    return 0


def _id_now(path: Path, expected: str | None) -> str | None:
    if expected is not None and expected.startswith("big:"):
        return _big_id(path)
    from genesis_agent import checkpoints
    try:
        return checkpoints.current_id(path)
    except OSError:
        return None


def rewind_plan(count: int) -> list[str]:
    """Какво rewind(count) ще направи — по ред на файл, крайното състояние, не
    всеки ход поотделно. Пресмята същото като поредицата /undo, без да пише."""
    from genesis_agent import checkpoints
    _last()
    turns = _turns[len(_turns) - count:] if count > 0 else []
    start: dict[Path, str | None] = {}
    state: dict[Path, str | None] = {}
    left: dict[Path, str] = {}
    notes: list[str] = []
    for turn in reversed(turns):
        targets: dict[Path, str | None] = {
            path: None if data is None else checkpoints.blob_id(data)
            for path, data in turn.before.items()}
        for path, cmd in turn.commands.items():
            if path in targets:
                continue
            if not cmd.existed:
                targets[path] = None
            elif cmd.old == checkpoints.ZERO or cmd.old_mode == "120000":
                notes.append(f"⚠ {path} (не е бил в снимката — оставен)")
            else:
                targets[path] = cmd.old
        notes += [f"не може: {p} (над {_MAX_BYTES // 1_000_000} MB)" for p in turn.too_big]
        if turn.lost:
            notes.append(f"не може: {turn.lost}")
        for path, before_id in targets.items():
            expected = turn.after.get(path)
            if path not in state:
                start[path] = state[path] = _id_now(path, expected)
            if path in left:
                # Оставен от по-нов ход: по-старите не го пипат (rewind() —
                # същото през `skip`; одит 2026-10-21: иначе по-стар ход,
                # чието „след“ съвпада с ръчното връщане, го презаписваше).
                continue
            if path in turn.after and state[path] != expected:
                # Пипнат отвън след този ход: /undo го оставя.
                left.setdefault(path, turn.label)
                continue
            state[path] = before_id
    lines = []
    for path, was in start.items():
        if path in left and state[path] == was:
            lines.append(f"⚠ {path} — променен извън Genesis след „{left[path]}“; "
                         "НЕ се презаписва")
        elif path in left:
            lines.append(f"⚠ {path} — променян извън Genesis между ходовете; връщам го само "
                         "до версията отвън")
        elif state[path] == was:
            continue
        elif state[path] is None:
            lines.append(f"изтрий {path} (създаден от Genesis след тази точка)")
        elif was is None:
            lines.append(f"възстанови {path} (изтрит след тази точка)")
        else:
            lines.append(f"върни {path}")
    if _cap_note:
        notes.append(_cap_note)
    return lines + list(dict.fromkeys(notes))


def rewind(count: int) -> list[str]:
    """Връща последните `count` хода, най-новия пръв. По ред на файл: какво
    стана накрая; предупреждение или грешка по пътя — тя, не тихо „върнат“.
    Файл, който ход остави („променен след хода“), по-старите ходове не пипат
    — както rewind_plan го е казал (одит 2026-10-21)."""
    by_path: dict[Path | None, list[str]] = {}
    skip: set[Path] = set()
    for _ in range(count):
        turn = _last()
        if turn is None:
            break
        _turns.pop()
        for path, line in _undo_turn(turn, frozenset(skip)):
            by_path.setdefault(path, []).append(line)
            if path is not None and line.endswith(f"({_LEFT})"):
                skip.add(path)
    done: list[str] = []
    for lines in by_path.values():
        warned = [ln for ln in lines if ln.startswith(("⚠", "❌"))]
        done += list(dict.fromkeys(warned)) if warned else lines[-1:]
    return done


def clear() -> None:
    global _current, _last_note, _cap_note
    _turns.clear()
    _known.clear()
    _dropped.clear()
    _current = None
    _last_note = _cap_note = ""
