"""
genesis_agent.repo_map — find things in a codebase you did not write.

LIST_DIR and READ_FILE are enough to explore a directory you already know.
They are not enough to answer "where does this crash come from", which is the
question the repair loop actually starts with. Two gaps, both of them real:

  * LIST_DIR truncates at 200 entries **alphabetically**. In a checkout of any
    size that means everything past the letter that fills the quota is
    invisible, and the agent has no signal that it is looking at a slice —
    a genesis-0 session once concluded a file did not exist for exactly this
    reason and built a whole plan on the wrong premise.
  * Nothing searched file *contents*. The model had to guess filenames, read
    them whole, and burn its context window doing what grep does in 30ms.

So: `search_code` (ripgrep when present, a pure-Python walk when not) and
`repo_map` (shape of the project, its languages, and — importantly for the
repair loop — how its tests are actually run).

Both refuse to descend into the directories that make a naive walk useless:
.git, node_modules, virtualenvs, build output, caches.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".venv", "venv", "env", ".env",
    "dist", "build", "target", ".next", ".tox", ".gradle", "vendor",
    "site-packages", ".verify_libs", ".idea", ".vscode",
}
# Extensions worth searching. Anything else is assumed to be data or a binary;
# grepping a 40MB model checkpoint helps nobody.
_TEXT_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".rb", ".java",
    ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php", ".swift", ".sh",
    ".bash", ".zsh", ".sql", ".html", ".css", ".scss", ".vue", ".svelte",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".json", ".md", ".rst", ".txt",
    ".tf", ".gradle", ".lua", ".pl", ".r", ".m", ".mm", ".dart", ".ex", ".exs",
}
_MAX_FILE_BYTES = 2_000_000   # a source file bigger than this is generated
_DEFAULT_MAX_RESULTS = 60
_LINE_CLIP = 200              # a matched minified line must not eat the window


@dataclass
class Match:
    path: str
    line: int
    text: str


def _iter_files(root: Path, glob: str | None = None):
    for p in root.rglob(glob or "*"):
        if p.is_dir():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if glob is None and p.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            if p.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield p


def _search_python(root: Path, pattern: str, glob: str | None,
                   max_results: int) -> list[Match]:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"невалиден regex: {e}") from e
    out: list[Match] = []
    for p in _iter_files(root, glob):
        try:
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        out.append(Match(str(p), i, line.rstrip()[:_LINE_CLIP]))
                        if len(out) >= max_results:
                            return out
        except OSError:
            continue
    return out


def _search_ripgrep(root: Path, pattern: str, glob: str | None,
                    max_results: int) -> list[Match] | None:
    """None means 'ripgrep could not answer' — the caller falls back."""
    cmd = ["rg", "--json", "--max-count", str(max_results), "-e", pattern]
    for d in sorted(_SKIP_DIRS):
        cmd += ["--glob", f"!{d}/"]
    if glob:
        cmd += ["--glob", glob]
    cmd.append(str(root))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # rc 1 is "no matches" — a real answer, not a failure. Anything above that
    # (bad pattern, unreadable tree) goes to the Python path so the user gets
    # a proper error message instead of ripgrep's exit code.
    if proc.returncode not in (0, 1):
        return None
    out: list[Match] = []
    for raw in proc.stdout.splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if ev.get("type") != "match":
            continue
        d = ev["data"]
        out.append(Match(
            d["path"].get("text", "?"),
            d["line_number"],
            (d["lines"].get("text") or "").rstrip()[:_LINE_CLIP],
        ))
        if len(out) >= max_results:
            break
    return out


def find_files(pattern: str, path: str | Path = ".",
               max_results: int = 200) -> list[str]:
    """Find files by NAME pattern (e.g. '**/*.tsx', 'test_*.py') — the filename
    counterpart to search_code's content grep. Skips the same noise dirs
    (.git, node_modules, venvs...) as everything else in this module."""
    root = Path(path).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"няма такъв път: {root}")
    out: list[str] = []
    for p in root.rglob(pattern):
        if p.is_dir():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        try:
            out.append(str(p.relative_to(root)))
        except ValueError:
            out.append(str(p))
        if len(out) >= max_results:
            break
    out.sort()
    return out


def search_code(pattern: str, path: str | Path = ".", glob: str | None = None,
                max_results: int = _DEFAULT_MAX_RESULTS) -> list[Match]:
    """Regex search across a tree. Prefers ripgrep, falls back to Python."""
    root = Path(path).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"няма такъв път: {root}")
    if root.is_file():
        root, glob = root.parent, root.name
    if shutil.which("rg"):
        hits = _search_ripgrep(root, pattern, glob, max_results)
        if hits is not None:
            return hits
    return _search_python(root, pattern, glob, max_results)


# ── Project shape ────────────────────────────────────────────────────────────

def _python_cmd() -> str:
    """Как да извикаме СЪЩИЯ интерпретатор, на който върви Genesis.

    Не голото "python3" (bug found end-to-end, 2026-08-12). На Windows това име
    сочи към Microsoft Store alias-а / Python install manager-а — различен
    интерпретатор от този, който изпълнява Genesis, обикновено без pytest в
    него. Наблюдавано на живо: `genesis fix` поправи бъга ПРАВИЛНО, после пусна
    `python3 -m pytest -q`, което задейства install manager-а („Downloading…"),
    върна код 1 и репортва „тестовете още падат" — при вече минаващи тестове.
    А „тестовете са присъдата" е основният принцип на repo_agent, тоест
    подсистемата не можеше да отчете успех на тази машина изобщо.

    Наклонените черти стават прави, защото командата се подава на shell
    (виж sandbox._shell_argv, който на Windows предпочита bash) — там
    обратната наклонена черта е escape знак. Windows приема и двата вида.
    """
    exe = sys.executable or "python3"
    exe = exe.replace("\\", "/")
    return f'"{exe}"' if " " in exe else exe


# Ordered: the first match wins, so a Python project that also carries a
# package.json for its docs site is still tested with pytest.
_TEST_RULES: list[tuple[str, str, str]] = [
    # (marker file, language, test command)
    ("pytest.ini", "python", f"{_python_cmd()} -m pytest -q"),
    ("tox.ini", "python", f"{_python_cmd()} -m pytest -q"),
    ("pyproject.toml", "python", f"{_python_cmd()} -m pytest -q"),
    ("setup.py", "python", f"{_python_cmd()} -m pytest -q"),
    ("Cargo.toml", "rust", "cargo test"),
    ("go.mod", "go", "go test ./..."),
    ("package.json", "javascript", "npm test"),
    ("Gemfile", "ruby", "bundle exec rspec"),
    ("pom.xml", "java", "mvn -q test"),
    ("build.gradle", "java", "gradle test"),
    ("Makefile", "make", "make test"),
]


@dataclass
class ProjectInfo:
    root: Path
    language: str
    test_command: str
    file_counts: Counter
    entry_points: list[str]
    is_git: bool
    git_dirty: bool


def _package_json_has_test(root: Path) -> bool:
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool((data.get("scripts") or {}).get("test"))


def _makefile_has_test(root: Path) -> bool:
    try:
        return bool(re.search(r"^test\s*:", (root / "Makefile").read_text(
            encoding="utf-8", errors="replace"), re.MULTILINE))
    except OSError:
        return False


def _has_pytest_style_tests(root: Path) -> bool:
    """Има ли изобщо файлове, които pytest би събрал?

    Обратната страна на "маркерният файл не е обещание" (виж коментара в
    detect_project): липсата на маркерен файл също не е обещание, че тестове
    НЯМА. Хванато на живо (2026-08-12) с `genesis fix` върху малък проект от
    два файла — `calc.py` и `test_calc.py`, без pyproject.toml, без setup.py,
    без git. detect_project върна test_command="" ("тестове: няма открити"),
    затова целият тест-гейт на repo_agent просто не се пусна: цикълът изгори
    всичките 8 рунда, моделът си измисляше ad-hoc `python -c ...` проверки, а
    накрая поправка, която БЕШЕ вярна, се докладва като "промените НЕ са
    проверени — прегледай диффа преди да му вярваш". Точно проектите, върху
    които някой пуска `genesis fix` (скрипт, домашно, малък инструмент), най-
    рядко имат packaging маркер.

    Нарочно тесен критерий — pytest-овата собствена конвенция за имена
    (`test_*.py` / `*_test.py`), в корена или в отделна тестова директория, не
    "някъде в дървото": файл на име `test_helpers.py` вътре в пакета е
    помощен модул поне толкова често, колкото и тестов.
    """
    patterns = ("test_*.py", "*_test.py")
    for pat in patterns:
        if any(root.glob(pat)):
            return True
    for d in ("tests", "test"):
        sub = root / d
        if not sub.is_dir():
            continue
        for pat in patterns:
            if any(sub.rglob(pat)):
                return True
    return False


def detect_project(path: str | Path) -> ProjectInfo:
    root = Path(path).expanduser().resolve()
    counts: Counter = Counter()
    entries: list[str] = []
    for p in _iter_files(root):
        counts[p.suffix.lower()] += 1
        if p.name in ("main.py", "__main__.py", "app.py", "manage.py", "cli.py",
                      "index.js", "main.js", "main.go", "main.rs", "server.js"):
            try:
                entries.append(str(p.relative_to(root)))
            except ValueError:
                entries.append(str(p))

    language, test_cmd = "unknown", ""
    for marker, lang, cmd in _TEST_RULES:
        if not (root / marker).exists():
            continue
        # A marker file is not a promise that the command exists. Claiming
        # `npm test` on a package.json without a test script sends the repair
        # loop chasing a shell error instead of the bug it was asked to fix.
        if marker == "package.json" and not _package_json_has_test(root):
            language = language if language != "unknown" else lang
            continue
        if marker == "Makefile" and not _makefile_has_test(root):
            continue
        language, test_cmd = lang, cmd
        break
    # Няма маркерен файл, но има тестове по pytest конвенцията — виж
    # _has_pytest_style_tests за защо липсата на маркер не е доказателство.
    if not test_cmd and _has_pytest_style_tests(root):
        language, test_cmd = "python", f"{_python_cmd()} -m pytest -q"
    if language == "unknown" and counts:
        by_lang = {".py": "python", ".js": "javascript", ".ts": "typescript",
                   ".go": "go", ".rs": "rust", ".rb": "ruby", ".java": "java"}
        for suffix, _ in counts.most_common():
            if suffix in by_lang:
                language = by_lang[suffix]
                break

    is_git = (root / ".git").exists()
    dirty = False
    if is_git:
        try:
            proc = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                                  capture_output=True, text=True, timeout=15, check=False)
            dirty = bool(proc.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass

    return ProjectInfo(root, language, test_cmd, counts, entries[:10], is_git, dirty)


def repo_map(path: str | Path, max_dirs: int = 40) -> str:
    """A compact, model-readable summary of a project."""
    info = detect_project(path)
    total = sum(info.file_counts.values())
    top = ", ".join(f"{ext or '(без разширение)'}×{n}"
                    for ext, n in info.file_counts.most_common(8))

    dirs: list[str] = []
    for p in sorted(info.root.iterdir()):
        if p.name in _SKIP_DIRS or p.name.startswith("."):
            continue
        if p.is_dir():
            n = sum(1 for _ in _iter_files(p))
            dirs.append(f"  {p.name}/  ({n} файла)")
        else:
            dirs.append(f"  {p.name}")
        if len(dirs) >= max_dirs:
            dirs.append(f"  … (само първите {max_dirs})")
            break

    lines = [
        f"[REPO_MAP: {info.root}]",
        f"език: {info.language} · {total} файла с код · {top}",
        f"тестове: {info.test_command or 'не са открити — питай потребителя как се пускат'}",
        f"git: {'да' if info.is_git else 'НЕ (няма version control)'}"
        + (" · има некомитнати промени" if info.git_dirty else ""),
    ]
    if info.entry_points:
        lines.append("вход: " + ", ".join(info.entry_points))
    lines.append("съдържание:")
    lines.extend(dirs)
    return "\n".join(lines)
