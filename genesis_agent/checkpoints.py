"""
genesis_agent.checkpoints — what a command changed, for /undo.

edit_history keeps a file before WRITE_FILE/EDIT_FILE touches it. A command
(`RUN_CMD sed -i …`, `rm -r build`, a code generator, a skill driver) changes
files nobody named, so it was not undoable: /undo said "RUN_CMD is not
tracked". Here the project is snapshotted with git — a separate, hidden
repository under ~/.genesis/checkpoints/, never the project's own .git —
right before and right after each such tool, and edit_history keeps what
changed in between (2026-10-09).

Git, because it hashes only what changed since the last snapshot (its index
keeps file stats) and stores each version once. No git, the home folder, the
root of a disk or a tree over the limits → no snapshot, and /undo says so.

Audit 2026-10-09 (measured): one snapshot for the whole turn deleted files
that .gitignore hid at the start, overwrote edits made after a command and
reverted what the operator saved meanwhile; 1 GB took 42 s and left an
index.lock that broke the folder for good; nothing pruned the store. Hence
the window per command, the list of what existed before it, the size caps,
the lock cleanup and the prune once per process.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILES = 20_000
MAX_TOTAL_MB = 200
MAX_FILE_MB = 20
_TIMEOUT = 60
ZERO = "0" * 40
# Every file attribute of the project off: a `filter=lfs` in .gitattributes
# would otherwise run git-lfs (or any configured clean filter) on our copy,
# and `text`/`eol` would store other bytes than the ones on disk.
_ATTRIBUTES = "* -text -eol -filter -ident -diff -merge -working-tree-encoding\n"
# Only at the root: `pkg/build/builder.py` or `backend/env/settings.py` are
# code (the same rule as repo_agent._SKIP_ONLY_AT_TOP); deeper down they were
# silently not undone (audit 2026-10-09).
_ONLY_AT_TOP = {"build", "dist", "env", "target", "vendor"}
_pruned: set[Path] = set()


def skipped_dirs() -> list[str]:
    from genesis_agent.repo_map import _SKIP_DIRS
    return sorted(_SKIP_DIRS)


def _skip(name: str, depth: int) -> bool:
    from genesis_agent.repo_map import _SKIP_DIRS
    return name in _SKIP_DIRS and (depth == 0 or name not in _ONLY_AT_TOP)


@dataclass
class Survey:
    """What is in the folder right before a command: every file (ignored or
    unreadable ones too — git does not see those) and the ones too big."""
    files: set[str] = field(default_factory=set)
    big: list[str] = field(default_factory=list)
    total: int = 0
    over: str = ""


def survey(workspace: Path) -> Survey:
    out = Survey()
    limit = MAX_FILE_MB * 1024 * 1024
    for dirpath, dirnames, filenames in os.walk(workspace):
        rel_dir = Path(dirpath).relative_to(workspace)
        depth = len(rel_dir.parts)
        dirnames[:] = [d for d in dirnames if not _skip(d, depth)]
        for name in filenames:
            rel = (rel_dir / name).as_posix()
            out.files.add(rel)
            try:
                size = os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
            if size > limit:
                out.big.append(rel)
            else:
                out.total += size
        if len(out.files) > MAX_FILES:
            out.over = f"над {MAX_FILES} файла"
            return out
        if out.total > MAX_TOTAL_MB * 1024 * 1024:
            out.over = f"над {MAX_TOTAL_MB} MB"
            return out
    return out


@dataclass
class Change:
    path: str          # relative to the workspace, with "/"
    status: str        # A (created), M (changed), D (deleted), T (type)
    old: str           # blob before (ZERO when created)
    new: str           # blob after (ZERO when deleted)
    old_mode: str = "100644"


def _store(workspace: Path) -> Path:
    from genesis_agent import paths
    key = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:16]
    return Path(paths.GENESIS_HOME) / "checkpoints" / key


def _git(workspace: Path, *args: str) -> bytes:
    store = _store(workspace)
    env = {**os.environ, "GIT_DIR": str(store), "GIT_WORK_TREE": str(workspace),
           "GIT_INDEX_FILE": str(store / "index"), "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull}
    for var in ("GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR"):
        env.pop(var, None)
    try:
        proc = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
             "-c", "core.quotepath=off", "-c", "core.fsmonitor=false", "-c", "gc.auto=0",
             "-c", "core.hooksPath=" + os.devnull, *args],
            cwd=str(workspace), env=env, capture_output=True, timeout=_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        # Убитият git оставя index.lock и всяка следваща снимка на папката
        # пада завинаги (одит 2026-10-09). Ключалката е наша — махаме я.
        try:
            (store / "index.lock").unlink()
        except OSError:
            pass
        raise
    if proc.returncode != 0:
        raise OSError(proc.stderr.decode("utf-8", "replace").strip()[:300] or f"git {args[0]}")
    return proc.stdout


def why_not(workspace: Path) -> str | None:
    """Why this folder is never snapshotted — or None."""
    try:
        ws = Path(workspace).resolve()
    except OSError as e:
        return str(e)
    if ws == Path.home().resolve() or ws.parent == ws:
        return "работната папка е домашната папка или коренът на диска"
    if not ws.is_dir():
        return "няма такава папка"
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return "няма git"
    return None


def _escape(rel: str) -> str:
    out = "".join("\\" + c if c in "*?[]\\" else c for c in rel)
    return "/" + ("\\" + out if out[:1] in "#!" else out)


def _prepare(workspace: Path, big: list[str]) -> None:
    store = _store(workspace)
    if not (store / "HEAD").is_file():
        store.mkdir(mode=0o700, parents=True, exist_ok=True)
        _git(workspace, "init", "-q")
    (store / "info").mkdir(exist_ok=True)
    from genesis_agent.repo_map import _SKIP_DIRS
    lines = [f"/{d}/" if d in _ONLY_AT_TOP else f"{d}/" for d in sorted(_SKIP_DIRS)]
    lines += [_escape(rel) for rel in big]   # над MAX_FILE_MB — не се пази
    (store / "info" / "exclude").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (store / "info" / "attributes").write_text(_ATTRIBUTES, encoding="utf-8")
    lock = store / "index.lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > 2 * _TIMEOUT:
            lock.unlink()
    except OSError:
        pass
    if store not in _pruned:
        # Историята за /undo е само в паметта на процеса: всичко отпреди него
        # в хранилището е боклук (5 хода по 50 MB → +250 MB, нищо не чистеше).
        _pruned.add(store)
        try:
            _git(workspace, "prune", "--expire=now")
        except (OSError, subprocess.SubprocessError):
            pass


def snapshot(workspace: Path, big: list[str] | None = None) -> str | None:
    """The tree of the folder as it is now (or None)."""
    ws = Path(workspace).resolve()
    try:
        _prepare(ws, big or [])
        try:
            _git(ws, "add", "-A", "--ignore-errors", ".")
        except OSError:
            pass   # нечетим файл: --ignore-errors пак записва останалите
        return _git(ws, "write-tree").decode("ascii").strip() or None
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None


def changes(workspace: Path, before: str, after: str) -> list[Change]:
    """Files that differ between two snapshots (submodules left out)."""
    ws = Path(workspace).resolve()
    if before == after:
        return []
    raw = _git(ws, "diff-tree", "-r", "-z", "--no-renames", "--raw", before, after)
    out: list[Change] = []
    parts = raw.split(b"\0")
    i = 0
    while i + 1 < len(parts):
        meta, path = parts[i].decode("ascii", "replace"), parts[i + 1].decode("utf-8", "surrogateescape")
        i += 2
        fields = meta.lstrip(":").split()
        if len(fields) < 5:
            continue
        old_mode, new_mode, old, new, status = fields[:5]
        if "160000" in (old_mode, new_mode):
            continue
        out.append(Change(path, status[0], old, new, old_mode))
    return out


def blob(workspace: Path, sha: str) -> bytes:
    return _git(Path(workspace).resolve(), "cat-file", "blob", sha)


def blob_id(data: bytes) -> str:
    """git's id of these bytes — compared with what a snapshot saw."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def current_id(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    return blob_id(path.read_bytes())


def inside(workspace: Path, path: Path) -> bool:
    """`path` stays in the folder even through symlinked parents — a command
    could replace `src/` with a link outside, and undo wrote there (audit)."""
    try:
        ws = Path(workspace).resolve()
        parent = path.parent.resolve()
    except OSError:
        return False
    return parent == ws or ws in parent.parents
