#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genesis_agent.model_router — адаптивен избор на локален модел според сложността.

Genesis сам преценя колко трудна е задачата и избира мозъка:
    ниво 0 (лесно)  → 3b   (бърз)
    ниво 1 (средно) → 7b   (баланс)
    ниво 2 (трудно) → 14b  (макс качество, бавен)

Плюс ЕСКАЛАЦИЯ: ако избраният модел се провали няколко пъти, се качва на по-голям.
Рутира се само към РЕАЛНО инсталирани модели (проверка през Ollama).
"""
from __future__ import annotations

import os
import re

import requests

# Нивата (env-конфигурируеми). От бърз към мощен.
LOCAL_TIERS = [
    os.environ.get("GENESIS_TIER0", "qwen2.5-coder:3b"),
    os.environ.get("GENESIS_TIER1", "qwen2.5-coder:7b"),
    os.environ.get("GENESIS_TIER2", "qwen2.5-coder:14b"),
]

# Признаци за сложност (в целта на задачата).
_HARD = re.compile(
    r"\b(class|algorithm|parser|pars(e|ing)|compiler|interpreter|concurren|async|"
    r"thread|graph|tree|trie|dijkstra|dynamic\s+programming|state\s+machine|"
    r"recursi|cache|lru|regex|crypto|encrypt|distributed|neural|matrix|optimi|"
    r"simulat|balanced|priority\s+queue|topological|heap|backtrack|multi-?file|"
    r"framework|engine|protocol|scheduler)\b", re.I)
_MED = re.compile(
    r"\b(sort|search|merge|validat|encod|decod|convert|stack|queue|hash|"
    r"anagram|flatten|fibonacci|roman|matrix|palindrom|counter|group|parse)\b", re.I)


def estimate_tier(goal: str) -> int:
    """0=лесно, 1=средно, 2=трудно — от ключови думи + дължина/изисквания."""
    g = goal or ""
    score = 0
    if _HARD.search(g):
        score = 2
    elif _MED.search(g):
        score = 1
    # дълга/многосъставна цел вдига нивото
    if len(g) > 220 and score < 2:
        score += 1
    if g.count(" and ") + g.count(",") >= 4 and score < 2:
        score += 1
    return min(score, 2)


def available_tiers() -> list[bool]:
    """Кои от LOCAL_TIERS са реално инсталирани в Ollama."""
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=2)
        names = [m.get("name", "") for m in r.json().get("models", [])] if r.status_code == 200 else []
    except Exception:
        names = []
    return [any(t.split(":")[0] in n and t.split(":")[-1] in n for n in names) for t in LOCAL_TIERS]


def pick_model(goal: str) -> str | None:
    """Избира модел според сложността, но само измежду инсталираните."""
    tier = estimate_tier(goal)
    avail = available_tiers()
    if not any(avail):
        return None
    # Търсим желаното ниво надолу до първия наличен (ако 14b липсва → 7b и т.н.).
    for t in range(tier, -1, -1):
        if avail[t]:
            return LOCAL_TIERS[t]
    # ако нищо надолу, вземи първия наличен нагоре
    for t in range(tier + 1, len(LOCAL_TIERS)):
        if avail[t]:
            return LOCAL_TIERS[t]
    return None


def next_tier_model(current: str | None) -> str | None:
    """Следващият по-голям НАЛИЧЕН модел (за ескалация при провал)."""
    avail = available_tiers()
    try:
        idx = LOCAL_TIERS.index(current) if current in LOCAL_TIERS else -1
    except ValueError:
        idx = -1
    for t in range(idx + 1, len(LOCAL_TIERS)):
        if avail[t]:
            return LOCAL_TIERS[t]
    return None


if __name__ == "__main__":
    tests = [
        "Reverse a string",
        "Implement bubble sort of a list",
        "Implement an LRU cache class with O(1) get/set using a doubly linked list",
        "Build a recursive descent parser for arithmetic expressions with a state machine",
    ]
    print("налични нива:", dict(zip(LOCAL_TIERS, available_tiers())))
    for g in tests:
        print(f"  ниво {estimate_tier(g)} → {pick_model(g)}   « {g[:50]}")
