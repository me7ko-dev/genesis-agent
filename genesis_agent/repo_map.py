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
import os
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
# Одит 2026-10-22: търсенето режеше тези имена на ВСЯКА дълбочина, а те са и
# обикновени папки с код — `src/env/schema.mjs` (create-t3-app),
# `packages/build/tool.py`, `.vscode/settings.json`. SEARCH_CODE отговаряше
# „низът наистина го няма“, GLOB — „няма файлове“, REPO_MAP — „src/ (0 файла)“.
# Същото правило като в checkpoints и repo_agent (одит 2026-10-07/09): само в
# корена, а по-надолу — само ако си личи, че е артефакт (виж _looks_like_artifact).
_ONLY_AT_TOP = {"build", "dist", "env", "target", "vendor"}
# Малки папки с настройки: съдържание са, търсят се. В _SKIP_DIRS остават заради
# checkpoints — IDE-то ги пренаписва постоянно.
_SEARCHED_CONFIG = {".idea", ".vscode"}
# Пропуснати, но не си струва да се споменават в празния отговор.
_QUIET_SKIPS = {".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
                ".ruff_cache"}
# Разширенията, които detect_project/REPO_MAP броят за код. SEARCH_CODE вече
# не филтрира по тях (пропуска двоичните по NUL байт, като rg — 2026-10-11).
# .mjs/.cjs/.mts/.cts/.astro (одит 2026-10-22): ESM проект с 5 такива файла в
# src/ излизаше „src/ (0 файла)“, „.json×1“ — грешна предпоставка от REPO_MAP.
_TEXT_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".rb", ".java",
    ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php", ".swift", ".sh",
    ".bash", ".zsh", ".sql", ".html", ".css", ".scss", ".vue", ".svelte",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".json", ".md", ".rst", ".txt",
    ".tf", ".gradle", ".lua", ".pl", ".r", ".m", ".mm", ".dart", ".ex", ".exs",
    ".mjs", ".cjs", ".mts", ".cts", ".astro", ".ps1", ".bat", ".proto", ".graphql",
}
_MAX_FILE_BYTES = 2_000_000   # a source file bigger than this is generated
_DEFAULT_MAX_RESULTS = 60
_LINE_CLIP = 200              # a matched minified line must not eat the window


@dataclass
class Match:
    path: str
    line: int
    text: str


def _looks_like_artifact(path: Path) -> bool:
    """Вложена `build/`, `env/`, `target/`… е артефакт, ако си личи: venv
    (pyvenv.cfg), кеш (CACHEDIR.TAG) или `target` до Cargo.toml — същите
    признаци като repo_agent._skip_in_snapshot."""
    try:
        return ((path / "pyvenv.cfg").is_file() or (path / "CACHEDIR.TAG").is_file()
                or (path.name == "target" and (path.parent / "Cargo.toml").is_file()))
    except OSError:
        return False


def _pruned_dir(name: str, depth: int, path: Path) -> bool:
    """Не се слиза ли в тази папка. `depth` е спрямо корена на ПРОЕКТА (0 = пряко в него)."""
    if name in _SEARCHED_CONFIG or name not in _SKIP_DIRS:
        return False
    if name not in _ONLY_AT_TOP or depth == 0:
        return True
    return _looks_like_artifact(path)


def _lead(root: Path, top: Path | None) -> tuple[str, ...]:
    """Частите от корена на проекта (`top`) до търсената папка — за дълбочината."""
    if top is None:
        return ()
    try:
        return root.absolute().relative_to(top.absolute()).parts
    except ValueError:
        return ()


def _skipped(p: Path, root: Path, top: Path | None = None) -> bool:
    """В папка за пропускане ВЪТРЕ в проекта. Само частите под корена:
    проект в `~/build/app` или `~/env/proj` иначе губеше всеки свой файл
    (2026-10-07: GLOB „няма файлове“, REPO_MAP „0 файла с код“)."""
    try:
        parts = p.relative_to(root).parts[:-1]
    except ValueError:
        parts = p.parts[:-1]
    lead = _lead(root, top)
    return any(_pruned_dir(part, len(lead) + i, root.joinpath(*parts[:i + 1]))
               for i, part in enumerate(parts))


def skipped_here(root: Path, top: Path | None = None) -> list[str]:
    """Папките пряко в `root`, в които търсенето НЕ влиза — за празния отговор.
    Одит 2026-10-22: „низът наистина го няма“ при пропуснат node_modules/ беше
    лъжа; моделът трябва да знае къде не е гледано."""
    lead = _lead(root, top)
    try:
        return sorted(f"{p.name}/" for p in root.iterdir()
                      if p.name not in _QUIET_SKIPS and p.is_dir() and not p.is_symlink()
                      and _pruned_dir(p.name, len(lead), p))
    except OSError:
        return []


def _walk(root: Path, match=None, *, top: Path | None = None):
    """Файловете под `root`, без да се слиза в пропуснатите папки (_pruned_dir).
    rglob влизаше в node_modules и чак после ги отхвърляше: 2026-10-09, 100k
    файла там → detect_project 3.3 s, REPO_MAP 3.1 s, търсенето 2.9 s на всяко
    извикване. `match(части на пътя спрямо root)` избира файловете (виж _matcher).

    Редът е този на `rg --sort path` (имената в папка подредени, папки и файлове
    вместо първо файловете): одит 2026-10-22 — при тавана от 60 двата двигателя
    връщаха РАЗЛИЧНИ 60 попадения, а rg без --sort — различни при всяко
    извикване (5 различни отговора от 5 пуска). Символна връзка към папка не се
    следва, както при os.walk."""
    lead = _lead(root, top)
    stack: list[tuple[str, tuple[str, ...]] | Path] = [(os.fspath(root), ())]
    while stack:
        item = stack.pop()
        if isinstance(item, Path):
            yield item
            continue
        path, rel = item
        try:
            with os.scandir(path) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        children: list[tuple[str, tuple[str, ...]] | Path] = []
        for e in entries:
            try:
                is_dir = e.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                if not e.is_symlink() and not _pruned_dir(e.name, len(lead) + len(rel), Path(e.path)):
                    children.append((e.path, (*rel, e.name)))
            elif match is None or match((*rel, e.name)):
                children.append(Path(e.path))
        stack.extend(reversed(children))


def _expand_braces(pattern: str, limit: int = 256) -> list[str]:
    """`*.{ts,tsx}` → `*.ts`, `*.tsx` (и вложени `{a,{b,c}}`). Одит 2026-10-22:
    fnmatch няма {}, GLOB `**/*.{ts,tsx}` беше „няма файлове“, а SEARCH_CODE с
    такъв glob намираше през rg и „наистина го няма“ без него. `{x}` без
    запетая остава буквално, както в bash."""
    depth, start = 0, -1
    for i, ch in enumerate(pattern):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth:
                continue
            alts, level, cur = [], 0, ""
            for c in pattern[start + 1:i]:
                if c == "," and level == 0:
                    alts.append(cur)
                    cur = ""
                    continue
                level += (c == "{") - (c == "}")
                cur += c
            alts.append(cur)
            if len(alts) < 2:
                continue
            out: list[str] = []
            for alt in alts:
                for v in _expand_braces(pattern[:start] + alt + pattern[i + 1:], limit):
                    if v not in out:
                        out.append(v)
                    if len(out) >= limit:
                        return out
            return out
    return [pattern]


_GLOB_CHARS = "*?[{"


def split_glob(pattern: str, root: Path) -> tuple[Path, str, bool]:
    """(корен, остатък, закотвен ли е остатъкът към корена).

    Абсолютен шаблон или такъв с `..` — коренът става буквалната част до
    първия сегмент с `* ? [ {`. Одит 2026-10-22: `GLOB ../.ssh/*` минаваше
    през rglob, излизаше от workspace-а и изреждаше имената на ключовете, а
    пазачът гледаше само workspace-а; `GLOB /абсолютен/**/*.py` падаше с
    „Non-relative patterns are unsupported“. Сега истинският корен минава през
    същия пазач като `GLOB * | ~/.ssh`. Обикновеният относителен шаблон не се
    пипа — той е като rglob (`src/*.py` хваща и `pkg/src/*.py`)."""
    pure = Path(pattern)
    if not pure.is_absolute() and ".." not in pure.parts:
        return root, pattern, False
    parts = pure.parts
    cut = next((i for i, seg in enumerate(parts) if any(c in seg for c in _GLOB_CHARS)),
               len(parts) - 1)
    if pure.is_absolute():
        cut = max(cut, 1)
    if ".." in parts[cut:]:
        raise ValueError("`..` след `*`/`**` не се поддържа — подай папката като път: "
                         "[GLOB: шаблон | папка]")
    if cut >= len(parts):
        raise ValueError("шаблонът няма име на файл — напр. [GLOB: **/*.py | папка]")
    base = Path(*parts[:cut]) if cut else Path(".")
    new_root = Path(os.path.normpath(base if pure.is_absolute() else root / base))
    return new_root, "/".join(parts[cut:]), True


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


def _matcher(pattern: str, *, anchored: bool = False):
    """Шаблонът като rglob (`**/` отпред), но върху вече подрязания обход.
    Одит 2026-10-09: `**/components/*.tsx` минаваше по стария път и пак
    обхождаше node_modules (1.1 s на 40k файла). Сравнява сегмент по сегмент
    с fnmatch — `*` не минава през `/`, а `**` е нула или повече папки.
    `anchored` — без `**/` отпред (остатъкът от split_glob).
    None = необичаен шаблон (абсолютен, `..`, `\\`, завършващ на `/`) → rglob."""
    if (not pattern or pattern.startswith("/") or pattern.endswith("/") or "\\" in pattern
            or ".." in pattern.split("/") or ":" in pattern):
        return None
    segs: list[str] = [] if anchored else ["**"]
    for seg in pattern.split("/"):
        if seg in ("", "."):
            continue
        if seg == "**" and segs and segs[-1] == "**":
            continue
        segs.append(seg)
    if not segs or segs[-1] == "**":
        return None
    frozen = tuple(segs)
    return lambda parts: _segments_match(tuple(parts), frozen)


def _files(root: Path, pattern: str, *, anchored: bool = False, top: Path | None = None):
    """Файловете по шаблон — подрязаният обход, или rglob за необичайния."""
    variants = _expand_braces(pattern)
    matchers = [_matcher(v, anchored=anchored) for v in variants]
    if all(m is not None for m in matchers):
        if len(matchers) == 1:
            return _walk(root, matchers[0], top=top)
        return _walk(root, lambda parts: any(m(parts) for m in matchers), top=top)
    return _rglob(root, variants, anchored, top)


def _rglob(root: Path, variants: list[str], anchored: bool, top: Path | None):
    # Само файлове ПОД корена (одит 2026-10-22): `**/../../.ssh/*` като glob на
    # SEARCH_CODE иначе излизаше навън — test_find_files_does_not_escape_the_root.
    # По текста на пътя, не resolve(): символна връзка в проекта не е бягство.
    inside = Path(os.path.normpath(root))
    seen: set[Path] = set()
    for v in variants:
        for p in (root.glob(v) if anchored else root.rglob(v)):
            if p in seen or not Path(os.path.normpath(p)).is_relative_to(inside):
                continue
            if p.is_dir() or _skipped(p, root, top):
                continue
            seen.add(p)
            yield p


def _iter_files(root: Path, glob: str | None = None, *, any_suffix: bool = False,
                top: Path | None = None):
    # Файл като корен е самият той — не шаблон по името му (виж search_code).
    files = ([root] if root.is_file() else _walk(root, top=top) if glob is None
             else _files(root, glob, top=top))
    for p in files:
        if glob is None and not any_suffix and p.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            if p.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield p


# ASCII класовете на rg (Rust regex) — само вътре в `[...]`, като там.
_POSIX_CLASSES = {
    "alnum": "0-9A-Za-z", "alpha": "A-Za-z", "ascii": "\\x00-\\x7F", "blank": "\\t ",
    "cntrl": "\\x00-\\x1F\\x7F", "digit": "0-9", "graph": "!-~", "lower": "a-z",
    "print": " -~", "punct": "!-/:-@\\[-`{-~", "space": "\\t\\n\\x0B\\f\\r ",
    "upper": "A-Z", "word": "0-9A-Za-z_", "xdigit": "0-9A-Fa-f",
}


def _posix_classes(pattern: str) -> str:
    r"""`[[:upper:]]` → `[A-Z]` за Python пътя.

    Одит 2026-10-22: без rg (обичайното на Windows лаптопа) `[[:upper:]]oo`
    се компилираше само с FutureWarning, не съвпадаше с нищо и инструментът
    казваше „низът наистина го няма“, а rg намираше реда."""
    out: list[str] = []
    i, n, in_set = 0, len(pattern), False
    while i < n:
        c = pattern[i]
        if c == "\\":
            out.append(pattern[i:i + 2])
            i += 2
            continue
        if not in_set:
            out.append(c)
            i += 1
            if c == "[":
                in_set = True
                for lead in ("^", "]"):   # `[^]…]`, `[]…]` — `]` там е знак
                    if pattern.startswith(lead, i):
                        out.append(lead)
                        i += 1
            continue
        if c == "]":
            in_set = False
        elif pattern.startswith("[:", i):
            end = pattern.find(":]", i + 2)
            name = pattern[i + 2:end] if end != -1 else ""
            if name in _POSIX_CLASSES:
                out.append(_POSIX_CLASSES[name])
                i = end + 2
                continue
            if name.startswith("^") and name[1:] in _POSIX_CLASSES:
                raise ValueError(f"невалиден regex: `[:{name}:]` без ripgrep не се поддържа — "
                                 f"ползвай `[^[:{name[1:]}:]]`")
        out.append(c)
        i += 1
    return "".join(out)


def _compile(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(_posix_classes(pattern))
    except re.error as e:
        # `\p{Lu}` rg разбира, Python `re` — не (одит 2026-10-22): ясно указание,
        # не голото „bad escape \p“, за да не изглежда като „няма го“.
        if re.search(r"(?<!\\)(?:\\\\)*\\[pP]", pattern):
            raise ValueError(f"невалиден regex: {e} — `\\p{{…}}` (Unicode класове) работи само "
                             "с ripgrep; без него ползвай изброяване като `[A-ZА-Я]`") from e
        raise ValueError(f"невалиден regex: {e}") from e


def _clip(line: str, start: int, end: int) -> str:
    """Около съвпадението, не първите _LINE_CLIP знака. Одит 2026-10-22: в
    минифициран файл съвпадението на колона ~3200 се отрязваше и „попадението“
    не съдържаше търсения низ, без знак, че нещо липсва. `…` там, където е рязано."""
    line = line.rstrip()
    if len(line) <= _LINE_CLIP:
        return line
    width = _LINE_CLIP - 2
    start = max(0, min(start, len(line)))
    end = max(start, min(end, len(line)))
    left = start if end - start >= width else start - (width - (end - start)) // 2
    left = max(0, min(left, len(line) - width))
    return ("…" if left else "") + line[left:left + width] + ("…" if left + width < len(line) else "")


def _search_python(root: Path, pattern: str, glob: str | None,
                   max_results: int, top: Path | None = None) -> list[Match]:
    rx = _compile(pattern)
    out: list[Match] = []
    wants_cr = _wants_cr(pattern)
    # Без филтър по разширение — текст или двоичен се решава като при rg (NUL в
    # първите 8 KB). Одит 2026-10-11: без rg (обичайното на Windows лаптопа)
    # Dockerfile, Makefile, .ps1, .bat, .env.example, .xml, .mts, .cjs бяха
    # „няма съвпадения“, а rg ги намираше 8 от 8.
    for p in _iter_files(root, glob, any_suffix=True, top=top):
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
            m = rx.search(line)
            if m:
                out.append(Match(str(p), i, _clip(line, *m.span())))
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
                    max_results: int, top: Path | None = None) -> list[Match] | None:
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

      --sort path     същият ред като _walk — при тавана двата двигателя дават
                      едни и същи попадения, и при всяко извикване (одит 2026-10-22)
    """
    out = _run_ripgrep(root, pattern, glob, max_results, None, top)
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
        extra = _run_ripgrep(root, pattern, glob, max_results, rg_enc, top)
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
                 max_results: int, encoding: str | None,
                 top: Path | None = None) -> list[Match] | None:
    cmd = ["rg", "--json", "--max-count", str(max_results), "--sort", "path",
           "--hidden", "--no-ignore", "--max-filesize", str(_MAX_FILE_BYTES),
           *([] if _wants_cr(pattern) else ["--crlf"]), "-e", pattern]
    if encoding:
        cmd += ["--encoding", encoding]
    # `!/build/` е закотвен към cwd на rg (мерено с rg 14.1, 2026-10-22) —
    # затова cwd е търсената папка: `build/` в корена се пропуска, `src/build/`
    # не. Под подпапка на проекта (`lead`) такива имена вече не са в корена му.
    root_abs = root.absolute()
    cwd = root_abs if root_abs.is_dir() else root_abs.parent
    lead = _lead(root, top)
    base = top.absolute() if top is not None and lead else cwd
    for d in sorted(_SKIP_DIRS - _SEARCHED_CONFIG):
        if d not in _ONLY_AT_TOP:
            cmd += ["--glob", f"!{d}/"]
        elif not lead:
            cmd += ["--glob", f"!/{d}/"]
    for g in _rg_globs(glob):
        cmd += ["--glob", g]
    cmd.append(str(root_abs))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=30, check=False, cwd=str(cwd))
    except (OSError, subprocess.TimeoutExpired):
        return None
    # rc 1 is "no matches" — a real answer, not a failure. Anything above that
    # (bad pattern, unreadable tree) goes to the Python path so the user gets
    # a proper error message instead of ripgrep's exit code.
    if proc.returncode not in (0, 1):
        return None
    out: list[Match] = []
    known: dict[str, str] = {}
    artifacts: dict[Path, bool] = {}
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
        if _in_nested_artifact(Path(path), base, artifacts):
            continue
        if not root.is_absolute():
            # Пътят, както го дава Python пътят — спрямо подадения корен.
            try:
                path = str(root / Path(path).relative_to(root_abs))
            except ValueError:
                pass
        lines = data.get("lines") or {}
        text = _rg_text(lines, path, known)
        start, end = _rg_span(data, lines, text, path, known)
        out.append(Match(path, line_no, _clip(text, start, end)))
        if len(out) >= max_results:
            break
    return out


def _in_nested_artifact(path: Path, base: Path, cache: dict[Path, bool]) -> bool:
    """Вложен venv/кеш под `build/`, `env/`… — rg няма как да ги познае по
    glob, затова се махат тук, със същото правило като _walk (_pruned_dir)."""
    try:
        parts = path.relative_to(base).parts[:-1]
    except ValueError:
        return False
    for depth, name in enumerate(parts):
        if depth and name in _ONLY_AT_TOP:
            here = base.joinpath(*parts[:depth + 1])
            if here not in cache:
                cache[here] = _looks_like_artifact(here)
            if cache[here]:
                return True
    return False


def _rg_span(data: dict, lines: dict, text: str, path: str,
             known: dict[str, str]) -> tuple[int, int]:
    """Къде в `text` е първото съвпадение. rg дава отместване в БАЙТОВЕ на
    реда, а `text` е декодиран — превръща се през същото декодиране."""
    subs = data.get("submatches") or []
    sub = subs[0] if subs and isinstance(subs[0], dict) else {}
    start, end = sub.get("start"), sub.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return 0, 0
    if isinstance(lines.get("text"), str):
        raw = lines["text"].encode("utf-8", "surrogateescape")
        return (len(raw[:start].decode("utf-8", "ignore")),
                len(raw[:end].decode("utf-8", "ignore")))
    try:
        raw = base64.b64decode(lines.get("bytes") or "")
    except (ValueError, TypeError):
        return 0, 0
    return (len(_rg_text({"bytes": base64.b64encode(raw[:start]).decode()}, path, known)),
            len(_rg_text({"bytes": base64.b64encode(raw[:end]).decode()}, path, known)))


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


_GLOB_SCAN_CAP = 20_000   # над толкова кандидата GLOB казва „поне“, не брои до край


def find_files(pattern: str, path: str | Path = ".",
               max_results: int = 200) -> list[str]:
    """Find files by NAME pattern (e.g. '**/*.tsx', 'test_*.py') — the filename
    counterpart to search_code's content grep. Skips the same noise dirs
    (.git, node_modules, venvs...) as everything else in this module."""
    return find_files_counted(pattern, path, max_results)[0]


def find_files_counted(pattern: str, path: str | Path = ".", max_results: int = 200,
                       *, top: Path | None = None) -> tuple[list[str], int]:
    """(показаните, колко съвпадат общо). Над `max_results` остават НАЙ-НОВИТЕ
    по mtime, подредени по име. Одит 2026-10-22: събираше в азбучния ред на
    обхода и спираше на 200 — току-що редактираният `src/app.py` зад 230 стари
    `docs/*.md` изчезваше, а заглавието „200 файла“ изглеждаше като всичко."""
    root, pattern, anchored = split_glob(pattern, Path(path).expanduser())
    if not root.exists():
        raise FileNotFoundError(f"няма такъв път: {root}")
    found: list[Path] = []
    for p in _files(root, pattern, anchored=anchored, top=top):
        found.append(p)
        if len(found) >= _GLOB_SCAN_CAP:
            break
    total = len(found)
    if total > max_results:
        def mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0
        found = sorted(found, key=mtime, reverse=True)[:max_results]
    out: list[str] = []
    for p in found:
        try:
            out.append(str(p.relative_to(root)))
        except ValueError:
            out.append(str(p))
    out.sort()
    return out, total


def search_code(pattern: str, path: str | Path = ".", glob: str | None = None,
                max_results: int = _DEFAULT_MAX_RESULTS, *,
                top: Path | None = None) -> list[Match]:
    """Regex search across a tree. Prefers ripgrep, falls back to Python.
    `top` — коренът на проекта, ако `path` е подпапка в него (виж _pruned_dir)."""
    root = Path(path).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"няма такъв път: {root}")
    if root.is_file():
        # Само този файл — подаден направо на двата енджина. Преди името му
        # ставаше glob, а `[slug].tsx` е клас знаци и за rg, и за fnmatch:
        # „низът наистина го няма“ във файл, който го съдържа (одит 2026-10-11).
        glob = None
    hits = _search_ripgrep(root, pattern, glob, max_results, top) if shutil.which("rg") else None
    if hits is None:
        hits = _search_python(root, pattern, glob, max_results, top)
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
            # Дълбочината спрямо проекта: `packages/build/` е код (одит 2026-10-22).
            n = sum(1 for _ in _iter_files(p, top=info.root))
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
