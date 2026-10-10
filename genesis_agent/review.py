"""
genesis_agent.review — /review: преглед на промените за грешки (2026-10-10).

Като `/review` в Claude Code: без аргумент — незаписаните промени спрямо HEAD
(няма ли такива — последният commit); `/review main` — всичко спрямо клона
main. Diff-ът влиза в заявката наготово: моделът не губи ход да го търси, а
файловете около него чете сам с READ_FILE.

git тук чете хранилище, което може да е чуждо: `.git/config` с
`core.fsmonitor` пуска програма при всеки diff, а външният diff и textconv —
при показването. Затова са изключени изрично (`--no-ext-diff --no-textconv`).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

MAX_DIFF = 60000   # знака; ~15K токена — над това моделът получава началото
# Име на клон/commit, не опция: `/review --output=/tmp/x` би бил флаг за git.
_REF = re.compile(r"[\w][\w./~^@{}-]{0,200}")
# fsmonitor празно = изключено и в стария git (там е път до програма), и в новия
# (там е bool). quotePath=false: „файл.py“ остава „файл.py“, не "\321\204…" —
# READ_FILE не намираше такъв път (одит 2026-10-10).
_SAFE = ("-c", "core.fsmonitor=", "-c", "core.quotePath=false")

PROMPT = """Прегледай тези промени за грешки — НЕ променяй файлове, само докладвай.

Търси: грешна логика, пропуснати случаи (празно, None, грешка от мрежата/диска),
счупени извиквания (сменен подпис, а старите места не са), сигурност (вход от
потребителя към команда/SQL/път), изтичане на ресурси, тестове, които не
проверяват това, което казват. Прочети с READ_FILE кода около промяната, преди
да твърдиш нещо за него. Стил и вкус — не.

За всяка находка: `файл:ред` — какво е грешно, конкретен вход или стъпки, при
които се проявява, и как да се поправи. Подреди по тежест. Няма ли истински
грешки — кажи го с едно изречение, без да измисляш."""


def _git(root: Path, *args: str, safe: tuple[str, ...] = ()) -> tuple[int, str]:
    """(код, изход); при грешка изходът е празен — stderr на git не бива да
    стига до модела като diff („fatal: ambiguous argument 'HEAD'“, одит 2026-10-10)."""
    try:
        proc = subprocess.run(["git", *_SAFE, *safe, "-C", str(root), *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=30,
                              check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, proc.stdout if proc.returncode == 0 else ""


def _no_filters(root: Path) -> tuple[str, ...]:
    """`-c filter.X.clean= -c filter.X.process=` за всеки филтър в настройките.
    `git diff` спрямо работното дърво пуска clean филтъра на всеки променен
    файл — подхвърлен `.git/config` пускаше програма (одит 2026-10-10,
    възпроизведено). Самото четене на настройките не пуска нищо."""
    _, names = _git(root, "config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|process)$")
    out: list[str] = []
    for name in {n.strip() for n in names.splitlines() if n.strip()}:
        out += ["-c", f"{name}="]
    return tuple(out)


def _own_git_dir(root: Path, top: Path) -> bool:
    """Хранилището е на тази папка, не друго, към което сочи `.git` файл
    („gitdir: /друг/проект/.git“ вкарваше чуждите тайни в заявката — одит
    2026-10-10). Позволени: `.git` в някоя от горните папки (и подмодулите
    в `.git/modules` там) и работно дърво, чийто обратен линк сочи насам."""
    rc, raw = _git(root, "rev-parse", "--absolute-git-dir")
    if rc != 0:
        return False
    git_dir = Path(os.path.realpath(raw.strip()))
    for folder in (top, *top.parents):
        own = Path(os.path.realpath(folder / ".git"))
        if git_dir == own or own in git_dir.parents:
            return True
    try:   # git worktree add: <главното>/.git/worktrees/<име>/gitdir → <тук>/.git
        back = (git_dir / "gitdir").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return Path(os.path.realpath(back)) == Path(os.path.realpath(top / ".git"))


def build(workspace: Path, target: str = "") -> tuple[str | None, str]:
    """(заявката за модела или None, бележка за оператора)."""
    root = Path(workspace)
    rc, top = _git(root, "rev-parse", "--show-toplevel")
    if rc != 0:
        return None, "/review иска git хранилище — тази папка не е."
    if not _own_git_dir(root, Path(os.path.realpath(top.strip()))):
        return None, "/review: .git тук сочи към друго хранилище — не го преглеждам."
    safe = _no_filters(root)

    def diff(*spec: str) -> str:
        # --relative: пътищата са спрямо работната папка, както ги чете READ_FILE
        # (в подпапка на хранилището бяха спрямо корена му — одит 2026-10-10);
        # `--` — файл с име HEAD не прави ревизията двусмислена.
        _, text = _git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-color",
                       "--relative", *spec, "--", safe=safe)
        return text

    def has(rev: str) -> bool:
        return _git(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")[0] == 0

    target = target.strip()
    untracked: list[str] = []
    if target:
        if not _REF.fullmatch(target):
            return None, f"/review {target}: очаквам име на клон или commit (напр. /review main)."
        if not has(target):
            return None, f"/review {target}: няма такъв клон или commit."
        # От общия предшественик: промените на този клон, без чуждите в target.
        rc, base = _git(root, "merge-base", target, "HEAD")
        text = diff(base.strip() if rc == 0 and base.strip() else target)
        what = f"промените спрямо {target}"
    else:
        has_head = has("HEAD")
        text = diff("HEAD") if has_head else diff("--cached")
        rc, others = _git(root, "ls-files", "--others", "--exclude-standard")
        untracked = [ln for ln in others.splitlines() if ln.strip()] if rc == 0 else []
        what = "незаписаните промени"
        if not text.strip() and not untracked and has_head:
            what = "последния commit"
            if has("HEAD~1"):
                text = diff("HEAD~1", "HEAD")
            else:   # единственият commit — спрямо нищо (одит 2026-10-10)
                _, text = _git(root, "diff-tree", "-p", "--root", "--no-commit-id", "--no-color",
                               "--no-ext-diff", "--no-textconv", "--relative", "HEAD", safe=safe)
    diff_text = text
    if not diff_text.strip() and not untracked:
        return None, (f"Няма какво да прегледам ({what} са празни)." if target else
                      "Няма какво да прегледам: няма незаписани промени, нито commit.")
    clipped = len(diff_text) > MAX_DIFF
    body = diff_text[:MAX_DIFF]
    parts = [PROMPT, f"\n\nПромените ({what}, git diff):\n```diff\n{body}\n```"]
    if clipped:
        parts.append(f"\n(diff-ът е {len(diff_text)} знака — показани са първите {MAX_DIFF}; "
                     "останалото виж с `git diff` през RUN_CMD.)")
    if untracked:
        listed = "\n".join(f"- {u}" for u in untracked[:50])
        more = f"\n… и още {len(untracked) - 50}" if len(untracked) > 50 else ""
        parts.append(f"\n\nНови файлове, още не в git (прочети ги с READ_FILE):\n{listed}{more}")
    files = len(re.findall(r"^diff --git ", diff_text, re.MULTILINE)) + len(untracked)
    note = f"🔎 Преглед на {what}: файлове {files}, {len(diff_text)} знака diff"
    return "".join(parts), note + (" (отрязан)" if clipped else "") + "."
