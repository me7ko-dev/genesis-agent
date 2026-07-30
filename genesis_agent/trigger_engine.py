#!/usr/bin/env python3
"""
genesis_agent/trigger_engine.py — Skill Trigger система за Genesis Agent.

Парсира потребителски заявки и активира подходящото умение директно
(без да минава през Brain/LLM), ако trigger keywords съвпадат.

Употреба:
    from genesis_agent.trigger_engine import TriggerEngine
    engine = TriggerEngine()
    result = engine.match_and_run("направи retry decorator за API повиквания")
    if result:
        print(result)
    else:
        # Няма match → пускай Brain/LLM
        pass
"""

from __future__ import annotations

import logging
import re

from genesis_agent.skill_loader import load_skills_index, search_skills, skill_view

log = logging.getLogger("genesis.trigger")

# Праг на Score — колко ключови думи трябва да съвпадат за активиране
TRIGGER_THRESHOLD = 2


class TriggerEngine:
    """
    Проверява дали потребителска заявка директно съответства на
    съществуващо умение чрез keyword matching.
    """

    def __init__(self, threshold: int = TRIGGER_THRESHOLD):
        self.threshold = threshold

    def match(self, query: str) -> dict | None:
        """
        Търси най-релевантното умение за дадена заявка.

        Returns:
            dict с метаданни на умението или None ако няма достатъчно силно съвпадение.
        """
        results = search_skills(query, top_n=1)
        if not results:
            return None

        best = results[0]
        # Изчисляваме score ръчно за да проверим threshold
        query_words = set(re.findall(r"[a-z0-9_]+", query.lower()))
        triggers = best.get("triggers", [])
        if isinstance(triggers, str):
            triggers = [triggers]
        trigger_words = {
            w for t in triggers for w in re.findall(r"[a-z0-9_]+", t.lower())
        }
        name_words = set(re.findall(r"[a-z0-9_]+", best.get("name", "").lower()))
        score = len(query_words & (trigger_words | name_words))

        log.debug(f"[trigger] Query='{query}' → Best='{best.get('name')}' score={score}")

        if score >= self.threshold:
            return best
        return None

    def match_and_run(self, query: str) -> str | None:
        """
        Търси умение и ако намери съвпадение — зарежда и описва го.

        NOTE: Не изпълнява кода автоматично по сигурност.
              Връща описание + пътя на файла за потвърждение от autonomous_loop.

        Returns:
            str с описание или None.
        """
        skill_meta = self.match(query)
        if not skill_meta:
            return None

        name = str(skill_meta.get("name") or "")
        category = skill_meta.get("category", "?")
        description = skill_meta.get("description", "")
        file_path = skill_meta.get("file_path", "")

        try:
            data = skill_view(name)
            code_preview = data["code"][:200] + ("..." if len(data["code"]) > 200 else "")
        except Exception:
            code_preview = "(не може да се зареди кодът)"

        return (
            f"🔵 Открито съществуващо умение: **{name}** ({category})\n"
            f"📄 Описание: {description}\n"
            f"📁 Файл: {file_path}\n"
            f"```python\n{code_preview}\n```"
        )

    def list_triggers(self) -> list[dict]:
        """Връща всички умения с техните trigger keywords."""
        index = load_skills_index()
        return [
            {
                "name": s.get("name"),
                "category": s.get("category"),
                "triggers": s.get("triggers", []),
            }
            for s in index.values()
        ]


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    engine = TriggerEngine()

    test_queries = [
        "искам retry decorator с exponential backoff",
        "направи pub sub event система",
        "създай pandas dataframe processor",
        "нещо напълно случайно за марсианци",
    ]

    for q in test_queries:
        result = engine.match_and_run(q)
        if result:
            print(f"\n✅ Query: '{q}'")
            print(result)
        else:
            print(f"\n❌ Query: '{q}' → Няма match (ще се изпрати към Brain/LLM)")
