#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genesis_agent.reflection — мета-обучение: агентът се учи от миналите си грешки.

- record_mission(goal, success, detail): записва изхода на мисия в episodic_memory.
- distill_lessons(last_n): анализира последните мисии, намира ПОВТАРЯЩИ СЕ грешки
  и връща кратки уроци (детерминистично, без LLM разход).
- lessons_for_prompt(): готов текст за инжектиране в system_prompt-а.

Така всяка нова мисия стъпва върху наученото от провалите, не ги повтаря.
"""
from __future__ import annotations

import re
from collections import Counter

from genesis_agent import episodic_memory as _em

# Образец на грешка → кратък урок. Проверяват се в реда на списъка.
_ERROR_LESSONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ModuleNotFoundError|No module named", re.I),
     "Липсващ пакет е чест проблем — пиши със СТАНДАРТНАТА библиотека, без външни зависимости."),
    (re.compile(r"no passing self-test|has NO passing self-test|self.test", re.I),
     "Винаги слагай assert-based self-test най-отдолу, който проверява целта и печата 'OK'."),
    (re.compile(r"AssertionError", re.I),
     "Логиката често не минава собствения тест — провери граничните случаи преди да върнеш кода."),
    (re.compile(r"SyntaxError|IndentationError", re.I),
     "Синтактични грешки са чести — върни един валиден ```python``` блок, внимавай с отстъпите."),
    (re.compile(r"Timeout|timed out", re.I),
     "Избягвай безкрайни цикли/тежки изчисления — кодът трябва да приключва бързо."),
    (re.compile(r"SANDBOX (BLOCKED|DENIED)|blocked", re.I),
     "Избягвай опасни операции (изтриване на системни пътища, os.system) — те се блокират."),
    (re.compile(r"NameError|not defined", re.I),
     "Дефинирай всичко, което ползваш — без недекларирани имена."),
    (re.compile(r"FileNotFoundError", re.I),
     "Не разчитай на външни файлове — създавай нужните данни в самия скрипт."),
]


def detect_reuse(rag_context: str, code: str, threshold: float = 0.25) -> bool:
    """
    Компаундинг метрика: доколко генерираният код реално преизползва инжектирания
    от build_context код на съществуващо умение (не само че е бил показан).
    Прост textual overlap — достатъчен сигнал, без нужда от AST/LLM.
    """
    if not rag_context or "# от умение:" not in rag_context:
        return False
    try:
        injected = rag_context.split("```python", 1)[1].split("```")[0]
    except IndexError:
        return False
    inj_lines = {
        l.strip() for l in injected.splitlines()
        if len(l.strip()) >= 15 and not l.strip().startswith("#")
    }
    if not inj_lines:
        return False
    code_lines = {l.strip() for l in code.splitlines()}
    overlap = inj_lines & code_lines
    return (len(overlap) / len(inj_lines)) >= threshold


def record_mission(goal: str, success: bool, detail: str = "", *, reused_existing: bool = False) -> None:
    """
    Записва изхода на мисия като епизод (безопасно, не хвърля). При провал пази
    СУРОВИЯ текст на грешката, за да може distill_lessons да го категоризира.
    reused_existing=True → мисията реално е композирала инжектиран verified код
    (виж detect_reuse), пази се като таг за reuse_rate().
    """
    tags = ["mission", "success" if success else "failure"]
    if reused_existing:
        tags.append("composed")
    try:
        _em.record_episode(
            goal=goal,
            outcome="success" if success else "failed",
            skill_path="autonomous_loop",
            lessons_learned=[detail[:300]] if (not success and detail) else None,
            tags=tags,
        )
    except Exception:
        pass


def reuse_rate(last_n: int = 100) -> float | None:
    """% от УСПЕШНИТЕ мисии в последните last_n епизода, които реално са композирали
    (преизползвали) инжектиран verified код. None ако няма успешни мисии в прозореца —
    компаундинг ефектът трябва да расте с растежа на библиотеката от умения."""
    try:
        episodes = _em._fetch_all_episodes()[-last_n:]
    except Exception:
        return None
    successes = [e for e in episodes if e.get("outcome") == "success"]
    if not successes:
        return None
    composed = sum(1 for e in successes if "composed" in (e.get("tags") or []))
    return composed / len(successes)


def distill_lessons(last_n: int = 60, top: int = 4) -> list[str]:
    """Категоризира суровите грешки от последните провалени мисии → чести уроци."""
    try:
        episodes = _em._fetch_all_episodes()[-last_n:]
    except Exception:
        return []
    counter: Counter = Counter()
    for ep in episodes:
        if ep.get("outcome") != "failed":
            continue
        raw = " ".join(ep.get("lessons_learned", [])).lower()
        matched = False
        for rx, les in _ERROR_LESSONS:
            if rx.search(raw):
                counter[les] += 1
                matched = True
                break
        if not matched:
            counter["Unknown Error"] += 1
    # "Unknown Error" е диагностичен бъкет (видимост за некатегоризирани
    # провали, добавен нарочно да не се губят тихо), НЕ истински дестилиран
    # урок — lessons_for_prompt() го инжектира буквално в system prompt-а на
    # всяка мисия/оркестрация, а "не повтаряй: Unknown Error" не значи нищо за
    # LLM-а и открадва един от малкото top слота от реален съвет. Затова взимаме
    # top+1 кандидата и го филтрираме след класирането, вместо да го изключим от
    # броенето (broenето само по себе си остава коректно за бъдеща диагностика).
    ranked = [les for les, _ in counter.most_common(top + 1) if les != "Unknown Error"]
    return ranked[:top]


def lessons_for_prompt(last_n: int = 60) -> str:
    """Форматирани уроци за инжектиране в system_prompt-а (или празно)."""
    lessons = distill_lessons(last_n)
    if not lessons:
        return ""
    return "## УРОЦИ ОТ МИНАЛИ ГРЕШКИ (не ги повтаряй)\n" + "\n".join(f"- {l}" for l in lessons)


if __name__ == "__main__":
    # Демонстрация с изкуствени провали.
    record_mission("демо цел А", False, "ModuleNotFoundError: No module named 'pandas'")
    record_mission("демо цел Б", False, "AssertionError in self-test")
    record_mission("демо цел В", False, "No module named 'torch'")
    print("Дестилирани уроци:")
    for l in distill_lessons():
        print("  -", l)
    print("\nЗа промпт:\n" + lessons_for_prompt())
