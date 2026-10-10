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

import re
import subprocess
from pathlib import Path

MAX_DIFF = 60000   # знака; ~15K токена — над това моделът получава началото
# Име на клон/commit, не опция: `/review --output=/tmp/x` би бил флаг за git.
_REF = re.compile(r"[\w][\w./~^@{}-]{0,200}")
# Празно = изключено и в стария git (там е път до програма), и в новия (там е bool).
_SAFE = ("-c", "core.fsmonitor=")

PROMPT = """Прегледай тези промени за грешки — НЕ променяй файлове, само докладвай.

Търси: грешна логика, пропуснати случаи (празно, None, грешка от мрежата/диска),
счупени извиквания (сменен подпис, а старите места не са), сигурност (вход от
потребителя към команда/SQL/път), изтичане на ресурси, тестове, които не
проверяват това, което казват. Прочети с READ_FILE кода около промяната, преди
да твърдиш нещо за него. Стил и вкус — не.

За всяка находка: `файл:ред` — какво е грешно, конкретен вход или стъпки, при
които се проявява, и как да се поправи. Подреди по тежест. Няма ли истински
грешки — кажи го с едно изречение, без да измисляш."""


def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", *_SAFE, "-C", str(root), *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=30,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stderr.strip()


def _diff(root: Path, *spec: str) -> str:
    _, text = _git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-color", *spec)
    return text


def build(workspace: Path, target: str = "") -> tuple[str | None, str]:
    """(заявката за модела или None, бележка за оператора)."""
    root = Path(workspace)
    rc, top = _git(root, "rev-parse", "--show-toplevel")
    if rc != 0:
        return None, "/review иска git хранилище — тази папка не е."
    root = Path(top.strip())
    target = target.strip()
    untracked: list[str] = []
    if target:
        if not _REF.fullmatch(target):
            return None, f"/review {target}: очаквам име на клон или commit (напр. /review main)."
        rc, _ = _git(root, "rev-parse", "--verify", "--quiet", f"{target}^{{commit}}")
        if rc != 0:
            return None, f"/review {target}: няма такъв клон или commit."
        # От общия предшественик: промените на този клон, без чуждите в target.
        rc, base = _git(root, "merge-base", target, "HEAD")
        diff = _diff(root, base.strip() if rc == 0 else target)
        what = f"промените спрямо {target}"
    else:
        has_head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD")[0] == 0
        diff = _diff(root, "HEAD") if has_head else _diff(root, "--cached")
        _, others = _git(root, "ls-files", "--others", "--exclude-standard")
        untracked = [ln for ln in others.splitlines() if ln.strip()]
        what = "незаписаните промени"
        if not diff.strip() and not untracked and has_head:
            diff = _diff(root, "HEAD~1", "HEAD") if _git(root, "rev-parse", "--verify", "--quiet",
                                                         "HEAD~1")[0] == 0 else ""
            what = "последния commit"
    if not diff.strip() and not untracked:
        return None, f"Няма какво да прегледам ({what} са празни)." if target else \
            "Няма какво да прегледам: няма незаписани промени, нито предишен commit."
    clipped = len(diff) > MAX_DIFF
    body = diff[:MAX_DIFF]
    parts = [PROMPT, f"\n\nПромените ({what}, git diff):\n```diff\n{body}\n```"]
    if clipped:
        parts.append(f"\n(diff-ът е {len(diff)} знака — показани са първите {MAX_DIFF}; "
                     "останалото виж с `git diff` през RUN_CMD.)")
    if untracked:
        listed = "\n".join(f"- {u}" for u in untracked[:50])
        more = f"\n… и още {len(untracked) - 50}" if len(untracked) > 50 else ""
        parts.append(f"\n\nНови файлове, още не в git (прочети ги с READ_FILE):\n{listed}{more}")
    files = len(re.findall(r"^diff --git ", diff, re.MULTILINE)) + len(untracked)
    note = f"🔎 Преглед на {what}: файлове {files}, {len(diff)} знака diff"
    return "".join(parts), note + (" (отрязан)" if clipped else "") + "."
