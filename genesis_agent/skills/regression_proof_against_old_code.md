---
name: regression_proof_against_old_code
category: autonomous
description: Run a new test against the pre-fix code from git to prove it actually
  catches the bug, restoring the working copy unconditionally.
triggers:
- regression proof against old code
- Run a new test against the pre-fix code from git to prove it actually catches the
  bug, restoring the working copy uncond
- докажи че тестът хваща бъга
- пусни теста срещу стария код
version: '1.0'
author: Genesis
last_updated: '2026-09-20T14:57:28.860262+00:00'
---

## Описание
Run a new test against the pre-fix code from git to prove it actually catches the bug, restoring the working copy unconditionally.

## Python Код
```python
"""Доказва, че новият тест наистина хваща бъга — като го пуска срещу СТАРИЯ код.

Защо съществува: тест, написан след поправката, почти винаги минава. Това не
значи нищо. Единственото, което доказва, че тестът пази нещо, е да падне срещу
версията ОТПРЕДИ поправката. Без тази стъпка „добавих тест" е твърдение, не
факт — и точно този клас самоизмама пълни пакетите с тестове, които не биха
хванали регресията, заради която уж са написани.

Механиката е проста нарочно: взима версията на изходните файлове от git
(`git show <ref>:<път>`), слага я на мястото ѝ, пуска ПОСОЧЕНИТЕ тестове,
после връща работната версия — безусловно, дори при прекъсване. Възстановяване
чрез `git checkout` НЕ става: работното копие съдържа незакоммитната поправка,
която тъкмо се проверява, и `checkout` би я изтрил. Затова съдържанието се
пази в паметта и се връща байт за байт, а връщането се проверява с хеш.

Ограничение, казано направо: това мери ЕДИН файл назад във времето. Ако
поправката ти е в три файла и върнеш само единия, тестът може да падне по
причина, различна от бъга. Затова `source_paths` е списък и се връщат всички
наведнъж.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

_TIMEOUT = 300


@dataclass
class Proof:
    """Какво доказа пускането срещу стария код."""
    proving: list[str] = field(default_factory=list)      # паднаха → пазят нещо
    not_proving: list[str] = field(default_factory=list)  # минаха → не доказват нищо
    error: str = ""

    @property
    def ok(self) -> bool:
        """True само ако ВСЕКИ пуснат тест пада срещу стария код."""
        return bool(self.proving) and not self.not_proving and not self.error

    def summary(self) -> str:
        if self.error:
            return f"❌ не може да се провери: {self.error}"
        lines = []
        if self.proving:
            lines.append(f"✅ доказват бъга ({len(self.proving)}): "
                         + ", ".join(self.proving[:5])
                         + (" …" if len(self.proving) > 5 else ""))
        if self.not_proving:
            lines.append(f"⚠️  минават и срещу СТАРИЯ код ({len(self.not_proving)}): "
                         + ", ".join(self.not_proving[:5])
                         + (" …" if len(self.not_proving) > 5 else ""))
            lines.append("   Такъв тест закова поведение, но не доказва поправката.")
        if not lines:
            lines.append("⚠️  нито един тест не се пусна — провери пътя")
        return "\n".join(lines)


def _git_show(ref: str, path: str, cwd: Path) -> str | None:
    """Съдържанието на файла при `ref`, или None ако го няма там."""
    try:
        out = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=str(cwd),
                             capture_output=True, text=True, timeout=30,
                             check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def parse_failures(pytest_output: str) -> tuple[list[str], list[str]]:
    """(паднали, минали) имена на тестове от изхода на pytest.

    Чете редовете `FAILED …` / `PASSED …`, които `-v` дава. Отделно е
    функция, за да може да се тества без да се пуска pytest.
    """
    failed, passed = [], []
    for raw in pytest_output.splitlines():
        line = raw.strip()
        if line.startswith(("FAILED ", "ERROR ")):
            failed.append(_test_id(line.split(" ", 1)[1]))
        elif " PASSED" in line and "::" in line:
            passed.append(_test_id(line.split(" PASSED")[0]))
    return [f for f in failed if f], [p for p in passed if p]


def _test_id(chunk: str) -> str:
    """Името на теста от `път::Клас::test_x[параметър с интервали] - причина`.

    Наивното `split()[1]` реже точно параметризираните имена на първия
    интервал ВЪТРЕ в скобите — а те са най-честите в този проект.
    """
    chunk = chunk.strip()
    for sep in (" - ", "\t"):
        if sep in chunk:
            chunk = chunk.split(sep)[0]
    name = chunk.split("::")[-1].strip()
    # pytest пише не-ASCII параметрите като `\uXXXX`. Операторът чете на
    # български, а `test_it_runs[\u0443\u0431\u0438\u0439 ...]` не се чете от никого.
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), name)


def _restore(backups: dict[Path, str]) -> list[str]:
    """Връща запазеното съдържание. Връща списък с пътища, които НЕ успяха."""
    broken = []
    for path, content in backups.items():
        try:
            path.write_text(content, encoding="utf-8")
            if hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest() != \
               hashlib.sha256(content.encode()).hexdigest():
                broken.append(str(path))
        except OSError:
            broken.append(str(path))
    return broken


def prove(test_path: str, source_paths: list[str], *, ref: str = "HEAD",
          root: str | Path = ".") -> Proof:
    """Пуска `test_path` срещу версията на `source_paths` при `ref`.

    Работното съдържание се връща ВИНАГИ — и при провал, и при прекъсване.
    Ако връщането не успее, това се казва в `error`: по-добре шумен провал,
    отколкото тихо оставена стара версия на диска.
    """
    root = Path(root).resolve()
    proof = Proof()
    backups: dict[Path, str] = {}

    for rel in source_paths:
        path = (root / rel).resolve()
        if not path.is_file():
            proof.error = f"няма такъв файл: {rel}"
            return proof
        old = _git_show(ref, rel, root)
        if old is None:
            proof.error = f"{rel} не съществува при {ref} — няма с какво да се сравни"
            return proof
        backups[path] = path.read_text(encoding="utf-8")

    try:
        for rel in source_paths:
            path = (root / rel).resolve()
            old = _git_show(ref, rel, root)
            if old is not None:
                path.write_text(old, encoding="utf-8")
        try:
            run = subprocess.run([sys.executable, "-m", "pytest", test_path, "-v",
                                  "--no-header", "-p", "no:cacheprovider"],
                                 cwd=str(root), capture_output=True, text=True,
                                 timeout=_TIMEOUT, check=False)
            proof.proving, proof.not_proving = parse_failures(run.stdout + run.stderr)
        except subprocess.SubprocessError as e:
            proof.error = f"pytest не се пусна: {e}"
    finally:
        broken = _restore(backups)
        if broken:
            proof.error = ("РАБОТНАТА ВЕРСИЯ НЕ Е ВЪРНАТА за: " + ", ".join(broken)
                           + " — възстанови ръчно преди да продължиш")
    return proof


if __name__ == "__main__":
    # Самопроверка без git и без pytest: чистите функции се проверяват с
    # подаден изход, а връщането на файл — с истински файл в tmp.
    import tempfile

    out = (
        "tests/test_x.py::TestA::test_catches_it FAILED\n"
        "FAILED tests/test_x.py::TestA::test_catches_it - AssertionError\n"
        "tests/test_x.py::TestA::test_pins_old PASSED\n"
    )
    failed, passed = parse_failures(out)
    assert "test_catches_it" in failed, failed
    assert "test_pins_old" in passed, passed

    assert _test_id("tests/t.py::C::test_x[kill a stuck process] - AssertionError") \
        == "test_x[kill a stuck process]", "параметър с интервали не бива да се реже"
    assert _test_id(r"tests/t.py::C::test_x[\u0443\u0431\u0438\u0439]") == "test_x[убий]"

    p = Proof(proving=["test_catches_it"], not_proving=["test_pins_old"])
    assert p.ok is False, "тест, който минава и срещу стария код, не е доказателство"
    assert "не доказва" in p.summary()
    assert Proof(proving=["a"]).ok is True

    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "s.py"
        f.write_text("оригинал\n", encoding="utf-8")
        saved = {f: f.read_text(encoding="utf-8")}
        f.write_text("подменен\n", encoding="utf-8")
        assert _restore(saved) == []
        assert f.read_text(encoding="utf-8") == "оригинал\n", "връщането трябва да е байт за байт"

    print("OK")
```

## Pitfalls
- доказано срещу реален коммит: tests/test_dna.py срещу d119187~1 → 14 доказват, 15 не
