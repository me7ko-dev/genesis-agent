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

import base64
import json
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from genesis_agent.code_edit import decode_text, displayable

_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".venv", "venv", "env", ".env",
    "dist", "build", "target", ".next", ".tox", ".gradle", "vendor",
    "site-packages", ".verify_libs", ".idea", ".vscode",
}
# Разширенията, които detect_project/REPO_MAP броят за код. SEARCH_CODE вече
# не филтрира по тях (пропуска двоичните по NUL байт, като rg — 2026-10-11).
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


def _skipped(p: Path, root: Path) -> bool:
    """В папка за пропускане ВЪТРЕ в проекта. Само частите под корена:
    проект в `~/build/app` или `~/env/proj` иначе губеше всеки свой файл
    (2026-10-07: GLOB „няма файлове“, REPO_MAP „0 файла с код“)."""
    try:
        parts = p.relative_to(root).parts[:-1]
    except ValueError:
        parts = p.parts[:-1]
    return any(part in _SKIP_DIRS for part in parts)


def _walk(root: Path, match=None):
    """Файловете под `root`, без да се слиза в _SKIP_DIRS. rglob влизаше в
    node_modules и чак после ги отхвърляше: 2026-10-09, 100k файла там →
    detect_project 3.3 s, REPO_MAP 3.1 s, търсенето 2.9 s на всяко извикване.
    `match(части на пътя спрямо root)` избира файловете (виж _matcher)."""
    import os
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        rel = Path(dirpath).relative_to(root).parts
        for name in sorted(filenames):
            if match is None or match((*rel, name)):
                yield Path(dirpath) / name


def _segments_match(parts: tuple[str, ...], segs: tuple[str, ...]) -> bool:
    import fnmatch
    if not segs:
        return not parts
    if segs[0] == "**":
        return any(_segments_match(parts[i:], segs[1:]) for i in range(len(parts) + 1))
    # Буквалното име печели преди fnmatch: `[slug].tsx` (Next.js, SvelteKit) е
    # клас знаци за fnmatch и GLOB отговаряше „няма файлове“ (одит 2026-10-11).
    return (bool(parts) and (parts[0] == segs[0] or fnmatch.fnmatch(parts[0], segs[0]))
            and _segments_match(parts[1:], segs[1:]))


def _matcher(pattern: str):
    """Шаблонът като rglob (`**/` отпред), но върху вече подрязания обход.
    Одит 2026-10-09: `**/components/*.tsx` минаваше по стария път и пак
    обхождаше node_modules (1.1 s на 40k файла). Сравнява сегмент по сегмент
    с fnmatch — `*` не минава през `/`, а `**` е нула или повече папки.
    None = необичаен шаблон (абсолютен, `..`, `\\`, завършващ на `/`) → rglob."""
    if (not pattern or pattern.startswith("/") or pattern.endswith("/") or "\\" in pattern
            or ".." in pattern.split("/") or ":" in pattern):
        return None
    segs: list[str] = ["**"]
    for seg in pattern.split("/"):
        if seg in ("", "."):
            continue
        if seg == "**" and segs[-1] == "**":
            continue
        segs.append(seg)
    if segs[-1] == "**":
        return None
    frozen = tuple(segs)
    return lambda parts: _segments_match(tuple(parts), frozen)


def _files(root: Path, pattern: str):
    """Файловете по шаблон — подрязаният обход, или rglob за необичайния."""
    match = _matcher(pattern)
    if match is not None:
        return _walk(root, match)
    return (p for p in root.rglob(pattern) if not p.is_dir() and not _skipped(p, root))


def _iter_files(root: Path, glob: str | None = None, *, any_suffix: bool = False):
    # Файл като корен е самият той — не шаблон по името му (виж search_code).
    files = [root] if root.is_file() else _walk(root) if glob is None else _files(root, glob)
    for p in files:
        if glob is None and not any_suffix and p.suffix.lower() not in _TEXT_SUFFIXES:
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
    wants_cr = _wants_cr(pattern)
    # Без филтър по разширение — текст или двоичен се решава като при rg (NUL в
    # първите 8 KB). Одит 2026-10-11: без rg (обичайното на Windows лаптопа)
    # Dockerfile, Makefile, .ps1, .bat, .env.example, .xml, .mts, .cjs бяха
    # „няма съвпадения“, а rg ги намираше 8 от 8.
    for p in _iter_files(root, glob, any_suffix=True):
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:8192] and not raw.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
            continue
        # Кодирането по файла (одит 2026-10-11): „Грешка“ в cp1251 файл даваше
        # „няма съвпадения … наистина го няма“. Редовете се делят само по `\n`,
        # както при rg и EDIT_FILE, за да съвпадат номерата.
        text = displayable(decode_text(raw)[0])
        for i, line in enumerate(text.split("\n"), 1):
            # `\r` от CRLF не е част от реда (ревю 2026-10-11): `return 1$`
            # не съвпадаше в CRLF файл и инструментът казваше „наистина го
            # няма“ — старият текстов режим го махаше. rg е с `--crlf`.
            # Освен когато се търси самият `\r` (виж _wants_cr).
            if not wants_cr:
                line = line.removesuffix("\r")
            if rx.search(line):
                out.append(Match(str(p), i, line.rstrip()[:_LINE_CLIP]))
                if len(out) >= max_results:
                    return out
    return out


def _wants_cr(pattern: str) -> bool:
    r"""Търси ли шаблонът самия `\r` — напр. `\r$`, за да намери CRLF файловете.

    Ревю 2026-10-11 (трета вълна), мерено с rg 14.1: с `--crlf` rg не
    намира `\r` изобщо (и по средата на реда), а Python пътят го махаше от
    края — `\r$` беше „няма съвпадения“ и в двата, а rg без флага го
    намираше. За такъв шаблон `\r` остава част от реда и в двата пътя.
    Широко нарочно: `\\r` (буквално `\` и `r`) също минава оттук — цената
    е само, че в това търсене `$` пред CRLF е както преди вълна 2.
    """
    low = pattern.lower()
    return "\r" in pattern or "\\r" in pattern or "\\x0d" in low or "\\015" in pattern


def _search_ripgrep(root: Path, pattern: str, glob: str | None,
                    max_results: int) -> list[Match] | None:
    """None means 'ripgrep could not answer' — the caller falls back.

    The flags exist to make this path answer the SAME question as
    `_search_python`, because which one runs depends only on whether `rg`
    happens to be installed. Measured on this repository before they were
    added: `search_code("Windows installer self-test", ".")` returned 0 hits
    with ripgrep and 1 without — the string is in `.github/workflows/ci.yml`,
    and `.github` is a hidden directory, which ripgrep skips by default. The
    tool that wraps this tells the model "it really is not there; do not assume
    it is hidden", so an engine that quietly skips files makes that a lie.

      --hidden        `.github/`, `.claude/`, `.env.example` are real content
      --no-ignore     `.gitignore` applies to rg and not to the Python path;
                      generated trees are excluded by `_SKIP_DIRS` instead,
                      which BOTH paths honour
      --max-filesize  the Python path skips files over `_MAX_FILE_BYTES`
      --crlf          `$` съвпада пред `\r\n`, както Python пътят го маха
                      (ревю 2026-10-11)

    `.git/` stays excluded because it is in `_SKIP_DIRS`, which is turned into
    `--glob !.git/` below — so `--hidden --no-ignore` does not drag in object
    files.
    """
    out = _run_ripgrep(root, pattern, glob, max_results, None)
    # rg търси байтовете на шаблона в UTF-8, а cp1251 файл ги няма (одит
    # 2026-10-11: „Грешка“ → „наистина го няма“). Шаблон с не-ASCII знаци се
    # пуска втори път с windows-1251. От него се взимат само файловете, които
    # decode_text (и Python пътят) смята за cp1251: ревю 2026-10-11 — UTF-8
    # „мир“ там е „РјРёСЂ“ и търсене на „Р“ връщаше боклук, а Python — нищо.
    # Същото за cp1252 (2026-10-15): „crème“ в latin-1 файл.
    for rg_enc, enc in (("windows-1251", "cp1251"), ("windows-1252", "cp1252")):
        if out is None or pattern.isascii() or len(out) >= max_results:
            break
        if enc == "cp1252" and not any(not c.isascii() and _fits(c, enc) for c in pattern):
            continue   # само кирилица в шаблона: в cp1252 файл тя няма как да е
        extra = _run_ripgrep(root, pattern, glob, max_results, rg_enc)
        seen = {(m.path, m.line) for m in out}
        known: dict[str, str] = {}
        for m in extra or []:
            if len(out) >= max_results:
                break
            if _file_encoding(m.path, known) == enc and (m.path, m.line) not in seen:
                out.append(m)
    return out


def _fits(text: str, encoding: str) -> bool:
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        return False
    return True


def _file_encoding(path: str, known: dict[str, str]) -> str:
    """Кодирането на файла — както Python пътят го чете (decode_text);
    `known` пази вече прочетените в едно търсене."""
    if path not in known:
        try:
            known[path] = decode_text(Path(path).read_bytes())[1]
        except OSError:
            known[path] = "utf-8"
    return known[path]


def _run_ripgrep(root: Path, pattern: str, glob: str | None,
                 max_results: int, encoding: str | None) -> list[Match] | None:
    cmd = ["rg", "--json", "--max-count", str(max_results),
           "--hidden", "--no-ignore", "--max-filesize", str(_MAX_FILE_BYTES),
           *([] if _wants_cr(pattern) else ["--crlf"]), "-e", pattern]
    if encoding:
        cmd += ["--encoding", encoding]
    for d in sorted(_SKIP_DIRS):
        cmd += ["--glob", f"!{d}/"]
    for g in _rg_globs(glob):
        cmd += ["--glob", g]
    cmd.append(str(root))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # rc 1 is "no matches" — a real answer, not a failure. Anything above that
    # (bad pattern, unreadable tree) goes to the Python path so the user gets
    # a proper error message instead of ripgrep's exit code.
    if proc.returncode not in (0, 1):
        return None
    out: list[Match] = []
    known: dict[str, str] = {}
    for raw in proc.stdout.splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if ev.get("type") != "match":
            continue
        # Всичко през .get(): това е изход на ВЪНШНА програма, чийто JSON
        # формат зависи от версията ѝ. Директният достъп вдигаше KeyError,
        # който минава покрай `except (OSError, TimeoutExpired)` по-горе и
        # излиза от функцията — при положение че целият ѝ договор е "не можах
        # да отговоря → None, извикващият пада към Python пътя". Един непознат
        # вариант на match убиваше търсенето, вместо да го прехвърли на
        # резервния път, който работи винаги.
        data = ev.get("data") or {}
        line_no = data.get("line_number")
        if not isinstance(line_no, int):
            continue
        path = (data.get("path") or {}).get("text", "?")
        out.append(Match(path, line_no, _rg_text(data.get("lines") or {}, path, known).rstrip()[:_LINE_CLIP]))
        if len(out) >= max_results:
            break
    return out


def _rg_globs(glob: str | None) -> list[str]:
    """`glob` за rg, така че да избира същите файлове като `_matcher`.

    Ревю 2026-10-11, мерено с rg 14.1: `--glob pages/*.tsx` се мери спрямо
    cwd, не спрямо търсената папка — когато workspace-ът не е cwd, rg
    връщаше „няма съвпадения“, а Python пътят намираше файла. `_matcher`
    слага `**/` отпред, тук също. `[slug].tsx` е клас знаци за rg, а
    `_segments_match` приема и буквалното име — затова второ `--glob` с
    `[[]` (буквално `[`); няколко `--glob` са „или“.
    """
    if not glob:
        return []
    neg, body = ("!", glob[1:]) if glob.startswith("!") else ("", glob)
    # `./src/*.py` и `src/./*.py`: `_matcher` пропуска сегментите `.`, а rg ги
    # мери буквално и не намираше нищо (ревю 2026-10-11, трета вълна).
    # Абсолютен или завършващ на `/` шаблон `_matcher` не приема — него не пипаме.
    if not body.startswith("/") and not body.endswith("/"):
        body = "/".join(seg for seg in body.split("/") if seg not in ("", ".")) or body
    if "/" in body and not body.startswith(("/", "**/")):
        body = "**/" + body
    variants = [body] + ([body.replace("[", "[[]")] if "[" in body else [])
    return [neg + v for v in variants]


def _rg_text(lines: dict, path: str, known: dict[str, str]) -> str:
    """Редът от събитие на rg. Ред, който не е UTF-8, идва като `bytes`
    (base64), не като `text` — попадението излизаше с празен текст
    („msg.txt:1: “, одит 2026-10-11).

    Байтовете се четат с кодирането на ЦЕЛИЯ файл, както в Python пътя
    (2026-10-15): ред „Да ok“ сам не е „ясно cp1251“ и излизаше „�� ok“, а
    „NAME = 'Café'“ в cp1252 файл — според догадката по един ред."""
    if isinstance(lines.get("text"), str):
        return lines["text"]
    try:
        raw = base64.b64decode(lines.get("bytes") or "")
    except (ValueError, TypeError):
        return ""
    enc = _file_encoding(path, known)
    if enc in ("utf-8", "cp1251", "cp1252"):
        return displayable(raw.decode(enc, "surrogateescape"))
    return displayable(decode_text(raw)[0])


def find_files(pattern: str, path: str | Path = ".",
               max_results: int = 200) -> list[str]:
    """Find files by NAME pattern (e.g. '**/*.tsx', 'test_*.py') — the filename
    counterpart to search_code's content grep. Skips the same noise dirs
    (.git, node_modules, venvs...) as everything else in this module."""
    root = Path(path).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"няма такъв път: {root}")
    out: list[str] = []
    for p in _files(root, pattern):
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
        # Само този файл — подаден направо на двата енджина. Преди името му
        # ставаше glob, а `[slug].tsx` е клас знаци и за rg, и за fnmatch:
        # „низът наистина го няма“ във файл, който го съдържа (одит 2026-10-11).
        glob = None
    hits = _search_ripgrep(root, pattern, glob, max_results) if shutil.which("rg") else None
    if hits is None:
        hits = _search_python(root, pattern, glob, max_results)
    return hits


# ── Project shape ────────────────────────────────────────────────────────────

def _python_cmd(root: Path | None = None) -> str:
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

    В родния Windows билд `sys.executable` е genesis.exe, не Python, а в pipx е
    venv без пакетите на проекта — затова пътят идва от paths.project_python(root),
    който предпочита venv-а на проекта и иначе пита истинския Python.
    """
    from genesis_agent.paths import project_python
    exe = project_python(root).replace("\\", "/")
    return f'"{exe}"' if " " in exe else exe


# Ordered: the first match wins, so a Python project that also carries a
# package.json for its docs site is still tested with pytest. `{python}` is
# filled in at use, not at import: in the native build finding the real Python
# costs a subprocess, which every start would otherwise pay.
_TEST_RULES: list[tuple[str, str, str]] = [
    # (marker file, language, test command)
    ("pytest.ini", "python", "{python} -m pytest -q"),
    ("tox.ini", "python", "{python} -m pytest -q"),
    ("pyproject.toml", "python", "{python} -m pytest -q"),
    ("setup.py", "python", "{python} -m pytest -q"),
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
    test = str((data.get("scripts") or {}).get("test") or "")
    # Заготовката на `npm init -y` („Error: no test specified“ && exit 1) не е
    # тест — с нея всеки проект „падаше“ (одит 2026-10-09).
    return bool(test) and "no test specified" not in test


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
        language, test_cmd = lang, cmd.replace("{python}", _python_cmd(root))
        break
    # Няма маркерен файл, но има тестове по pytest конвенцията — виж
    # _has_pytest_style_tests за защо липсата на маркер не е доказателство.
    if not test_cmd and _has_pytest_style_tests(root):
        language, test_cmd = "python", f"{_python_cmd(root)} -m pytest -q"
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
        # Чуждото хранилище не пуска програми (core.fsmonitor, филтри):
        # REPO_MAP само чете — без въпрос и в plan mode (одит 2026-10-11).
        from genesis_agent.review import safe_git
        dirty = bool(safe_git(root, "status", "--porcelain", timeout=15)[1].strip())

    return ProjectInfo(root, language, test_cmd, counts, entries[:10], is_git, dirty)


def repo_map(path: str | Path, max_dirs: int = 40) -> str:
    """A compact, model-readable summary of a project.

    A file as the root is a routine call — the model has just read
    `src/main.py` and asks to be shown around it. Before, that raised
    NotADirectoryError, which `_tool_repo_map` turned into
    `[REPO_MAP] Грешка: [Errno 20] Not a directory: …`: an errno where an
    instruction belonged. `search_code` already handles the same input by
    working on the file's directory, so this does too, and says that it did.
    """
    given = Path(path).expanduser()
    note = ""
    if given.is_file():
        note = f"(подаден е файл — картирам папката му: {given.name})\n"
        path = given.parent
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
    return note + "\n".join(lines)
