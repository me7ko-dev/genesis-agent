---
name: project_checks_detect_and_run
category: autonomous
description: Detect and run a project's OWN checks (ruff/mypy/pytest/npm/cargo/go),
  cheapest first, and report exactly what passed, failed or was skipped — so a change
  can be verified before claiming it is done.
triggers:
- project checks detect and run
- пусни проверките на проекта
- кои линтери и тестове има проектът
version: '1.0'
author: Genesis
last_updated: '2026-09-19T18:50:25.138798+00:00'
---

## Описание
Detect and run a project's OWN checks (ruff/mypy/pytest/npm/cargo/go), cheapest first, and report exactly what passed, failed or was skipped — so a change can be verified before claiming it is done.

## Python Код
```python
"""Открива и пуска СОБСТВЕНИТЕ проверки на проекта, после казва ясно какво минава.

Защо съществува: най-честият начин агент да навреди е да каже "готово" без да
е пуснал нищо. Проверките обаче се казват различно във всеки проект — pytest,
make test, npm test, cargo test — затова "пусни тестовете" не е една команда,
а първо разпознаване. Този модул прави разпознаването, така че проверката да
е един въпрос, а не изследване всеки път.

Нищо не се приема за налично: всяка команда се проверява дали изобщо
съществува, преди да бъде пусната, и липсващият инструмент се отчита като
"пропуснат", не като "провал" — иначе агентът щеше да рапортува счупен проект
на всяка машина без mypy.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

DEFAULT_TIMEOUT = 600


def detect_checks(root: str | Path = ".") -> list[dict[str, object]]:
    """Какви проверки има ТОЗИ проект, най-евтината първа.

    Подредбата е нарочна: линтърът пада за секунда и хваща глупави грешки,
    типовата проверка е следващата по цена, тестовете са последни. Агент,
    който пуска 4-минутен pytest, за да открие липсваща запетая, е скъп агент.
    """
    root = Path(root)
    checks: list[dict[str, object]] = []

    def add(name: str, cmd: list[str], when: bool) -> None:
        if when:
            checks.append({"name": name, "cmd": cmd, "available": bool(shutil.which(cmd[0]))})

    pyproject = root / "pyproject.toml"
    py_text = pyproject.read_text(encoding="utf-8", errors="replace") if pyproject.is_file() else ""

    add("ruff", ["ruff", "check", "."], "ruff" in py_text or bool(list(root.glob("*.py"))))
    add("mypy", ["mypy", "."], "mypy" in py_text)
    add("pytest", ["pytest", "-q"],
        (root / "tests").is_dir() or bool(list(root.glob("test_*.py"))))

    pkg = root / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
        except (ValueError, OSError):
            scripts = {}
        add("npm-lint", ["npm", "run", "lint"], "lint" in scripts)
        add("npm-test", ["npm", "test"], "test" in scripts)

    add("cargo", ["cargo", "test"], (root / "Cargo.toml").is_file())
    add("go", ["go", "test", "./..."], (root / "go.mod").is_file())
    return checks


def run_checks(root: str | Path = ".", *, timeout: int = DEFAULT_TIMEOUT,
               stop_on_fail: bool = False) -> dict[str, object]:
    """Пуска откритите проверки и връща какво реално се случи.

    `stop_on_fail=True` спира на първия провал — по-бързо за цикъла
    "поправи → провери пак", където по-нататъшните резултати и без това
    ще бъдат препуснати след следващата поправка.
    """
    results: list[dict[str, object]] = []
    for check in detect_checks(root):
        if not check["available"]:
            results.append({"name": check["name"], "status": "skipped",
                            "detail": f"{check['cmd'][0]} не е инсталиран"})
            continue
        try:
            proc = subprocess.run(check["cmd"], cwd=str(root), capture_output=True,
                                  text=True, timeout=timeout, check=False)
        except (subprocess.TimeoutExpired, OSError) as e:
            results.append({"name": check["name"], "status": "error", "detail": str(e)[:300]})
            continue
        output = (proc.stdout or "") + (proc.stderr or "")
        results.append({
            "name": check["name"],
            "status": "pass" if proc.returncode == 0 else "fail",
            "detail": output.strip()[-1500:],
        })
        if stop_on_fail and proc.returncode != 0:
            break

    failed = [r for r in results if r["status"] in ("fail", "error")]
    return {"ok": not failed, "results": results,
            "summary": summarize(results)}


def summarize(results: list[dict[str, object]]) -> str:
    """Един ред на проверка — това е, което агентът показва на човека.

    Провалите носят и последните редове от изхода: "pytest падна" без
    причината просто праща човека да пусне командата сам.
    """
    if not results:
        return "Не открих проверки в този проект."
    marks = {"pass": "✓", "fail": "✗", "skipped": "–", "error": "!"}
    lines = []
    for r in results:
        line = f"{marks.get(str(r['status']), '?')} {r['name']}"
        if r["status"] in ("fail", "error"):
            tail = str(r["detail"]).splitlines()[-3:]
            line += "\n    " + "\n    ".join(tail)
        elif r["status"] == "skipped":
            line += f"  ({r['detail']})"
        lines.append(line)
    failed = [r["name"] for r in results if r["status"] in ("fail", "error")]
    verdict = "ВСИЧКО МИНАВА" if not failed else f"ПАДА: {', '.join(map(str, failed))}"
    return "\n".join(lines) + f"\n{verdict}"


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Празна директория: няма какво да се пусне, и това не е провал.
        assert detect_checks(root) == [], "празен проект не бива да измисля проверки"

        # Python проект: линтърът трябва да е ПРЕДИ тестовете, защото е по-евтин.
        (root / "tests").mkdir()
        (root / "pyproject.toml").write_text("[tool.ruff]\n[tool.mypy]\n", encoding="utf-8")
        names = [c["name"] for c in detect_checks(root)]
        assert names.index("ruff") < names.index("pytest"), "евтиното върви първо"
        assert "mypy" in names, "mypy в pyproject.toml трябва да бъде открит"

        # Node проект: само скриптовете, които реално съществуват.
        (root / "package.json").write_text(
            json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8")
        node_names = [c["name"] for c in detect_checks(root)]
        assert "npm-test" in node_names
        assert "npm-lint" not in node_names, "не измисляй скрипт, който липсва"

        # Липсващият инструмент е "пропуснат", не "провал" — иначе всяка
        # машина без mypy би изглеждала като счупен проект.
        skipped = {"name": "mypy", "status": "skipped", "detail": "mypy не е инсталиран"}
        assert "ПАДА" not in summarize([skipped])
        assert "✗" not in summarize([skipped])

        # Провалът носи причината със себе си.
        out = summarize([{"name": "pytest", "status": "fail",
                          "detail": "E   assert 1 == 2\nFAILED tests/test_x.py::test_y"}])
        assert "FAILED tests/test_x.py::test_y" in out
        assert "ПАДА: pytest" in out

    print("OK")
```

## Pitfalls
- OK
