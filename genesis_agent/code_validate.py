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
import shutil
import subprocess

_RUFF_TIMEOUT = 10
_MAX_ERRORS_SHOWN = 15


def _ruff_available() -> bool:
    return shutil.which("ruff") is not None


def validate_code_with_ruff(code: str) -> tuple[bool, str]:
    """Lint `code` with ruff. Returns (ok, detail):
    - ruff missing / subprocess hiccup → (True, "") — never block on tooling.
    - clean (after auto-fix)          → (True, possibly-auto-fixed code).
    - unfixable issues remain         → (False, formatted error list) — the
      caller should feed this back to the LLM as the next round's context
      instead of spending a sandbox execution on code that can't even parse.
    """
    if not _ruff_available():
        return True, ""

    try:
        fixed = subprocess.run(
            ["ruff", "check", "--fix", "--exit-zero",
             "--stdin-filename", "generated.py", "-"],
            input=code, capture_output=True, text=True, timeout=_RUFF_TIMEOUT,
            check=False,
        )
        checked = subprocess.run(
            ["ruff", "check", "--output-format", "json",
             "--stdin-filename", "generated.py", "-"],
            input=fixed.stdout or code, capture_output=True, text=True,
            timeout=_RUFF_TIMEOUT, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return True, ""

    if checked.returncode == 0:
        return True, fixed.stdout or code

    try:
        errors = json.loads(checked.stdout or "[]")
    except json.JSONDecodeError:
        # ruff's own crash/parse-error output isn't JSON — surface it as-is,
        # truncated, rather than silently passing broken code through.
        return False, (checked.stdout or checked.stderr)[:1000]

    lines = [
        f"  L{e['location']['row']}: {e['code']} {e['message']}"
        for e in errors[:_MAX_ERRORS_SHOWN]
    ]
    if len(errors) > _MAX_ERRORS_SHOWN:
        lines.append(f"  … и още {len(errors) - _MAX_ERRORS_SHOWN}")
    return False, "Ruff намери проблеми:\n" + "\n".join(lines)


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
            ["ruff", "check", "--output-format", "json",
             "--stdin-filename", "generated.py", "-"],
            input=code, capture_output=True, text=True,
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
