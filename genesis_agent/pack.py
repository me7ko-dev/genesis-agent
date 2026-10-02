"""`genesis pack <папка>` — проектът така, както го получава клиентът.

NEXT_STEPS Д.13: zip на проекта + кратък отчет (какво има, как се пуска,
минават ли тестовете). Никога ключове (`sandbox.sensitive_path_reason` —
същото правило като за READ_FILE) и никога инструментариума (`.venv`, `.git`,
кешове — `repo_map._SKIP_DIRS`). Решават само частите ПОД проекта: проект в
`…/build/site` не бива да излезе празен.
"""
from __future__ import annotations

import datetime
import fnmatch
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from genesis_agent import repo_agent, repo_map, sandbox

REPORT = "ОТЧЕТ.md"


@dataclass
class PackResult:
    zip_path: Path
    files: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    tests: str = "не са пускани"


_Rule = tuple[bool, str, bool, bool]  # отрицание, шаблон, само папка, от корена


def _gitignore(root: Path) -> list[_Rule]:
    """Правилата от `.gitignore` в корена на проекта."""
    gi = root / ".gitignore"
    rules = []
    for line in (gi.read_text(encoding="utf-8", errors="replace").splitlines() if gi.is_file() else []):
        line = line.strip()
        if line and not line.startswith("#"):
            pat = line.lstrip("!")
            rules.append((line.startswith("!"), pat.strip("/"), pat.endswith("/"),
                          "/" in pat.rstrip("/")))
    return rules


def _ignored(rel: Path, rules: list[_Rule]) -> bool:
    """Опростен gitignore: шаблон с `/` е от корена, без `/` — за всяка част; последният печели."""
    hit = False
    for neg, pat, dir_only, anchored in rules:
        n = len(rel.parts) - 1 if dir_only else len(rel.parts)
        if anchored:
            match = any(fnmatch.fnmatch("/".join(rel.parts[:k]), pat) for k in range(1, n + 1))
        else:
            match = any(fnmatch.fnmatch(part, pat) for part in rel.parts[:n])
        if match:
            hit = not neg
    return hit


def collect(root: Path, exclude: Path | None = None) -> tuple[list[str], list[str]]:
    """(файловете за клиента, прескочените тайни) — относителни, с `/`.

    Каквото авторът е сложил в `.gitignore` (логове, изходи от пусканията), не е
    и за клиента; ключовете обаче се изброяват и тогава — клиентът трябва да
    знае, че си слага свой `.env`.
    """
    rules = _gitignore(root)
    files: list[str] = []
    secrets: list[str] = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if (not p.is_file() or p == exclude or p.suffix == ".pyc" or rel.as_posix() == REPORT
                or any(part in repo_map._SKIP_DIRS for part in rel.parts[:-1])):
            continue
        if sandbox.sensitive_path_reason(p):
            secrets.append(rel.as_posix())
        elif not _ignored(rel, rules):
            files.append(rel.as_posix())
    return files, secrets


def _client_command(command: str) -> str:
    """Абсолютният път до Python-а е от тази машина, не от клиентската."""
    return re.sub(r'^(?:"[^"]+"|\S+) -m pytest', "python -m pytest", command)


def _run_command(entry: str) -> str:
    """`pkg/__main__.py` върви само като модул: пуснат като файл, гърми на `from .x import …`."""
    p = Path(entry)
    if p.name == "__main__.py" and len(p.parts) > 1:
        return "python -m " + ".".join(p.parts[:-1])
    return f"python {entry}" if entry.endswith(".py") else entry


def _how_to_run(root: Path, info: repo_map.ProjectInfo) -> list[str]:
    steps = []
    if (root / "requirements.txt").is_file():
        steps.append("`pip install -r requirements.txt`")
    steps += [f"`{_run_command(e)}`" for e in info.entry_points]
    if info.test_command:
        steps.append(f"тестове: `{_client_command(info.test_command)}`")
    return steps or ["виж README.md"]


def _report(root: Path, files: list[str], secrets: list[str], info: repo_map.ProjectInfo,
            tests: str) -> str:
    lines = [f"# {root.name} — отчет", "",
             f"Дата: {datetime.date.today().isoformat()} · файлове: {len(files)}", "",
             "## Как се пуска", *(f"- {s}" for s in _how_to_run(root, info)), "",
             "## Тестове", tests, ""]
    if (root / "README.md").is_file():
        lines += ["## Описание и допускания", "Виж README.md.", ""]
    if secrets:
        lines += ["## Не са включени (ключове/тайни)", *(f"- {s}" for s in secrets), ""]
    return "\n".join(lines)


def pack(root: Path | str, out: Path | str | None = None, *, run_tests: bool = True) -> PackResult:
    root = Path(root).resolve()
    zip_path = Path(out).resolve() if out else root.parent / f"{root.name}-{datetime.date.today():%Y%m%d}.zip"
    files, secrets = collect(root, exclude=zip_path)
    info = repo_map.detect_project(root)
    tests = "не са пускани"
    if run_tests and info.test_command:
        run = repo_agent.run_tests(root, info.test_command)
        last = (run.output.strip().splitlines() or ["(без изход)"])[-1]
        tests = (f"{'✅' if run.passed else '❌'} `{_client_command(run.command)}` → {last}"
                 if run.ran else run.output)
    elif run_tests:
        tests = "няма открити тестове"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            z.write(root / rel, f"{root.name}/{rel}")
        z.writestr(f"{root.name}/{REPORT}", _report(root, files, secrets, info, tests))
    return PackResult(zip_path, files, secrets, tests)


def summary(res: PackResult) -> str:
    """Редовете за терминала — общи за `genesis pack` и `/pack` в чата."""
    lines = [f"📦 {res.zip_path}  ({len(res.files)} файл(а) + {REPORT})", f"Тестове: {res.tests}"]
    if res.secrets:
        lines.append("Не са включени (ключове/тайни): " + ", ".join(res.secrets))
    return "\n".join(lines)
