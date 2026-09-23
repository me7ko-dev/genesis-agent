---
name: safe_edit_probe_anchor_uniqueness
category: autonomous
description: Before an anchor-based file edit, check whether the anchor is unique
  in the file; on ambiguity report every matching line and offer a widened anchor
  that is verified unique, so the edit never silently lands on the wrong occurrence.
triggers:
- safe edit probe anchor uniqueness
- как да редактирам файл безопасно
- уникален anchor за редакция
version: '1.0'
author: Genesis
last_updated: '2026-09-19T23:01:40.288352+00:00'
---

## Описание
Before an anchor-based file edit, check whether the anchor is unique in the file; on ambiguity report every matching line and offer a widened anchor that is verified unique, so the edit never silently lands on the wrong occurrence.

## Python Код
```python
"""Проверява anchor-а ПРЕДИ редакция: уникален ли е, и ако не — къде точно съвпада.

Защо съществува: редакция по anchor ("намери този текст, замени го с онзи")
мълчи, когато anchor-ът се среща на повече от едно място. Инструментът или
променя първото срещане, или отказва — и двете са лоши изненади насред
многостъпкова работа. Единственият евтин начин да не стане е да се преброи
преди да се пише.

Три изхода, три различни действия за агента:
    unique   → редактирай спокойно;
    multiple → разшири anchor-а с околен контекст, НЕ редактирай наслуки;
    missing  → прочети файла пак; текстът, който помниш, не е там (най-често
               защото междувременно вече си го редактирал).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AnchorProbe:
    status: str                       # "unique" | "multiple" | "missing" | "error"
    count: int
    lines: list[int] = field(default_factory=list)
    advice: str = ""
    suggestion: str = ""


def _line_numbers(text: str, anchor: str) -> list[int]:
    lines: list[int] = []
    start = 0
    while True:
        idx = text.find(anchor, start)
        if idx == -1:
            return lines
        lines.append(text.count("\n", 0, idx) + 1)
        # +len(anchor), не +1: точно както `str.replace`/`str.count` броят.
        # Ако тук се броеше със застъпване, `lines` щеше да дава повече места
        # от `count` — две части от един и същ гейт, които се разминават,
        # тоест точно съобщението, на което агентът не бива да вярва.
        start = idx + len(anchor)


def _widen(text: str, anchor: str, extra_lines: int = 1) -> str:
    """По-дълъг anchor около ПЪРВОТО срещане — готов за копиране от агента.

    Предложението се връща само когато реално става уникално; иначе агентът
    би го поставил на доверие и пак щеше да удари грешното място.
    """
    idx = text.find(anchor)
    if idx == -1:
        return ""
    line_start = text.rfind("\n", 0, idx) + 1
    line_end = text.find("\n", idx + len(anchor))
    line_end = len(text) if line_end == -1 else line_end
    for _ in range(extra_lines):
        prev = text.rfind("\n", 0, max(line_start - 1, 0))
        line_start = prev + 1 if prev != -1 else 0
        nxt = text.find("\n", line_end + 1)
        line_end = nxt if nxt != -1 else len(text)
    wider = text[line_start:line_end]
    return wider if text.count(wider) == 1 else ""


def probe_anchor(path: str | Path, anchor: str) -> AnchorProbe:
    """Безопасно ли е да се редактира по този anchor. Никога не пише нищо."""
    if not anchor:
        return AnchorProbe("error", 0, advice="Празен anchor — няма какво да се търси.")
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return AnchorProbe("error", 0, advice=f"Файлът не се чете: {e}")

    count = text.count(anchor)
    if count == 1:
        return AnchorProbe("unique", 1, _line_numbers(text, anchor),
                           "Уникален — редактирай.")
    if count == 0:
        return AnchorProbe(
            "missing", 0, [],
            "Няма такъв текст. Прочети файла пак — най-често вече си го редактирал.")
    lines = _line_numbers(text, anchor)
    suggestion = _widen(text, anchor)
    advice = (f"Среща се {count} пъти (редове {', '.join(map(str, lines))}). "
              "НЕ редактирай — разшири anchor-а с околен ред.")
    if suggestion:
        advice += " Предложение по-долу е проверено за уникалност."
    return AnchorProbe("multiple", count, lines, advice, suggestion)


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "sample.py"
        f.write_text(
            "def alpha():\n"
            "    value = 1\n"
            "    return value\n"
            "\n"
            "def beta():\n"
            "    value = 1\n"
            "    return value\n",
            encoding="utf-8")

        # Уникалният anchor е безопасен.
        got = probe_anchor(f, "def alpha():")
        assert got.status == "unique" and got.count == 1 and got.lines == [1], got

        # Повтарящият се anchor е капанът, заради който скилът съществува:
        # "value = 1" изглежда конкретно, а е на два реда.
        got = probe_anchor(f, "    value = 1\n")
        assert got.status == "multiple" and got.count == 2, got
        assert got.lines == [2, 6], got.lines
        assert "НЕ редактирай" in got.advice
        # Предложението трябва РЕАЛНО да е уникално, не просто по-дълго.
        assert got.suggestion, "при двусмислие се очаква работещо предложение"
        assert f.read_text(encoding="utf-8").count(got.suggestion) == 1

        # Липсващият anchor сочи най-честата причина, вместо само "няма го".
        got = probe_anchor(f, "def gamma():")
        assert got.status == "missing" and got.count == 0
        assert "редактирал" in got.advice

        # Броенето следва това, което РЕАЛНАТА редакция би направила: "aaaa"
        # съдържа "aa" два пъти за str.replace, не три. `count` и `lines`
        # трябва да са съгласни — иначе гейтът противоречи на себе си.
        g = Path(tmp) / "aaa.txt"
        g.write_text("aaaa", encoding="utf-8")
        probe = probe_anchor(g, "aa")
        assert probe.count == 2, probe.count
        assert len(probe.lines) == probe.count, (probe.lines, probe.count)
        assert probe.status == "multiple"

        # Грешките се отчитат, не се хвърлят — това е гейт преди запис,
        # той не бива сам да събаря цикъла.
        assert probe_anchor(Path(tmp) / "nope.py", "x").status == "error"
        assert probe_anchor(f, "").status == "error"

        # Гейтът не пипа диска.
        assert f.read_text(encoding="utf-8").count("value = 1") == 2

    print("OK")
```

## Pitfalls
- OK
