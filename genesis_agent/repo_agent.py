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

1. **A checkpoint before the first edit.** A tar snapshot of the project, taken
   before the agent is allowed to touch anything, restorable with one command.
   Git is used as well when present, but not relied on — the projects most
   likely to need this are exactly the ones not under version control.

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
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from genesis_agent.brain import Brain
from genesis_agent.repo_map import detect_project, repo_map
from genesis_agent.tool_schemas import REPAIR_TOOLS

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

def _tree_size_mb(root: Path) -> float:
    total = 0
    for p in root.rglob("*"):
        if any(part in _SKIP_IN_SNAPSHOT for part in p.parts):
            continue
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total / (1024 * 1024)


def _snapshot_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(info.name).parts
    if any(part in _SKIP_IN_SNAPSHOT for part in parts):
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
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = CHECKPOINT_DIR / f"{root.name}-{stamp}.tar.gz"
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(root, arcname=".", filter=_snapshot_filter)
    meta = {
        "project": str(root),
        "created": stamp,
        "git_head": _git(root, "rev-parse", "HEAD") or "",
        "git_dirty": bool(_git(root, "status", "--porcelain")),
    }
    dest.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return dest


def _git(root: Path, *args: str) -> str:
    """git output, or "" when this is not a repo / git is unavailable."""
    try:
        proc = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=30, check=False)
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
                mine.append((meta.get("created", ""), tar_path))
    if not mine:
        return None
    return max(mine)[1]


def restore_checkpoint(root: str | Path, checkpoint: str | Path | None = None) -> str:
    """Put the project back exactly as it was when the snapshot was taken."""
    root = Path(root).resolve()
    cp = Path(checkpoint) if checkpoint else latest_checkpoint(root)
    if not cp or not cp.exists():
        return f"❌ Няма намерена снимка за {root}."
    with tarfile.open(cp, "r:gz") as tar:
        # Python 3.12+ validates member paths; on older versions the archive is
        # one we wrote ourselves minutes ago, so there is nothing to filter.
        try:
            tar.extractall(root, filter="data")  # type: ignore[call-arg]
        except TypeError:
            tar.extractall(root)
    return f"✓ {root} е върнат към снимката {cp.name}"


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
        d = _git(root, "diff")
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
    return "✓" in (result or "")


def _relative(root: Path, path_str: str) -> str:
    p = Path(path_str)
    if not p.is_absolute():
        return path_str
    try:
        return str(p.resolve().relative_to(root.resolve()))
    except (ValueError, OSError):
        return path_str


def repair(project: str | Path, task: str, *, test_command: str | None = None,
           max_rounds: int = 8, quality: str | None = None,
           on_status=None) -> RepairOutcome:
    """
    Fix `task` in `project`. Returns what actually happened — including, when
    that is the truth, that it did not manage to fix it.
    """
    import genesis_skills

    root = Path(project).expanduser().resolve()
    say = on_status or (lambda msg: print(msg))
    if not root.is_dir():
        return RepairOutcome(False, f"Няма такава директория: {root}")

    info = detect_project(root)
    cmd = test_command if test_command is not None else info.test_command

    say(f"🔎 {root.name}: {info.language}, тестове: {cmd or 'няма открити'}")

    try:
        checkpoint = create_checkpoint(root)
    except (RuntimeError, OSError) as e:
        return RepairOutcome(False, f"Снимката не успя, нищо не е променено: {e}")
    say(f"📦 Снимка преди промените: {checkpoint.name}")

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
                        args = json.loads(fn.get("arguments") or "{}")
                    except (ValueError, TypeError):
                        args = {}
                    say(f"  ⚙️  {name} {str(args.get('path') or args.get('pattern') or args.get('command') or '')[:70]}")
                    result = genesis_skills.dispatch_tool_call(name, args)
                    if _edit_succeeded(result):
                        touched += [_relative(root, p) for p in _tool_call_paths(name, args)]
                    messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                     "content": result[:_MAX_TOOL_OUTPUT]})
                messages = _compact_history(messages)
                if _nudge_if_stalled(messages, touched, rounds):
                    say("  ↯ само четене досега — подсещам модела да действа")
                continue

            # ── Text-tag fallback (models without native tool-calling) ──────
            tag_results = genesis_skills.parse_and_execute_tools(raw)
            if tag_results:
                for r in tag_results:
                    say(f"  ⚙️  {r.splitlines()[0][:90] if r else ''}")
                touched += _paths_from_tag_results(root, tag_results)
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content":
                                 "Резултати от инструментите:\n"
                                 + "\n".join(r[:_MAX_TOOL_OUTPUT] for r in tag_results)})
                messages = _compact_history(messages)
                if _nudge_if_stalled(messages, touched, rounds):
                    say("  ↯ само четене досега — подсещам модела да действа")
                continue

            # ── No tools: the model considers itself done ───────────────────
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
                    rounds, checkpoint, [], "", before, before)

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
    if touched and after is before and cmd:
        say("🧪 Рундовете свършиха — пускам тестовете за финална присъда…")
        after = run_tests(root, cmd)

    diff = project_diff(root, checkpoint, touched)
    files = sorted(set(touched))

    if after.ran and after.passed and not before.passed:
        ok, summary = True, f"Поправено: тестовете вече минават ({cmd})."
    elif after.ran and not after.passed:
        ok, summary = False, (f"НЕ е поправено — тестовете още падат след {rounds} рунда. "
                              f"Промените са запазени за преглед; върни ги с "
                              f"`genesis fix --revert {root}`.")
    elif not after.ran:
        ok, summary = False, ("Промените са направени, но НЕ са проверени — този проект няма "
                              "открита тестова команда. Прегледай диффа преди да му вярваш.")
    else:
        ok, summary = True, ("Промените са направени. Тестовете минаваха и преди поправката, "
                             "така че те не доказват нищо за нея — прегледай диффа.")

    return RepairOutcome(ok, summary, rounds, checkpoint, files, diff, before, after)


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
