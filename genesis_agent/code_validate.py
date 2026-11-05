"""
genesis_agent.code_validate — lint generated code with ruff BEFORE it hits the
sandbox loop.

Why: the sandbox is a real subprocess (venv setup for needs_deps skills,
CPU/memory limits, a timeout) — obvious syntax errors and dead imports don't
need that round-trip to be caught. Ruff is a Rust binary, sub-10ms typically,
so a pre-check costs nothing next to a sandbox round.

Fail-open by design, same convention as workspace_memory.py/provider_stats.py:
ruff is NOT a hard dependency (see pyproject.toml's `quality` extra) — if the
binary isn't on PATH, or the subprocess call itself errors out, this returns
(True, "") rather than blocking the pipeline. An optional quality gate must
never become a reason the agent can't run at all.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

_RUFF_TIMEOUT = 10
_MAX_ERRORS_SHOWN = 15
# Само това, което чупи при пускане: синтаксис и недефинирани имена. Без --select ruff 0.16
# вади стотици стилови правила (bench contact-form 2026-10-04 #2: DTZ003/I001/UP035/F401 →
# 14 от 25 рунда козметика и счупен `datetime.timezone`); --isolated — чужд pyproject не важи.
_RULES = ("--isolated", "--select", "E9,F63,F7,F82")


def _ruff_exe() -> str | None:
    """`ruff` от PATH, иначе инсталираният до този Python (Scripts/ или bin/ на venv-а).

    bench cli-config 2026-10-02: Genesis върви като python.exe на pipx venv-а без
    activate — папката с ruff.exe не е в PATH, всеки WRITE_FILE пропускаше ruff и
    недефинирано `eprint` (F821) излезе с ✓; скритият тест получи NameError вместо код 2.
    """
    found = shutil.which("ruff")
    if found:
        return found
    for name in ("ruff.exe", "ruff"):
        candidate = Path(sys.executable).with_name(name)
        if candidate.is_file():
            return str(candidate)
    return None


def _ruff_available() -> bool:
    return _ruff_exe() is not None


def validate_code_with_ruff(code: str, *, fix: bool = True) -> tuple[bool, str]:
    """Lint `code` with ruff. Returns (ok, detail):
    - ruff missing / subprocess hiccup → (True, "") — never block on tooling.
    - clean (after auto-fix)          → (True, possibly-auto-fixed code).
    - unfixable issues remain         → (False, formatted error list) — the
      caller should feed this back to the LLM as the next round's context
      instead of spending a sandbox execution on code that can't even parse.
    `fix=False` — само проверка: върнатият код е винаги `code` (WRITE_FILE с exact).
    """
    if not _ruff_available():
        return True, ""
    # Самотен сурогат (половин емоджи от JSON): с "replace" ruff получаваше „?“
    # и WRITE_FILE записваше поправения код с „?“ вместо отказа „половин
    # емоджи?“ (одит 2026-10-22, мерено). Такъв код не е наша работа тук —
    # записът след това го отказва с българското съобщение.
    try:
        code.encode("utf-8")
    except UnicodeEncodeError:
        return True, ""

    try:
        # Байтове, не text=True (одит 2026-10-22): текстовият режим чете изхода
        # с universal newlines и всеки `\r\n` ставаше `\n` — WRITE_FILE на .py
        # (и с exact=true) записваше LF вместо дадения CRLF, а същото в .txt
        # оставаше CRLF (мерено с ruff 0.16.8). ruff пази краищата, които получи.
        fixed = ""
        if fix:
            fixed_run = subprocess.run(
                [_ruff_exe() or "ruff", "check", *_RULES, "--fix", "--exit-zero",
                 "--stdin-filename", "generated.py", "-"],
                input=code.encode("utf-8", "replace"), capture_output=True,
                timeout=_RUFF_TIMEOUT, check=False,
            )
            fixed = fixed_run.stdout.decode("utf-8", "replace")
        checked_run = subprocess.run(
            [_ruff_exe() or "ruff", "check", *_RULES, "--output-format", "json",
             "--stdin-filename", "generated.py", "-"],
            input=(fixed or code).encode("utf-8", "replace"), capture_output=True,
            timeout=_RUFF_TIMEOUT, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return True, ""

    if checked_run.returncode == 0:
        return True, fixed or code

    checked_out = checked_run.stdout.decode("utf-8", "replace")
    try:
        errors = json.loads(checked_out or "[]")
    except json.JSONDecodeError:
        # ruff's own crash/parse-error output isn't JSON — surface it as-is,
        # truncated, rather than silently passing broken code through.
        return False, (checked_out or checked_run.stderr.decode("utf-8", "replace"))[:1000]
    if not errors:
        # Срив на ruff (паника, exit 2, нищо на stdout) — fail-open като при
        # timeout (одит 2026-10-22): иначе всеки .py получаваше празното
        # „Ruff намери проблеми:“, а autonomous_loop връщаше верен код за поправка
        # всеки рунд. Поправеният изход от такъв ruff също не се ползва.
        return True, ""

    lines = [
        f"  L{e['location']['row']}: {e['code']} {e['message']}"
        for e in errors[:_MAX_ERRORS_SHOWN]
    ]
    if len(errors) > _MAX_ERRORS_SHOWN:
        lines.append(f"  … и още {len(errors) - _MAX_ERRORS_SHOWN}")
    return False, "Ruff намери проблеми:\n" + "\n".join(lines)


def ruff_parses(code: str) -> bool | None:
    """Парсва ли се `code` от ruff — неговият парсер приема всяка версия на Python.

    None — няма ruff или не отговори. Само синтактичните находки броят
    (`invalid-syntax`; E999 в по-стари ruff), не F821 и останалите.
    """
    exe = _ruff_exe()
    if exe is None:
        return None
    try:
        run = subprocess.run(
            [exe, "check", "--isolated", "--select", "E9", "--output-format", "json",
             "--stdin-filename", "generated.py", "-"],
            input=code.encode("utf-8"), capture_output=True, timeout=_RUFF_TIMEOUT, check=False,
        )
        if run.returncode not in (0, 1):
            return None
        found = json.loads(run.stdout.decode("utf-8", "replace") or "[]")
    except (subprocess.TimeoutExpired, OSError, UnicodeEncodeError, ValueError):
        return None
    if not isinstance(found, list):
        return None
    return not any(e.get("code") in (None, "E999", "invalid-syntax")
                   for e in found if isinstance(e, dict))


def lint_note(code: str) -> str:
    """CHECK-ONLY ruff pass — no --fix, никога не връща променено съдържание.

    За EDIT_FILE (genesis_skills._tool_edit_file), не за WRITE_FILE. Причината
    да не преизползваме validate_code_with_ruff директно: тя auto-fix-ва и
    връща ЦЕЛИЯ пренаписан файл — правилно за WRITE_FILE (моделът и без това
    подава цялото съдържание), но за EDIT_FILE би означавало ruff тихо да
    пренапише части от файла, които моделът НЕ е поискал да пипа — точно
    обратното на обещанието на code_edit.py ("changes only what you name").
    Затова тук само отчитаме находки, без да пипаме нищо на диска.

    Празен низ = ruff не е наличен, или файлът е чист.
    """
    if not _ruff_available():
        return ""
    try:
        checked = subprocess.run(
            [_ruff_exe() or "ruff", "check", *_RULES, "--output-format", "json",
             "--stdin-filename", "generated.py", "-"],
            input=code, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_RUFF_TIMEOUT, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if checked.returncode == 0:
        return ""
    try:
        errors = json.loads(checked.stdout or "[]")
    except json.JSONDecodeError:
        return ""
    if not errors:
        return ""
    lines = [
        f"  L{e['location']['row']}: {e['code']} {e['message']}"
        for e in errors[:_MAX_ERRORS_SHOWN]
    ]
    if len(errors) > _MAX_ERRORS_SHOWN:
        lines.append(f"  … и още {len(errors) - _MAX_ERRORS_SHOWN}")
    return "\nRuff (само отчет, файлът не е пипнат отвъд редакцията):\n" + "\n".join(lines)


# ─── JSON / YAML / TOML при запис (2026-11-05) ────────────────────────────────
# Проба: `package.json` със запетая накрая, `docker-compose.yml` с изместен
# отстъп и `pyproject.toml` с `name = x` минаваха през WRITE_FILE с „✓“ — 3 от 3;
# грешката излизаше чак при `npm install`, `docker compose up` или `pip install .`,
# рундове по-късно и с чуждо съобщение. Като ruff за .py: записът не се спира
# (моделът поиска точно това), но резултатът казва реда, за да го поправи веднага.
_DATA_MAX_BYTES = 1_000_000
# JSON с коментари по стандарт на инструмента (tsconfig, VS Code, devcontainer):
# json.loads там е грешна мярка.
_JSONC_NAMES = re.compile(r"^(?:tsconfig|jsconfig)(?:\.[\w-]+)?\.json$|^\.?(?:eslintrc|babelrc|"
                          r"swcrc|devcontainer)\.json$", re.IGNORECASE)
_JSONC_DIRS = {".vscode", ".devcontainer", ".idea"}


def _toml_loads():
    try:
        import tomllib
        return tomllib.loads
    except ImportError:                     # 3.10: tomli, ако го има (pytest го носи)
        try:
            import tomli
            return tomli.loads
        except ImportError:
            return None


def data_error(path: Path, content: str) -> str:
    """Защо `content` не е валиден JSON/YAML/TOML за `path`; "" ако е или не се знае."""
    suffix = path.suffix.lower()
    if not content.strip() or len(content) > _DATA_MAX_BYTES:
        return ""
    if suffix == ".json":
        if _JSONC_NAMES.match(path.name) or _JSONC_DIRS & set(path.parts):
            return ""
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            return f"не е валиден JSON (ред {e.lineno}, колона {e.colno}: {e.msg})"
        except RecursionError:
            return "не е валиден JSON (твърде дълбоко вложен)"
        return ""
    if suffix in (".yaml", ".yml"):
        # Шаблони (Helm, Jinja, Go): `{{ … }}` не е YAML, докато не се изпълни.
        if "{{" in content or "{%" in content:
            return ""
        import yaml
        try:
            # compose, не load: `!Ref`/`!Sub` (CloudFormation) и други тагове са валидни.
            for _ in yaml.compose_all(content, Loader=yaml.SafeLoader):
                pass
        except yaml.MarkedYAMLError as e:
            mark = e.problem_mark or e.context_mark
            where = f"ред {mark.line + 1}, колона {mark.column + 1}: " if mark else ""
            return f"не е валиден YAML ({where}{e.problem or e.context})"
        except yaml.YAMLError as e:
            return f"не е валиден YAML ({e})"
        return ""
    if suffix == ".toml":
        loads = _toml_loads()
        if loads is None:
            return ""
        try:
            loads(content)
        except ValueError as e:              # TOMLDecodeError е ValueError
            return f"не е валиден TOML ({e})"
    return ""


def data_note(path: Path, content: str) -> str:
    """Бележката към резултата на WRITE_FILE/EDIT_FILE, или ""."""
    why = data_error(path, content)
    return (f"\n⚠ Записаният файл {why}. Поправи го, преди да го ползваш — инструментът, "
            "който го чете, ще падне със същото.") if why else ""
