"""
genesis_agent.repo_agent — fix a bug in a project the agent did not write.

Everything else in this package produces NEW code: a mission writes a skill, the
orchestrator writes a module, the forge writes several in parallel. Each of
those owns its output, so the worst case is a bad file nobody was using yet.
Repairing an existing project inverts that. The code belongs to someone, it
already runs, and a wrong edit does not fail loudly — it fails later, somewhere
else, in a way that looks like a different bug.

Three mechanisms carry that difference, and they are mechanisms rather than
prompt instructions on purpose (the model in the free rotation changes call to
call; a rule only holds if the model happens to cooperate):

1. **A way back.** With `--checkpoint`, a tar snapshot of the project, taken
   before the agent touches anything and restorable with one command. Opt-in
   since 2026-09-23 (the operator wants backups only on request). Without it,
   git is the way back, and a non-git project is warned that it has none.

2. **The project's own test suite is the verdict.** Tests are run BEFORE any
   change, so a suite that was already red is not later mistaken for damage the
   agent caused, and after every round of edits. "Fixed" means the suite went
   from failing to passing; nothing else is reported as fixed.

3. **A real diff at the end.** Not a summary of what the model believes it did
   — the actual bytes that changed, so a human reviews a change set rather than
   a claim.

The loop itself is deliberately narrow: `REPAIR_TOOLS` gives it the project and
a shell and nothing else — no browser, no sub-agents, no skill library.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from genesis_agent.brain import Brain
from genesis_agent.repo_map import detect_project, repo_map
from genesis_agent.tool_schemas import REPAIR_TOOLS, load_tool_arguments

# Нарочно в HOME, а НЕ в DATA_DIR: при git checkout DATA_DIR сочи вътре в самото
# repo, а тук се пазят tar архиви на ЧУЖДИ проекти — те нямат работа в дървото на
# агента (и един ден някой ще ги комитне по невнимание).
CHECKPOINT_DIR = Path.home() / ".genesis" / "checkpoints"
# Above this a tar snapshot stops being a safety net and becomes its own
# problem (minutes of IO, gigabytes of disk). Git-backed projects are told to
# rely on git; the rest are refused rather than silently left unprotected.
_MAX_SNAPSHOT_MB = 300
_TEST_TIMEOUT = 600
_MAX_TOOL_OUTPUT = 6000
_MAX_TEST_OUTPUT = 4000

_SKIP_IN_SNAPSHOT = {
    ".git", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".venv", "venv", "env", "dist", "build", "target",
    ".next", ".tox", ".gradle", ".verify_libs",
}


@dataclass
class TestRun:
    ran: bool
    passed: bool
    output: str
    command: str = ""


@dataclass
class RepairOutcome:
    success: bool
    summary: str
    rounds: int = 0
    checkpoint: Path | None = None
    files_touched: list[str] = field(default_factory=list)
    diff: str = ""
    tests_before: TestRun | None = None
    tests_after: TestRun | None = None


# ── Checkpoints ──────────────────────────────────────────────────────────────

# Имена, които са и обичайни папки с код (`mypkg/build/builder.py`,
# `app/env/settings.py`): пропускат се само в КОРЕНА на проекта. Одит
# 2026-10-07: пропускани на всяка дълбочина, те изчезваха от снимката, а
# `--revert` пак казваше „✓ върнат“ — без да ги върне.
_SKIP_ONLY_AT_TOP = {"build", "dist", "env", "target"}


def _skip_in_snapshot(rel_parts: tuple[str, ...], root: Path | None = None) -> bool:
    for depth, part in enumerate(rel_parts):
        if part not in _SKIP_IN_SNAPSHOT:
            continue
        if depth == 0 or part not in _SKIP_ONLY_AT_TOP:
            return True
        # Вложено `backend/env/`, `rustlib/target/`: артефакт е, ако си личи —
        # venv (pyvenv.cfg), кеш (CACHEDIR.TAG) или `target` до Cargo.toml.
        if root is not None:
            here = root.joinpath(*rel_parts[:depth + 1])
            if ((here / "pyvenv.cfg").is_file() or (here / "CACHEDIR.TAG").is_file()
                    or (part == "target" and (here.parent / "Cargo.toml").is_file())):
                return True
    return False


def _tree_size_mb(root: Path) -> float:
    # os.walk с подрязване, не rglob: rglob обхождаше целия node_modules и чак
    # после го пропускаше — 2.4 s на 100k файла преди всеки `genesis fix`
    # (2026-10-09). Пропуснатата папка значи пропуснато и всичко под нея.
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # Спрямо корена: проект в `~/build/app` иначе мереше 0 MB и минаваше тавана.
        rel = Path(dirpath).relative_to(root).parts
        dirnames[:] = [d for d in dirnames if not _skip_in_snapshot((*rel, d), root)]
        for name in filenames:
            p = Path(dirpath) / name
            if _skip_in_snapshot((*rel, name), root):
                continue
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    return total / (1024 * 1024)


_SNAPSHOT_ROOT: Path | None = None


def _snapshot_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = tuple(p for p in Path(info.name).parts if p != ".")
    if _skip_in_snapshot(parts, _SNAPSHOT_ROOT):
        return None
    return info


def create_checkpoint(root: Path) -> Path:
    """Snapshot `root` so every change made afterwards can be undone."""
    root = Path(root).resolve()
    size = _tree_size_mb(root)
    if size > _MAX_SNAPSHOT_MB:
        raise RuntimeError(
            f"Проектът е {size:.0f}MB — прекалено голям за снимка "
            f"(таван {_MAX_SNAPSHOT_MB}MB). Комитни в git преди поправката: "
            "тогава `git diff` и `git checkout .` вършат същата работа."
        )
    # Снимката носи и .env на проекта: само за собственика (одит 2026-10-09 —
    # ~/.genesis ставаше 0755, а архивът 0644, четим от всеки на машината).
    try:
        from genesis_agent.paths import ensure_genesis_home
        ensure_genesis_home()
    except Exception:
        pass
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _suppress_oserror():
        os.chmod(CHECKPOINT_DIR, 0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = CHECKPOINT_DIR / f"{root.name}-{stamp}.tar.gz"
    n = 2
    while dest.exists():   # две снимки в една секунда (напр. преди --revert)
        dest = CHECKPOINT_DIR / f"{root.name}-{stamp}-{n}.tar.gz"
        n += 1
    global _SNAPSHOT_ROOT
    _SNAPSHOT_ROOT = root
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            tar.add(root, arcname=".", filter=_snapshot_filter)
    finally:
        _SNAPSHOT_ROOT = None
    meta = {
        "project": str(root),
        "created": stamp,
        "git_head": _git(root, "rev-parse", "HEAD") or "",
        "git_dirty": bool(_git(root, "status", "--porcelain")),
    }
    dest.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return dest


class _suppress_oserror:
    def __enter__(self) -> None:
        return None

    def __exit__(self, kind, *_rest) -> bool:
        return kind is not None and issubclass(kind, OSError)


_FINGERPRINT_MAX = 50_000


def _fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    """{път: (mtime_ns, размер)} на файловете в проекта — без пропусканите папки.

    Така се виждат и промените, направени с команда (`sed -i`, patch): само
    EDIT_FILE/WRITE_FILE се броеха и такава поправка излизаше „няма промяна“
    и провал, без тестовете да се пуснат пак (одит 2026-10-09)."""
    out: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        rel_dir = base.relative_to(root).parts
        dirnames[:] = [d for d in dirnames
                       if d != "__pycache__" and not _skip_in_snapshot(rel_dir + (d,), root)]
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                continue
            try:
                st = (base / name).stat()
            except OSError:
                continue
            out[(base / name).relative_to(root).as_posix()] = (st.st_mtime_ns, st.st_size)
            if len(out) >= _FINGERPRINT_MAX:
                return out
    return out


def _changed_since(root: Path, before: dict[str, tuple[int, int]]) -> list[str]:
    now = _fingerprint(root)
    return sorted(rel for rel, stamp in now.items() if before.get(rel) != stamp)


def _drop_stale_bytecode(root: Path, rels: list[str]) -> None:
    """Махa .pyc на току-що променените модули: две редакции с еднаква дължина
    в една секунда караха pytest да внесе стария код и присъдата беше грешна."""
    for rel in set(rels):
        if not rel.endswith(".py"):
            continue
        cache = (root / rel).parent / "__pycache__"
        if cache.is_dir():
            for pyc in cache.glob(f"{Path(rel).stem}.*.pyc"):
                with _suppress_oserror():
                    pyc.unlink()


def _git_raw(root: Path, *args: str) -> str:
    """Като _git, но без strip — за `-z` изход."""
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _git(root: Path, *args: str) -> str:
    """git output, or "" when this is not a repo / git is unavailable."""
    try:
        proc = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def latest_checkpoint(root: Path) -> Path | None:
    root = Path(root).resolve()
    if not CHECKPOINT_DIR.exists():
        return None
    mine = []
    for meta_path in CHECKPOINT_DIR.glob("*.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("project") == str(root):
            tar_path = meta_path.with_suffix(".gz")
            # `.with_suffix` on `foo.tar.json` yields `foo.tar.gz` — the sidecar
            # is written next to the archive, so this is the archive's path.
            if tar_path.exists():
                # По време на файла, не по име: две снимки в една секунда
                # (резервната преди --revert) иначе се подреждаха по азбука.
                try:
                    stamp = tar_path.stat().st_mtime_ns
                except OSError:
                    continue
                mine.append((meta.get("created", ""), stamp, tar_path))
    if not mine:
        return None
    return max(mine)[2]


def restore_checkpoint(root: str | Path, checkpoint: str | Path | None = None) -> str:
    """Put the project back exactly as it was when the snapshot was taken."""
    root = Path(root).resolve()
    cp = Path(checkpoint) if checkpoint else latest_checkpoint(root)
    if not cp or not cp.exists():
        return f"❌ Няма намерена снимка за {root}."
    # Каквото е направено след снимката (и от човека) се пази в нова снимка
    # преди връщането: --revert презаписваше и по-късната ръчна работа, без
    # връщане назад (одит 2026-10-09).
    try:
        backup: Path | None = create_checkpoint(root)
    except (RuntimeError, OSError):
        backup = None
    skipped: list[str] = []
    in_archive: set[str] = set()
    with tarfile.open(cp, "r:gz") as tar:
        # Член по член (одит 2026-10-07): с extractall символна връзка извън
        # проекта (`shared -> ../common`) хвърляше OutsideDestinationError
        # посред възстановяването — всичко след нея по азбучен ред оставаше
        # променено. Връзките се пресъздават такива, каквито бяха; останалото
        # минава през филтъра "data" (Python 3.12+), а грешка в един член не
        # спира другите и се казва.
        for member in tar.getmembers():
            rel = Path(member.name).as_posix().removeprefix("./")
            if rel in ("", "."):
                continue
            in_archive.add(rel)
            target = root / rel
            try:
                if member.issym():
                    # Нищо извън проекта (преглед 2026-10-07): родител, който сега
                    # е връзка навън, или `..`/абсолютно име в архива биха
                    # изтрили и заместили чужд файл.
                    if (Path(rel).is_absolute() or ".." in Path(rel).parts
                            or not target.parent.resolve().is_relative_to(root)):
                        skipped.append(f"{rel} (извън проекта)")
                        continue
                    if target.is_symlink():
                        if os.readlink(target) == member.linkname:
                            continue
                        target.unlink()
                    elif target.is_dir():
                        skipped.append(f"{rel} (сега е папка, в снимката — връзка)")
                        continue
                    elif target.exists():
                        target.unlink()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(member.linkname, target)
                    continue
                if member.isdir() and target.is_symlink():
                    skipped.append(f"{rel} (сега е връзка, в снимката — папка)")
                    continue
                try:
                    tar.extract(member, root, filter="data")  # type: ignore[call-arg]
                except TypeError:
                    tar.extract(member, root)
            except (tarfile.TarError, OSError) as e:
                skipped.append(f"{rel} ({type(e).__name__})")
    created = _created_since(root, in_archive)
    age = ""
    with _suppress_oserror():
        hours = (time.time() - cp.stat().st_mtime) / 3600
        age = f" (отпреди {hours:.0f} ч)" if hours >= 1 else " (отпреди по-малко от час)"
    lines = [f"{'✓' if not skipped else '⚠️'} {root} е върнат към снимката {cp.name}{age}"]
    if backup is not None:
        lines.append(f"  Състоянието отпреди връщането е в {backup.name} — "
                     f"`genesis fix --revert` пак го връща.")
    if skipped:
        lines.append("  НЕ върнати: " + ", ".join(skipped[:10]) + (" …" if len(skipped) > 10 else ""))
    if created:
        lines.append("  Файлове, създадени след снимката (оставени — изтрий ги, ако не трябват): "
                     + ", ".join(created[:10]) + (" …" if len(created) > 10 else ""))
    return "\n".join(lines)


def _created_since(root: Path, in_archive: set[str]) -> list[str]:
    """Файлове в проекта, които ги няма в снимката (без пропусканите папки)."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        rel_dir = base.relative_to(root).parts
        dirnames[:] = [d for d in dirnames if not _skip_in_snapshot(rel_dir + (d,), root)]
        for name in filenames:
            rel = (base / name).relative_to(root).as_posix()
            if rel not in in_archive:
                out.append(rel)
            if len(out) > 50:
                return sorted(out)
    return sorted(out)


# ── Tests ────────────────────────────────────────────────────────────────────

def run_tests(root: Path, command: str) -> TestRun:
    """Run the project's own suite through the sandbox."""
    if not command:
        return TestRun(ran=False, passed=False, output="няма открита тестова команда")
    from genesis_agent import sandbox
    res = sandbox.run_shell(command, cwd=Path(root), timeout=_TEST_TIMEOUT)
    if res.blocked:
        return TestRun(False, False, f"sandbox отказа командата: {res.stderr}", command)
    out = ((res.stdout or "") + "\n" + (res.stderr or "")).strip()
    if res.returncode == 5 and "pytest" in command:
        # pytest: „не се събра нито един тест“ — няма тестове, не падащи тестове.
        return TestRun(False, False, "pytest не намери нито един тест", command)
    if len(out) > _MAX_TEST_OUTPUT:
        # The tail carries the failure summary; the head carries collection
        # errors. Both matter, the middle rarely does.
        out = out[:_MAX_TEST_OUTPUT // 2] + "\n… [отрязано] …\n" + out[-_MAX_TEST_OUTPUT // 2:]
    return TestRun(ran=True, passed=res.returncode == 0, output=out, command=command)


# ── Diff ─────────────────────────────────────────────────────────────────────

def _diff_from_checkpoint(root: Path, checkpoint: Path, files: list[str]) -> str:
    """Unified diff of `files` against the snapshot, for non-git projects."""
    if not files:
        return ""
    out: list[str] = []
    with tarfile.open(checkpoint, "r:gz") as tar:
        for rel in sorted(set(files)):
            try:
                member = tar.extractfile(f"./{rel}")
                before = member.read().decode("utf-8", "replace") if member else ""
            except KeyError:
                before = ""  # file did not exist in the snapshot — newly created
            except (OSError, UnicodeDecodeError):
                continue
            try:
                after = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                after = ""
            if before == after:
                continue
            out.extend(difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3))
    return "".join(out)


def project_diff(root: Path, checkpoint: Path | None, files: list[str]) -> str:
    """
    Git is authoritative when available: it also catches files changed by a
    shell command, which tool-call tracking cannot see.
    """
    root = Path(root)
    if (root / ".git").exists():
        # Само пипнатите файлове: целият `git diff` показваше и некомитнатата
        # работа на човека като част от поправката (одит 2026-10-09).
        d = _git(root, "diff", "--", *files) if files else ""
        # `git diff` не вижда нови (неследени) файлове — помощен модул, нов
        # тест. Одит 2026-10-07: поправка САМО с нов файл даваше празен дифф.
        # Само новите файлове, които поправката е пипнала — не всеки неследен
        # файл в проекта (200k-редов data.csv); `-z` пази кирилицата в имената.
        untracked = set(filter(None, _git_raw(root, "ls-files", "--others", "--exclude-standard",
                                              "-z").split("\0")))
        touched = {Path(f).as_posix() for f in files}
        for rel in sorted(untracked & touched)[:20]:
            try:
                if (root / rel).stat().st_size > 200_000:
                    continue
                after = (root / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            d += ("\n" if d and not d.endswith("\n") else "") + "".join(difflib.unified_diff(
                [], after.splitlines(keepends=True), fromfile="/dev/null", tofile=f"b/{rel}"))
        if d:
            return d
    if checkpoint:
        return _diff_from_checkpoint(root, checkpoint, files)
    return ""


# ── The repair loop ──────────────────────────────────────────────────────────

_SYSTEM = """You are Genesis Agent, repairing a bug in a codebase you did not write.

The code belongs to someone else and already runs. That changes how you work:

1. UNDERSTAND BEFORE YOU EDIT. Call REPO_MAP, then SEARCH_CODE to find the
   relevant code, then READ_FILE it. Never edit a file you have not read in
   this session.
2. EDIT_FILE, NOT WRITE_FILE. Change the smallest snippet that fixes the
   problem. WRITE_FILE on an existing file replaces the whole thing and silently
   destroys everything you did not reproduce — only use it for a file you are
   genuinely creating.
3. FIX THE CAUSE, NOT THE SYMPTOM. Never delete a failing assertion, loosen an
   expected value, or wrap a bug in try/except to make an error disappear —
   that hides the bug instead of fixing it.
   This is NOT a ban on touching test files. A test with a genuine defect of
   its own — a missing import, a typo, a stale API call — is a bug like any
   other: fix it and say in your summary that you did, and why. What you must
   never do is change what a test ASSERTS in order to make it pass.
4. STAY IN SCOPE. Fix what you were asked to fix. No refactors, no renames, no
   style cleanups, no dependency bumps alongside it.
5. THE TESTS ARE THE VERDICT. After your edits the suite is run for real and
   the output comes back to you. Keep working until it passes.
6. NEVER CLAIM A FIX YOU HAVE NOT SEEN WORK. If you could not solve it, say
   exactly that, what you found, and what you would try next. An honest failure
   is useful; a confident wrong answer costs the user hours.

7. DESCRIBING AN EDIT IS NOT MAKING IT. Never write the fixed code into your
   reply and ask the user to apply it — you have EDIT_FILE, so apply it. A
   plain-text answer is for the SUMMARY once the work is done.

When the tests pass, or you are certain the fix is complete, reply with a short
plain-text summary and no further tool calls."""


# Колко съобщения от края се пазят цели при съкращаване на историята.
_KEEP_TAIL = 14
# След толкова рунда без НИТО една промяна цикълът бута модела да действа.
_STALL_ROUNDS = 3
# Колко пъти да настояваме, ако моделът описва промяна вместо да я прави.
_MAX_PUSHBACKS = 2


def _compact_history(messages: list[dict]) -> list[dict]:
    """
    Съкращава дълга история, БЕЗ да къса връзката tool_calls → tool резултат.

    Умишлено НЕ ползва `Brain.trim_round_history`: той е писан за мисиите, къде
    всеки рунд е самостоятелен опит за код и единственото нужно е последната
    грешка. Тук е обратното — прочетеният файл ОТ ПРЕДИ 4 рунда е точно
    контекстът, върху който се прави редакцията. Първият жив тест го показа
    недвусмислено: с агресивното рязане моделът прочете `stats.py` три пъти и
    не редактира нищо — на всеки рунд забравяше какво току-що е видял.
    """
    if len(messages) <= _KEEP_TAIL + 2:
        return messages
    head = messages[:2]                      # system + оригиналната задача
    tail = messages[-_KEEP_TAIL:]
    # Осиротял `tool` резултат (родителят му е отрязан) е невалиден за API-то.
    while tail and tail[0].get("role") == "tool":
        tail = tail[1:]
    marker = {"role": "user", "content": "[по-ранните стъпки са съкратени — "
                                         "продължи от текущото състояние на файловете]"}
    return head + [marker] + tail


def _nudge_if_stalled(messages: list[dict], touched: list[str], rounds: int) -> bool:
    """
    Няколко рунда само четене без нито една промяна = моделът обикаля.

    Механизъм, а не ред в промпта, по същата причина както навсякъде другаде:
    кой модел ще се падне в безплатната ротация не се знае, а инструкция работи
    само ако точно този модел реши да я послуша.
    """
    if touched or rounds < _STALL_ROUNDS:
        return False
    messages.append({"role": "user", "content":
                     f"Досега си направил {rounds} рунда четене и НИТО ЕДНА промяна. "
                     "Спри да четеш. Или направи конкретна редакция с EDIT_FILE сега, "
                     "или обясни точно какво ти пречи да я направиш."})
    return True


def _tool_call_paths(name: str, args: dict) -> list[str]:
    if name in ("EDIT_FILE", "WRITE_FILE"):
        p = str(args.get("path", "")).strip()
        return [p] if p else []
    return []


def _edit_succeeded(result: str) -> bool:
    """Дали резултатът от EDIT_FILE/WRITE_FILE отчита РЕАЛНА промяна.

    `touched` се пълнеше от аргументите на извикването, тоест провалена
    редакция (несъвпаднал anchor, отказан от sandbox запис) се броеше за
    променен файл (bug found end-to-end, 2026-08-12). Две последствия, и
    второто е същественото:
      • финалният отчет изброяваше файлове като „променени", които не са;
      • `_nudge_if_stalled` мълчи, щом `touched` не е празен — значи един
        ПРОВАЛЕН EDIT_FILE изключваше подсещането „стига четене, действай"
        за остатъка от ремонта, точно когато моделът има най-голяма нужда
        от него, защото още не е променил нищо.
    genesis_skills слага "✓" при успех и "❌" при отказ и в двата инструмента.
    """
    # Само в заглавието: отказът цитира близки редове от файла и `return "✓ готово"`
    # в тях се броеше за успех (одит 2026-10-07).
    return bool(re.match(r"\[(?:EDIT|WRITE)_FILE: [^\n]*?\] ✓", (result or "").lstrip()))


def _verify_after_edit(root: Path, cmd: str | None, before: TestRun,
                       messages: list[dict], say) -> TestRun | None:
    """Пуска тестовете веднага след успешна редакция (design note, 2026-09-24).

    Досега тестовете вървяха едва когато моделът сам реши, че е готов — и
    докато стигне дотам, той четеше и проверяваше на ръка. `bench_fix.py`:
    всяка поправка на ЕДИН ред изгаряше всичките 6 рунда. Тестовете са
    присъдата, така че щом минат → край. Ако още падат, изходът им отива при
    модела наготово, вместо да губи рунд за RUN_CMD.

    Връща TestRun при зелено (цикълът спира), None иначе. Мълчи, ако няма
    тестова команда или ако тестовете минаваха и ПРЕДИ — зелено тогава не
    доказва нищо за поправката.
    """
    if not cmd or (before.ran and before.passed):
        return None
    run = run_tests(root, cmd)
    if not run.ran:
        return None
    if run.passed:
        say("🧪 Тестовете минават след редакцията — готово.")
        return run
    say("🧪 След редакцията тестовете още падат — давам изхода на модела.")
    messages.append({"role": "user", "content":
                     "Пуснах тестовете след редакцията ти — ВСЕ ОЩЕ падат:\n"
                     f"{run.output[:2000]}\n\nПродължи с поправката."})
    return None


def _relative(root: Path, path_str: str) -> str:
    p = Path(path_str)
    if not p.is_absolute():
        # `./stats.py` → `stats.py`: иначе търсенето в снимката е `././stats.py`
        # и целият файл излизаше като нов в диффа.
        p = root / p
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return path_str


def repair(project: str | Path, task: str, *, test_command: str | None = None,
           max_rounds: int = 8, quality: str | None = None,
           on_status=None, checkpoint: bool = False) -> RepairOutcome:
    """
    Fix `task` in `project`. Returns what actually happened — including, when
    that is the truth, that it did not manage to fix it.

    `checkpoint=True` (`genesis fix --checkpoint`) snapshots the project first.
    Off by default since 2026-09-23 — the operator wants backups only on
    request. Git projects lose nothing (diff and undo come from git); a
    non-git project gets a loud warning that there is no way back.
    """
    import genesis_skills

    root = Path(project).expanduser().resolve()
    say = on_status or (lambda msg: print(msg))
    if not root.is_dir():
        return RepairOutcome(False, f"Няма такава директория: {root}")

    info = detect_project(root)
    cmd = test_command if test_command is not None else info.test_command

    say(f"🔎 {root.name}: {info.language}, тестове: {cmd or 'няма открити'}")

    snapshot: Path | None = None
    # Некомитнати промени + поправка = смесени в един `git diff`, а съветът
    # `git checkout .` триеше и работата на човека (одит 2026-10-09). Тогава
    # снимка се прави винаги.
    dirty = (root / ".git").exists() and bool(_git(root, "status", "--porcelain"))
    if dirty and not checkpoint:
        say("⚠️  Има некомитнати промени — правя снимка, за да не се смесят с поправката.")
        checkpoint = True
    if checkpoint:
        try:
            snapshot = create_checkpoint(root)
        except (RuntimeError, OSError) as e:
            extra = (" Комитни или `git stash` промените си и пусни пак." if dirty else "")
            return RepairOutcome(False, f"Снимката не успя, нищо не е променено: {e}{extra}")
        say(f"📦 Снимка преди промените: {snapshot.name}")
    elif (root / ".git").exists():
        say("↩️  Без снимка — промените се връщат с git (или `--checkpoint` за снимка).")
    else:
        say("⚠️  Без снимка и без git — промените НЯМА как да се върнат. "
            "Пусни с `--checkpoint`, ако искаш снимка.")

    files_before = _fingerprint(root)
    before = run_tests(root, cmd)
    if before.ran:
        say(f"🧪 Преди: {'минават ✅' if before.passed else 'падат ❌'}")
        if before.passed:
            # Worth saying out loud: a green suite cannot confirm this fix, so
            # the user should not read a later green run as proof of anything.
            say("   ⚠️  Тестовете вече минават — те НЕ могат да потвърдят тази поправка.")

    messages: list[dict] = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content":
            f"ПРОЕКТ: {root}\n\n{repo_map(root)}\n\n"
            f"ЗАДАЧА: {task}\n\n"
            + (f"Тестова команда: {cmd}\n"
               f"Изход преди промените ({'минават' if before.passed else 'падат'}):\n"
               f"{before.output[:2000]}\n" if before.ran else
               "Тестова команда не е открита — провери ръчно дали поправката работи.\n")},
    ]

    brain = Brain(quality=quality)
    touched: list[str] = []
    prev_workspace = genesis_skills._WORKSPACE
    genesis_skills.set_workspace(root)
    rounds = 0
    pushbacks = 0
    after = before
    edited_now = False
    try:
        for rounds in range(1, max_rounds + 1):
            reply = brain.complete(messages, tools=REPAIR_TOOLS)
            raw = (reply.raw_text or "").strip()

            # ── Native tool calls ───────────────────────────────────────────
            if getattr(reply, "tool_calls", None):
                messages.append({"role": "assistant", "content": raw or None,
                                 "tool_calls": reply.tool_calls})
                for tc in reply.tool_calls:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args = load_tool_arguments(fn.get("arguments"))
                    except (ValueError, TypeError):
                        args = {}
                    say(f"  ⚙️  {name} {str(args.get('path') or args.get('pattern') or args.get('command') or '')[:70]}")
                    result = genesis_skills.dispatch_tool_call(name, args or fn.get("arguments") or {})
                    if _edit_succeeded(result):
                        touched += [_relative(root, p) for p in _tool_call_paths(name, args)]
                        edited_now = True
                    messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                     "content": result[:_MAX_TOOL_OUTPUT]})
                if edited_now:
                    edited_now = False
                    _drop_stale_bytecode(root, touched)
                    verdict = _verify_after_edit(root, cmd, before, messages, say)
                    if verdict is not None:
                        after = verdict
                        break
                messages = _compact_history(messages)
                if _nudge_if_stalled(messages, touched, rounds):
                    say("  ↯ само четене досега — подсещам модела да действа")
                continue

            # ── Text-tag fallback (models without native tool-calling) ──────
            tag_results = genesis_skills.parse_and_execute_tools(raw)
            if tag_results:
                for r in tag_results:
                    say(f"  ⚙️  {r.splitlines()[0][:90] if r else ''}")
                edited = _paths_from_tag_results(root, tag_results)
                touched += edited
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content":
                                 "Резултати от инструментите:\n"
                                 + "\n".join(r[:_MAX_TOOL_OUTPUT] for r in tag_results)})
                if edited:
                    _drop_stale_bytecode(root, touched)
                    verdict = _verify_after_edit(root, cmd, before, messages, say)
                    if verdict is not None:
                        after = verdict
                        break
                messages = _compact_history(messages)
                if _nudge_if_stalled(messages, touched, rounds):
                    say("  ↯ само четене досега — подсещам модела да действа")
                continue

            # ── No tools: the model considers itself done ───────────────────
            if not touched:
                # Промяна с команда (`sed -i`, patch) също е промяна.
                touched += _changed_since(root, files_before)
            if not touched:
                # It answered in prose without changing anything. Usually that
                # means it wrote the fixed code INTO the reply and asked the
                # user to paste it — the exact "described it instead of doing
                # it" failure this whole mode exists to eliminate, and it
                # showed up on the very first live run. One explicit push back
                # (twice at most, so a model that simply cannot do it does not
                # burn every round) before giving up.
                if pushbacks < _MAX_PUSHBACKS and rounds < max_rounds:
                    pushbacks += 1
                    say("  ↯ описа промяна, но не я направи — искам я реално")
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({"role": "user", "content":
                                     "Ти ОПИСА какво трябва да се промени, но не промени нищо — "
                                     "файловете на диска са непокътнати. Направи промяната сега "
                                     "с EDIT_FILE (path + точния съществуващ текст + новия). "
                                     "Не пиши поправения код в отговора си — приложи го."})
                    messages = _compact_history(messages)
                    continue
                return RepairOutcome(
                    False, raw or "Моделът не направи нито една промяна.",
                    rounds, snapshot, [], "", before, before)

            _drop_stale_bytecode(root, touched)
            after = run_tests(root, cmd)
            if not after.ran or after.passed:
                break
            if rounds >= max_rounds:
                break
            say(f"🧪 Рунд {rounds}: тестовете още падат — връщам изхода на модела")
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content":
                             f"Тестовете ВСЕ ОЩЕ падат след промените ти:\n{after.output}\n\n"
                             "Продължи да поправяш. Ако причината е в самия тест, а не в кода, "
                             "кажи го изрично и не го променяй."})
            messages = _compact_history(messages)
    finally:
        genesis_skills.set_workspace(prev_workspace)

    # Финален тестов пас, ако цикълът е свършил, без нито веднъж да е стигнал
    # до run_tests (bug found end-to-end, 2026-08-12). `after` се инициализира
    # като `before`, а run_tests се вика САМО в клона „моделът не върна tool
    # call". Модел, който изразходва целия бюджет от рундове по tool calls —
    # напълно нормално: REPO_MAP → READ_FILE → GLOB → EDIT_FILE → RUN_CMD са
    # вече пет — излизаше оттук с присъда, изчислена от състоянието ОТПРЕДИ
    # промяната. Наблюдавано на живо: поправката беше приложена и вярна,
    # тестовете минаваха, а инструментът каза „НЕ е поправено" и посъветва
    # `--revert`, тоест да изхвърлиш работеща поправка. За подсистема, чийто
    # пръв принцип е „ТЕСТОВЕТЕ СА ПРИСЪДАТА", присъда по остарели данни е
    # по-лоша от липсваща.
    # Промените с команди (RUN_CMD) не минават през touched — гледа се диска.
    touched = sorted(set(touched) | set(_changed_since(root, files_before)))
    if touched and after is before and cmd:
        say("🧪 Рундовете свършиха — пускам тестовете за финална присъда…")
        _drop_stale_bytecode(root, touched)
        after = run_tests(root, cmd)

    diff = project_diff(root, snapshot, touched)
    files = sorted(set(touched))

    if not files:
        # Зелени тестове без нито една промяна не са поправка (одит 2026-10-09:
        # „Промените са направени“ при празен дифф и код 0).
        ok, summary = False, "Не е направена нито една промяна — нищо не е поправено."
    elif after.ran and after.passed and not before.passed:
        ok, summary = True, f"Поправено: тестовете вече минават ({cmd})."
    elif after.ran and not after.passed:
        ok, summary = False, (f"НЕ е поправено — тестовете още падат след {rounds} рунда. "
                              f"Промените са запазени за преглед; {_undo_hint(root, snapshot, files)}")
    elif not after.ran:
        ok, summary = False, ("Промените са направени, но НЕ са проверени — този проект няма "
                              "открита тестова команда. Прегледай диффа преди да му вярваш.")
    else:
        ok, summary = True, ("Промените са направени. Тестовете минаваха и преди поправката, "
                             "така че те не доказват нищо за нея — прегледай диффа.")

    return RepairOutcome(ok, summary, rounds, snapshot, files, diff, before, after)


def _undo_hint(root: Path, snapshot: Path | None, files: list[str] | None = None) -> str:
    """Как се връщат промените — зависи от това какво има, не от надежда.
    Никога `git checkout .`: то връща и чужди (на човека) промени."""
    if snapshot:
        return f"върни ги с `genesis fix --revert {root}`."
    if (root / ".git").exists():
        names = " ".join(f'"{f}"' for f in (files or [])[:20])
        return (f"върни ги с `git checkout -- {names}` в проекта (новите файлове изтрий)."
                if names else "няма какво да се връща.")
    return "снимка не е правена и проектът не е в git — връщане няма."


def _paths_from_tag_results(root: Path, results: list[str]) -> list[str]:
    """Recover edited paths from tag-mode tool output (`[EDIT_FILE: /p] ✓ …`)."""
    out: list[str] = []
    for r in results:
        head = r.splitlines()[0] if r else ""
        if not _edit_succeeded(head):
            continue  # виж _edit_succeeded — отказана редакция не е промяна
        for tag in ("[EDIT_FILE: ", "[WRITE_FILE: "):
            if head.startswith(tag) and "]" in head:
                out.append(_relative(root, head[len(tag):head.index("]")]))
    return out


def format_outcome(out: RepairOutcome, *, show_diff: bool = True) -> str:
    lines = [("✅ " if out.success else "❌ ") + out.summary,
             f"рундове: {out.rounds}"]
    if out.files_touched:
        lines.append("променени файлове: " + ", ".join(out.files_touched))
    if out.checkpoint:
        lines.append(f"снимка преди промените: {out.checkpoint}")
    if out.tests_before and out.tests_before.ran and out.tests_after:
        lines.append(f"тестове: {'✅' if out.tests_before.passed else '❌'} преди → "
                     f"{'✅' if out.tests_after.passed else '❌'} след")
    if show_diff and out.diff:
        lines.append("\n─── diff ───\n" + out.diff)
    return "\n".join(lines)


if __name__ == "__main__":  # ръчна проверка: genesis_agent.repo_agent <път> <задача>
    import sys
    if len(sys.argv) < 3:
        print("употреба: python3 -m genesis_agent.repo_agent <проект> <описание на бъга>")
        raise SystemExit(2)
    result = repair(sys.argv[1], " ".join(sys.argv[2:]),
                    quality=os.environ.get("GENESIS_QUALITY"))
    print(format_outcome(result))
