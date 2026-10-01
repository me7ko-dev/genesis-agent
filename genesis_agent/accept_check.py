"""Acceptance tests written from the request alone, run against the code (plan Б.4).

bench_projects 2026-10-01, six harder projects: the model's own tests were
green and the hidden ones were not, because its tests follow its code.
expense-bot checked the category with [A-Za-z0-9_] and tested it with English
words, so "храна" was refused. shop-scraper never saw "1 299,00 лв." with a
no-break space. sklad accepted 2026-02-30 although the request says invalid
values raise ValueError. So once per turn, when the model says it is done, a
separate call that never sees the code writes tests from the request and the
signatures. They run once, outside the project, and only the failures go back
to the model, with the request as the judge, not the test. (c333, 2026-09-30:
the model "fixed" correct code to match a wrong assert of its own.)
GENESIS_ACCEPT_CHECK=0 turns it off.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from genesis_agent.page_check import _clean_env
from genesis_agent.paths import project_python

_WRITTEN = re.compile(r"^\[(?:WRITE_FILE|EDIT_FILE): ([^\]\n]+)\] ✓", re.MULTILINE)
_SIGNATURE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+\w+")
_CODE_BLOCK = re.compile(r"```(?:python|py)?[ \t]*\n(.*?)```", re.DOTALL)
_COUNT = re.compile(r"(\d+) (passed|failed|errors?)\b")
# Кратка заявка („скрипт, който печата часа“) не носи правила за проверка.
_MIN_TASK = 200
_MAX_SIGNATURES = 80
_TIMEOUT = 120
_MAX_OUT = 3500

_TESTER = """Ти си независим проверяващ. Пишеш pytest приемни тестове САМО по заявката на клиента.
Кода не виждаш: имаш само файловете и сигнатурите им.
- Проверявай всяко правило, ограничение и формат, които заявката казва изрично: граничните
  стойности, входа, който трябва да бъде отхвърлен, запазването след рестарт, кодовете за изход.
- Данните да са реалистични за български клиент: кирилица в текстовите полета, ако заявката
  не казва друго; суми и цени във вида, в който заявката ги показва.
- НЕ проверявай неща, които заявката не казва: точния текст на съобщения, имена извън заявката
  и сигнатурите, вътрешни детайли.
- Без мрежа. Файловете — в tmp_path. Модулите се внасят по името на файла.
- Командният ред — през subprocess с sys.executable и cwd=PROJECT, където в началото на файла
  стои PROJECT = Path.cwd() (тестовете тръгват от папката на проекта).
- 6–15 теста, кратки. Върни САМО един ```python блок, без обяснения."""


def enabled() -> bool:
    return os.environ.get("GENESIS_ACCEPT_CHECK", "1") != "0"


def _project_root(files: list[Path]) -> Path:
    """Общата папка на записаните файлове; над пакет (`sklad/__init__.py`) — една нагоре."""
    root = Path(os.path.commonpath([str(p.parent) for p in files]))
    while (root / "__init__.py").is_file() and root.parent != root:
        root = root.parent
    return root


def signatures(files: list[Path], root: Path) -> str:
    out: list[str] = []
    for p in files:
        if p.name.startswith("test_") or p.name == "conftest.py":
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        try:
            name = p.relative_to(root).as_posix()
        except ValueError:
            name = p.name
        out.append(f"# {name}")
        out += [ln.rstrip() for ln in lines if _SIGNATURE.match(ln)]
    if len(out) > _MAX_SIGNATURES:
        out = out[:_MAX_SIGNATURES] + ["# … (отрязано)"]
    return "\n".join(out)


def _counts(output: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for n, kind in _COUNT.findall(output):
        found[kind.rstrip("s") if kind.startswith("error") else kind] = int(n)
    return found


def _failures(output: str) -> str:
    """Само провалите: от раздела FAILURES до обобщението, отрязано."""
    start = output.find("= FAILURES =")
    text = output[output.rfind("\n", 0, start) + 1:] if start != -1 else output
    if len(text) > _MAX_OUT:
        text = text[:_MAX_OUT] + "\n… [отрязано]"
    return text.strip()


class AcceptCheck:
    """The chat loop calls `observe` for every tool result and `check(ask)`
    when the model stops calling tools; a non-empty note goes back to it."""

    def __init__(self, task: str) -> None:
        self._task = task.strip()
        self._written: dict[str, Path] = {}
        self._done = False

    def observe(self, result: str) -> None:
        for p in _WRITTEN.findall(result or ""):
            path = Path(p.strip())
            if path.suffix.lower() == ".py":
                self._written[str(path)] = path

    def due(self) -> bool:
        code = [p for p in self._written.values() if not p.name.startswith("test_")]
        return (enabled() and not self._done and bool(code)
                and len(self._task) >= _MIN_TASK)

    def check(self, ask: Callable[[list[dict]], str]) -> tuple[str, str]:
        """(бележка към модела, ред за екрана). Празна бележка = нищо за поправка."""
        self._done = True
        files = [p for p in self._written.values() if p.is_file()]
        if not files:
            return "", ""
        root = _project_root(files)
        prompt = (f"ЗАЯВКАТА:\n{self._task}\n\nФАЙЛОВЕТЕ И СИГНАТУРИТЕ:\n"
                  f"{signatures(files, root)}")
        try:
            reply = ask([{"role": "system", "content": _TESTER},
                         {"role": "user", "content": prompt}]) or ""
        except Exception as e:  # проверката е допълнителна — не бива да спира хода
            return "", f"пропусната: {type(e).__name__}"
        blocks = _CODE_BLOCK.findall(reply)
        code = max(blocks, key=len) if blocks else ""
        if "def test" not in code:
            return "", "пропусната: проверяващият не върна тестове"
        output, rc = self._run(code, root)
        found = _counts(output)
        failed = found.get("failed", 0)
        total = failed + found.get("passed", 0)
        if rc == 0 and total:
            return "", f"✓ {total}/{total} теста само по заявката"
        if rc != 1 or not failed:
            # Тестовете не се събраха (грешка в тях, липсва pytest, таймаут) —
            # шум, не находка за кода.
            return "", "пропусната: тестовете на проверяващия не тръгнаха"
        note = (f"[приемна проверка] Отделен проверяващ написа {total} теста САМО по заявката, "
                f"без да вижда кода ти. Паднаха {failed}:\n{_failures(output)}\n\n"
                "За всеки провал първо сравни очакването с ТЕКСТА НА ЗАЯВКАТА. Кодът не прави "
                "каквото заявката казва → поправи кода, добави този случай в своите тестове и ги "
                "пусни. Проверяващият е тълкувал заявката грешно или е предположил нещо, което тя "
                "не казва → НЕ пипай кода заради него, кажи в един ред защо. Тестовете му не са "
                "в проекта — не ги копирай там.")
        return note, f"{failed} от {total} теста само по заявката паднаха"

    @staticmethod
    def _run(code: str, root: Path) -> tuple[str, int]:
        with tempfile.TemporaryDirectory(prefix="genesis-accept-") as tmp:
            test = Path(tmp) / "test_acceptance.py"
            test.write_text(code, encoding="utf-8")
            env = _clean_env()
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(root), env.get("PYTHONPATH")]))
            env["PYTHONUTF8"] = "1"
            cmd = [project_python(root), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                   "--tb=short", "-rf", "--rootdir", tmp, str(test)]
            try:
                r = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=_TIMEOUT,
                                   check=False)
            except (OSError, subprocess.SubprocessError) as e:
                return str(e), -1
            return (r.stdout or "") + (r.stderr or ""), r.returncode
