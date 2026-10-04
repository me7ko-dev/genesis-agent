"""Приемни тестове само от заявката (NEXT_STEPS Б.4) — зад GENESIS_ACCEPTANCE=1.

Тестовете на модела идват от кода му — затова бяха зелени и при грешен ЕГН,
ЕИК и евро (bench/projects, 2026-09-25). Тук отделно обръщение вижда САМО
заявката, пише pytest тестове и те се пускат срещу написаното. Провалите
стигат до модела веднъж, с правилото на code_check: първо очакваното срещу
условието. Струва едно обръщение на задача — по подразбиране става само ако
bench_projects покаже повече верни; иначе се маха.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("genesis.acceptance")

_WRITTEN = re.compile(r"^\[(?:WRITE_FILE|EDIT_FILE): ([^\]\n]+\.py)\] ✓", re.MULTILINE)
_CODE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)
_PROMPT = ("Пишеш приемни тестове с pytest САМО по заявката долу — кодът не ти е показан. "
           "Импортирай точно имената на модул и функции, които заявката дава. Тествай всяко "
           "правило от заявката с конкретни стойности, които следват от нея, без догадки извън "
           "нея. Ако заявката не дава имена на модул и функция — отговори само NONE. Иначе "
           "отговори само с един ```python блок.")


def enabled() -> bool:
    return os.environ.get("GENESIS_ACCEPTANCE") == "1"


def project_root(py_file: Path) -> Path:
    return py_file.parent.parent if py_file.parent.name == "tests" else py_file.parent


def extract_tests(reply: str) -> str:
    m = _CODE.search(reply or "")
    code = m.group(1) if m else ""
    return code if "def test_" in code else ""


def _counts(output: str) -> tuple[int, int]:
    n = {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|error)", output)}
    passed = n.get("passed", 0)
    return passed, passed + n.get("failed", 0) + n.get("error", 0)


def _brain_complete(messages: list[dict]) -> str:
    from genesis_agent.brain import Brain
    return Brain().complete(messages).raw_text or ""


class AcceptanceCheck:
    """Като code_check.RunCheck: `observe` за всеки резултат, `check()` когато
    моделът спре да вика инструменти; непразна бележка отива при модела."""

    def __init__(self, request: str, complete: Callable[[list[dict]], str] | None = None) -> None:
        self._request = request or ""
        self._complete = complete or _brain_complete
        self._root: Path | None = None
        self._done = False

    def observe(self, result: str) -> None:
        for p in _WRITTEN.findall(result or ""):
            if self._root is None:
                self._root = project_root(Path(p.strip()))

    def due(self) -> bool:
        return enabled() and self._root is not None and not self._done

    def check(self) -> str:
        if not self.due() or self._root is None:
            return ""
        self._done = True
        try:
            reply = self._complete([{"role": "system", "content": _PROMPT},
                                    {"role": "user", "content": self._request[:6000]}])
        except Exception:  # обръщението е допълнително — провалът му не спира хода
            log.debug("приемните тестове не се написаха — ходът продължава без тях", exc_info=True)
            return ""
        code = "" if reply.startswith("Error:") else extract_tests(reply)
        if not code:
            return ""
        from genesis_agent import sandbox
        from genesis_agent.paths import project_python
        with tempfile.TemporaryDirectory(prefix="genesis_acceptance_") as tmp:
            (Path(tmp) / "test_acceptance.py").write_text(code, encoding="utf-8")
            # Като RUN_CMD на модела: същата среда (без ключовете на Genesis),
            # същите лимити за CPU/памет/процеси, убиване на цялото дърво при таймаут.
            run = sandbox._run([project_python(self._root), "-m", "pytest", "-q", "-p",
                                "no:cacheprovider", "--rootdir", tmp, tmp], cwd=Path(tmp),
                               policy=sandbox.get_policy(), timeout=120,
                               env_extra={"PYTHONPATH": str(self._root)})
        out = (run.stdout or "") + (run.stderr or "")
        passed, total = _counts(out)
        if total == 0 or passed == total:
            return ""
        fails = "\n".join(ln for ln in out.splitlines() if ln.startswith(("FAILED", "ERROR")))[:1500]
        return (f"[приемни тестове] Независими тестове, писани САМО по заявката (без да виждат "
                f"кода): {passed}/{total} минават.\n{fails}\nЗа всеки провал първо провери "
                "очакваното срещу условието: тестът греши спрямо условието → кажи кой и защо, "
                "кодът не се пипа; кодът греши → поправи го и пусни своите тестове пак.")
