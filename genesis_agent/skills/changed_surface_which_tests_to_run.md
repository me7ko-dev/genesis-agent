---
name: 'changed_surface_which_tests_to_run'
category: 'autonomous'
description: 'List what changed (git diff plus untracked files) and which test files cover it, so the fix-and-verify cycle runs the affected tests instead of the whole suite — falling back to the full suite whenever coverage cannot be established.'
triggers: ["changed surface which tests to run"]
version: '1.0'
author: 'Genesis'
last_updated: '2026-09-19T23:02:53.150612+00:00'
---

## Описание
List what changed (git diff plus untracked files) and which test files cover it, so the fix-and-verify cycle runs the affected tests instead of the whole suite — falling back to the full suite whenever coverage cannot be established.

## Python Код
```python
"""Какво е променено и кои тестове го покриват — за да не се пуска цял suite за един файл.

Защо съществува: пълният suite на този проект върви ~25 секунди, на по-голям
проект — минути. Цикълът "поправи → провери" се случва десетки пъти в една
сесия, така че разликата между "пусни трите засегнати файла" и "пусни всичко"
е реална част от времето на оператора.

Съответствието модул→тест е по конвенция (`budget.py` → `test_budget.py`) плюс
търсене по споменаване в тестовете. Нарочно НЕ се прави покритие по
инструментиране: то иска изпълнение на целия suite, което е точно това, което
този модул съществува да избегне.

Честно за границите: конвенцията пропуска тестове, които засягат модула, без
да го споменават по име (през фикстура, през интеграционен път). Затова
`fallback_to_full_suite` е True винаги, когато нищо не е намерено, и
`summarize` го казва — по-добре загубени 25 секунди, отколкото поправка,
проверена с тестове, които не я докосват.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_GIT_TIMEOUT = 30


@dataclass
class Surface:
    changed: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    fallback_to_full_suite: bool = False
    note: str = ""


def changed_files(root: str | Path = ".", *, base: str = "") -> list[str]:
    """Променените файлове: некомитнати, или срещу `base` ако е подаден.

    Празен списък при липса на git/грешка — извикващият пада към целия suite,
    вместо да реши, че нищо не е променено.
    """
    if not shutil.which("git"):
        return []
    args = (["git", "diff", "--name-only", f"{base}...HEAD"] if base
            else ["git", "diff", "--name-only", "HEAD"])
    try:
        proc = subprocess.run(args, cwd=str(root), capture_output=True,
                              text=True, timeout=_GIT_TIMEOUT, check=False)
        names = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if not base:
            # Нов, още непроследен файл не се вижда в `git diff`, но е точно
            # това, което най-често трябва да се тества.
            extra = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                                   cwd=str(root), capture_output=True, text=True,
                                   timeout=_GIT_TIMEOUT, check=False)
            names += [ln.strip() for ln in (extra.stdout or "").splitlines() if ln.strip()]
    except (subprocess.TimeoutExpired, OSError):
        return []
    return sorted(set(names))


def tests_for(paths: list[str], root: str | Path = ".") -> list[str]:
    """Тестовите файлове, които засягат тези пътища.

    Два пътя: конвенцията по име и реално споменаване в текста на теста.
    Променен тестов файл се връща сам — него винаги трябва да се пусне.
    """
    root = Path(root)
    test_dirs = [d for d in (root / "tests", root) if d.is_dir()]
    all_tests: list[Path] = []
    for d in test_dirs:
        all_tests += sorted(d.glob("test_*.py"))
    all_tests = list(dict.fromkeys(all_tests))

    hits: set[str] = set()
    for raw in paths:
        p = Path(raw)
        if p.name.startswith("test_") and p.suffix == ".py":
            hits.add(raw)
            continue
        if p.suffix != ".py":
            continue
        stem = p.stem
        for t in all_tests:
            if t.stem == f"test_{stem}":
                hits.add(str(t.relative_to(root)) if t.is_relative_to(root) else str(t))
                continue
            try:
                text = t.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Границата пази `budget` от съвпадение в `budget_log`.
            if re.search(rf"\b{re.escape(stem)}\b", text):
                hits.add(str(t.relative_to(root)) if t.is_relative_to(root) else str(t))
    return sorted(hits)


def changed_surface(root: str | Path = ".", *, base: str = "") -> Surface:
    """Какво е променено + какво да се пусне заради него."""
    changed = changed_files(root, base=base)
    if not changed:
        return Surface([], [], True,
                       "Няма открити промени (или git липсва) — пусни целия suite.")
    tests = tests_for(changed, root)
    if not tests:
        return Surface(changed, [], True,
                       "Променените файлове не се покриват от нито един тест по име — "
                       "пусни целия suite.")
    return Surface(changed, tests, False, "")


def summarize(surface: Surface) -> str:
    lines = [f"Променени: {len(surface.changed)} файла"]
    lines += [f"  {c}" for c in surface.changed[:12]]
    if len(surface.changed) > 12:
        lines.append(f"  … и още {len(surface.changed) - 12}")
    if surface.fallback_to_full_suite:
        lines.append(surface.note)
        lines.append("  pytest tests/ -q")
    else:
        lines.append(f"Засегнати тестове: {len(surface.tests)}")
        lines += [f"  {t}" for t in surface.tests]
        lines.append("  pytest -q " + " ".join(surface.tests))
    return "\n".join(lines)


if __name__ == "__main__":
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "tests").mkdir()
        (root / "budget.py").write_text("x = 1\n", encoding="utf-8")
        (root / "tests" / "test_budget.py").write_text("import budget\n", encoding="utf-8")
        (root / "tests" / "test_other.py").write_text("# нищо общо\n", encoding="utf-8")
        (root / "tests" / "test_integration.py").write_text(
            "# ползва budget индиректно\nimport budget\n", encoding="utf-8")

        # Конвенцията по име хваща test_budget.py, споменаването — интеграционния.
        found = tests_for(["budget.py"], root)
        assert "tests/test_budget.py" in found, found
        assert "tests/test_integration.py" in found, found
        assert "tests/test_other.py" not in found, "несвързаният тест не бива да влиза"

        # Границата на думата: `budget` не бива да съвпада в `budget_log`.
        (root / "tests" / "test_wordboundary.py").write_text(
            "name = 'budget_log'\n", encoding="utf-8")
        assert "tests/test_wordboundary.py" not in tests_for(["budget.py"], root)

        # Променен тестов файл се връща сам.
        assert tests_for(["tests/test_other.py"], root) == ["tests/test_other.py"]

        # Не-Python промени не влачат тестове по име.
        assert tests_for(["README.md"], root) == []

        # Без git (или без промени) — честно падане към целия suite, не
        # празен списък, който би минал за "нищо за тестване".
        s = changed_surface(root)
        assert s.fallback_to_full_suite is True
        assert "целия suite" in s.note
        assert "pytest tests/ -q" in summarize(s)

        # Файл без покриващ тест също пада към целия suite, не към нищо.
        s2 = Surface(["genesis_agent/mystery.py"], [], True, "няма покритие — пусни всичко")
        assert "pytest tests/ -q" in summarize(s2)

        # Командата за стесненото пускане е готова за копиране.
        s3 = Surface(["budget.py"], ["tests/test_budget.py"], False, "")
        assert "pytest -q tests/test_budget.py" in summarize(s3)

        assert os.path.exists(root / "budget.py"), "гейтът не пипа диска"

    print("OK")
```

## Pitfalls
- OK
