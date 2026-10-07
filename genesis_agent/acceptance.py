"""
genesis_agent.acceptance — tests written from the request alone, never from
the code (NEXT_STEPS Б.4). Opt-in: GENESIS_ACCEPTANCE=1.

Why: the model writes its own tests from its own code, so they agree with it —
green with a wrong EGN rule, a wrong EIK weight, a wrong euro date (bench,
2026-09-25). Here a separate call sees only the operator's request (plus the
verified domain rules the chat already attached to it) and the NAMES of the
files written, never their content, and writes pytest acceptance tests. They
run against the project from a temporary folder, outside the workspace, so the
project is not touched. A failure goes back to the model once, with the rule
for settling it: the REQUEST decides — fix the code if the request says what
the test says, otherwise leave the code and say the test assumed too much.

Off by default because it is unmeasured: the plan is to run bench_projects
with and without it and keep it only if it catches more than it costs
(`GENESIS_ACCEPTANCE=1 scripts/bench_projects.py --runs 2 --compare ...`).
"""
from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

_WRITTEN = re.compile(r"^\[(?:WRITE_FILE|EDIT_FILE): ([^\]\n]+)\] ✓", re.MULTILINE)
_TIMEOUT = 120
_MAX_OUTPUT = 2500

_PROMPT = """You write ACCEPTANCE TESTS for a request that another developer has just
implemented. You do NOT see their code, on purpose: the tests must come from the
request, so they can disagree with the code.

Rules:
- Test only what the request states: its rules, its examples, the formats and
  edge cases it names. Nothing it does not say.
- Import exactly the module / function / class names the request gives. The
  developer wrote these Python files (names only): {files}
- If the request names no importable interface (no module or function names),
  reply with exactly: SKIP
- Use only expected values you are certain of from the request{rules_note}. If a
  value would need knowledge you are unsure of, leave that case out.
- 3 to 8 small pytest test functions with plain asserts. No network, no files
  outside pytest's tmp_path, no fixtures of your own beyond tmp_path.

Return ONE ```python block and nothing else.

REQUEST:
{request}
{rules}"""


def enabled() -> bool:
    return os.environ.get("GENESIS_ACCEPTANCE", "").strip().lower() in ("1", "true", "yes", "on")


def _is_test(path: Path) -> bool:
    return (path.name.startswith("test_") or path.name.endswith("_test.py")
            or path.name == "conftest.py" or "tests" in path.parts)


class AcceptanceCheck:
    """Like code_check.RunCheck: `observe` every tool result, `due()` when
    the model stops calling tools, `check()` → (note for the model, one line
    for the operator). At most once per turn."""

    def __init__(self, request: str, workspace: Path, *, rules: str = "",
                 complete: Callable[[list[dict]], object] | None = None,
                 on: bool | None = None) -> None:
        self.request = request
        self.rules = rules
        self.workspace = Path(workspace)
        self._complete = complete
        self._on = enabled() if on is None else on
        self._written: dict[str, Path] = {}
        self._done = False
        self.test_file: Path | None = None
        self.passed: bool | None = None

    def observe(self, result: str) -> None:
        for p in _WRITTEN.findall(result or ""):
            path = Path(p.strip())
            if path.suffix.lower() == ".py" and not _is_test(path):
                self._written[str(path)] = path

    def due(self) -> bool:
        return self._on and bool(self._written) and not self._done

    def _files(self) -> str:
        names = []
        for path in self._written.values():
            try:
                names.append(path.resolve().relative_to(self.workspace.resolve()).as_posix())
            except (ValueError, OSError):
                names.append(path.name)
        return ", ".join(sorted(names))

    def _ask(self) -> str:
        prompt = _PROMPT.format(
            files=self._files(), request=self.request.strip(),
            rules_note=" or the verified rules below" if self.rules else "",
            rules=f"\nVERIFIED DOMAIN RULES (trust these over your memory):\n{self.rules.strip()}\n"
                  if self.rules else "")
        complete = self._complete
        if complete is None:
            from genesis_agent.brain import Brain
            complete = Brain().complete
        reply = complete([{"role": "user", "content": prompt}])
        raw = getattr(reply, "raw_text", "") or ""
        if raw.startswith("Error:") or raw.strip().upper().startswith("SKIP"):
            return ""
        code = getattr(reply, "code", "") or ""
        if not code:
            m = re.search(r"```(?:python|py)?\s*\n(.*?)```", raw, re.DOTALL)
            code = m.group(1) if m else ""
        return code if "def test" in code else ""

    def check(self) -> tuple[str, str]:
        self._done = True
        try:
            code = self._ask()
        except Exception as e:  # a failed side check never stops the turn
            return "", f"приемни тестове: пропуснати ({e})"
        if not code:
            return "", "приемни тестове: заявката не дава интерфейс за тест — пропуснати"
        folder = Path(tempfile.mkdtemp(prefix="genesis-acceptance-"))
        self.test_file = folder / "test_acceptance.py"
        self.test_file.write_text(code, encoding="utf-8")
        ws = self.workspace.resolve()
        (folder / "conftest.py").write_text(
            "import sys\n"
            f"sys.path[:0] = [{str(ws)!r}, {str(ws / 'src')!r}]\n", encoding="utf-8")
        from genesis_agent import sandbox
        from genesis_agent.paths import project_python
        py = project_python(ws)
        py = f'"{py}"' if " " in py else py
        cmd = (f'{py} -m pytest -q -p no:cacheprovider --rootdir "{folder}" '
               f'"{self.test_file}"')
        res = sandbox.run_shell(cmd, cwd=ws, timeout=_TIMEOUT)
        if res.blocked or res.returncode is None:
            return "", "приемни тестове: не можаха да се пуснат — пропуснати"
        out = ((res.stdout or "") + "\n" + (res.stderr or "")).strip()
        summary = next((ln.strip(" =") for ln in reversed(out.splitlines())
                        if re.search(r"\d+ (passed|failed|error)", ln)), "")
        if res.returncode == 5:  # no tests collected
            return "", "приемни тестове: нито един не се събра — пропуснати"
        if res.returncode == 0:
            self.passed = True
            return "", f"приемни тестове от заявката: ✓ {summary}"
        self.passed = False
        if len(out) > _MAX_OUTPUT:
            out = out[:_MAX_OUTPUT // 3] + "\n… [отрязано] …\n" + out[-2 * _MAX_OUTPUT // 3:]
        note = (
            "[приемни тестове] Отделно обръщение написа тестове САМО от заявката, без да вижда "
            f"кода ти ({self.test_file}). Падат:\n{out}\n\n"
            "За всеки паднал тест сравни очакваното с УСЛОВИЕТО на заявката:\n"
            "- заявката казва същото като теста → кодът е грешен: поправи го и пусни своите тестове;\n"
            "- тестът допуска нещо, което заявката не казва (или е сгрешил стойност) → НЕ пипай "
            "кода, кажи го в един ред;\n"
            "- различно име на функция/модул от това в заявката → ползвай името от заявката.\n"
            "Не копирай тези тестове в проекта.")
        return note, f"приемни тестове от заявката: ✗ {summary or 'падат'}"
