---
name: sibling_paths_missing_the_guard
category: autonomous
description: Find the sibling code paths that reach the same resource without the
  guard one path already has — a check added on the hard path leaves the easy one
  open.
triggers:
- sibling paths missing the guard
- Find the sibling code paths that reach the same resource without the guard one path
  already has — a check added on the h
- кои пътища нямат същата проверка
- съседните врати към същия ресурс
version: '1.0'
author: Genesis
last_updated: '2026-09-20T15:09:36.409754+00:00'
---

## Описание
Find the sibling code paths that reach the same resource without the guard one path already has — a check added on the hard path leaves the easy one open.

## Python Код
```python
"""Щом една врата е с ключалка, кои съседни водят към същото без нея.

Защо съществува: защитите се добавят по пътя, по който някой се е сетил.
Реален случай от този проект — `cat ~/.genesis/.env` минаваше през гейт от
месеци, а `[READ_FILE: ~/.genesis/.env]` не минаваше през нищо. Гейтът пазеше
трудния път, а лесният — този, който моделът реално ползва — беше отворен.
Намирането стана с изброяване на ръка: кои други функции стигат до същия
ресурс. Това го прави механично.

Как: разбира кода с `ast` и за всяка функция пита две неща — докосва ли
ресурса (извиква ли нещо от `reaches`) и споменава ли пазача (`guard`).
Функция, която докосва ресурса без пазача, е кандидат за същата дупка.

Границите са истински и се казват направо, защото инструмент, на който се
вярва сляпо, е по-опасен от липсващ:

  • Пазач, приложен в ИЗВИКВАЩАТА функция, тук чете като липсващ (фалшива
    тревога). Затова изходът е „кандидати за проверка", не „дупки".
  • Функция, която стига до ресурса през помощна функция, чете като чиста
    (пропуск). Затова `reaches` трябва да включва и помощните имена, щом ги
    знаеш — виж `suggest_reaches`.
  • Това е статичен прочит на имена, не анализ на потока. Не замества
    четенето на кода; свива го до няколко функции, които СИ СТРУВА да се
    прочетат.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Finding:
    module: str
    function: str
    reaches: list[str] = field(default_factory=list)
    guarded: bool = False

    def __str__(self) -> str:
        mark = "✅" if self.guarded else "⚠️ "
        return f"{mark} {self.module}:{self.function}  ({', '.join(sorted(set(self.reaches)))})"


def _called_names(node: ast.AST) -> list[str]:
    """Имената на всичко извикано вътре — и `open(...)`, и `p.read_text(...)`."""
    names: list[str] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name):
            names.append(func.id)
        elif isinstance(func, ast.Attribute):
            names.append(func.attr)
    return names


def _mentions(node: ast.AST, needle: str) -> bool:
    """Споменава ли функцията пазача — като извикване или просто по име."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == needle:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == needle:
            return True
    return False


def audit_source(source: str, module: str, *, guard: str,
                 reaches: list[str]) -> list[Finding]:
    """Функциите в `source`, които докосват ресурса — с и без пазача."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    wanted = set(reaches)
    out: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        hits = [n for n in _called_names(node) if n in wanted]
        if not hits:
            continue
        out.append(Finding(module=module, function=node.name, reaches=hits,
                            guarded=_mentions(node, guard)))
    return out


def audit(paths: list[str] | str, *, guard: str, reaches: list[str],
          root: str | Path = ".") -> list[Finding]:
    """Същото, но по файлове. Подредено: непазените първи."""
    root = Path(root)
    if isinstance(paths, str):
        paths = [paths]
    findings: list[Finding] = []
    for rel in paths:
        path = root / rel
        if not path.is_file():
            continue
        findings += audit_source(path.read_text(encoding="utf-8", errors="replace"),
                                  rel, guard=guard, reaches=reaches)
    return sorted(findings, key=lambda f: (f.guarded, f.module, f.function))


def suggest_reaches(kind: str) -> list[str]:
    """Обичайните имена за един вид ресурс — начална точка, не пълен списък."""
    table = {
        "file_read": ["open", "read_text", "read_bytes", "read", "iterdir", "glob", "rglob"],
        "file_write": ["write_text", "write_bytes", "unlink", "rmtree", "rename", "mkdir"],
        "shell": ["run", "Popen", "system", "check_output", "call"],
        "network": ["get", "post", "put", "delete", "request", "urlopen"],
    }
    return table.get(kind, [])


def report(findings: list[Finding], *, guard: str) -> str:
    unguarded = [f for f in findings if not f.guarded]
    guarded = [f for f in findings if f.guarded]
    lines = [f"Пазач: `{guard}` — {len(guarded)} с него, {len(unguarded)} без."]
    if unguarded:
        lines.append("")
        lines.append("Кандидати за същата дупка (прочети ги, преди да заключиш):")
        lines += [f"  {f}" for f in unguarded]
    if guarded:
        lines.append("")
        lines.append("Минават през пазача:")
        lines += [f"  {f}" for f in guarded]
    if not findings:
        lines.append("Нито една функция не докосва тези имена — провери `reaches`.")
    return "\n".join(lines)


if __name__ == "__main__":
    SAMPLE = '''
import subprocess
from pathlib import Path

def guarded_read(path):
    if sensitive_path_reason(path):
        return "отказано"
    return Path(path).read_text()

def unguarded_read(path):
    return Path(path).read_text()

def no_resource(x):
    return x + 1

def runs_a_command(cmd):
    return subprocess.run(cmd)
'''
    found = audit_source(SAMPLE, "sample.py", guard="sensitive_path_reason",
                          reaches=suggest_reaches("file_read"))
    names = {f.function: f.guarded for f in found}
    assert names == {"guarded_read": True, "unguarded_read": False}, names
    assert "no_resource" not in names, "функция без ресурс не е находка"

    shell = audit_source(SAMPLE, "sample.py", guard="assess_command",
                          reaches=suggest_reaches("shell"))
    assert [f.function for f in shell] == ["runs_a_command"], shell

    text = report(found, guard="sensitive_path_reason")
    assert "unguarded_read" in text
    assert "Кандидати" in text

    assert audit_source("def broken(:", "x.py", guard="g", reaches=["open"]) == []
    assert suggest_reaches("няма такъв вид") == []

    print("OK")
```

## Pitfalls
- доказано срещу реален случай: върху genesis_skills.py преди 03cb7dc посочи _tool_read_file и _tool_edit_file; вторият се оказа непозната дотогава дупка
