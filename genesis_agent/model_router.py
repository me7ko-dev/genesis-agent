#!/usr/bin/env python3
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
    r"framework|engine|protocol|scheduler)\b", re.IGNORECASE)
_MED = re.compile(
    r"\b(sort|search|merge|validat|encod|decod|convert|stack|queue|hash|"
    r"anagram|flatten|fibonacci|roman|matrix|palindrom|counter|group|parse)\b", re.IGNORECASE)


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


# ── Чат: лек въпрос → малък модел (design note, 2026-09-23) ──────────────────
# Операторът: „малките за слабите задачи, силните за по-тежките". Всяко
# съобщение в чата отиваше на 550B модел — и „здрасти" също. Тук се решава
# само дали съобщението е ЯВНО леко; всичко съмнително е тежко, защото
# грешката в двете посоки не струва еднакво: тежък модел на лек въпрос губи
# секунди, лек модел на тежка задача губи работа. Оператор пише и на кирилица,
# и на латиница („napravi backup"), затова корените са и в двете.
_ACTION = re.compile(
    r"(направ|поправ|оправ|напиш|създа|инсталир|изтри|премахн|махн|премест|пусн|"
    r"стартир|провер|намер|отвор|запиш|копир|архив|бекъп|редакт|промен|добав|"
    r"обнов|генерир|изпълн|прочет|покаж|анализ|сравн|свали|качи|слей|комит|"
    r"napra|popra|opra|napish|napis|sazda|syzda|suzda|instal|iztri|premahn|mahn|"
    r"premest|pusn|startir|prover|nameri|otvor|zapish|kopir|arhiv|bekap|backup|"
    r"redakt|promen|dobav|obnov|generir|izpaln|izpyln|prochet|pokaj|pokazh|analiz|"
    r"sravn|svali|kachi|slei|komit|"
    r"\b(make|fix|write|create|install|delete|remove|move|run|start|check|find|"
    r"open|save|copy|edit|change|add|update|generate|execute|read|show|list|"
    r"analy[sz]e|compare|build|deploy|debug|refactor|test|download|upload|merge|commit)\b)",
    re.IGNORECASE)
_WORK_NOUN = re.compile(
    r"(файл|папк|директор|код|скрипт|проект|десктоп|диск|сървър|репо|"
    r"fajl|fail|papk|direktor|\bkod|skript|proekt|desktop|disk|server|repo|"
    r"\b(file|folder|code|script|project|function|class|bug|error)s?\b)",
    re.IGNORECASE)
# Потвърждение продължава ПРЕДИШНАТА задача — кратко е, но не е леко.
_CONFIRM = re.compile(
    r"^\s*(да|давай|добре|ок|окей|продължи|започни|пробвай|може|"
    r"da|davai|dobre|ok|okey|okay|prodalji|prodylji|zapochni|probvai|moje|"
    r"yes|go|sure|continue|proceed|do it)\b", re.IGNORECASE)
# Актуална информация иска търсене в мрежата, тоест инструмент — наживо малкият
# модел без инструменти на „какво време е навън?" само попита за града.
_LIVE_INFO = re.compile(
    # \b отпред навсякъде: „ре-курс-ия" съдържа „курс" (хванато от тест).
    r"\b(времето|навън|прогноз|новин|курс|цен[аи]|борс|резултат|днешн|сега\b|"
    r"vremeto|navan|navun|navyn|prognoz|novin|kurs|cen[ai]\b|bors|rezultat|dneshn|sega\b|"
    r"weather|forecast|news|prices?\b|stock|latest|today|current|score)",
    re.IGNORECASE)
_TECHNICAL = re.compile(r"```|`|https?://|[\\/~]\w|\b\w+\.(py|js|ts|json|ya?ml|md|txt|sh|ps1|exe|zip)\b|[{}<>=;]")

LIGHT_MAX_CHARS = 160


def is_light_request(text: str) -> bool:
    """Явно лек въпрос: кратък, без код/пътища, без действие и без работа."""
    t = (text or "").strip()
    if not t or len(t) > LIGHT_MAX_CHARS or t.count("\n") > 1:
        return False
    return not (_TECHNICAL.search(t) or _ACTION.search(t) or _WORK_NOUN.search(t)
                or _LIVE_INFO.search(t) or _CONFIRM.search(t))


def is_light_turn(messages: list[dict]) -> bool:
    """Дали СЛЕДВАЩИЯТ отговор в чата може да е от лек модел.

    Само в началото на ход (последното съобщение е на потребителя — средата
    на цикъла с инструменти е тежка по дефиниция) и само ако предишният ход
    не е бил работа с инструменти: „а сега колко са?" след търсене на файлове
    е продължение, не нов лек въпрос. GENESIS_CHAT_ROUTING=0 изключва."""
    if os.environ.get("GENESIS_CHAT_ROUTING", "1") == "0":
        return False
    msgs = list(messages)
    if not msgs or msgs[-1].get("role") != "user":
        return False
    for m in reversed(msgs[:-1][-6:]):
        if m.get("role") == "tool" or m.get("tool_calls"):
            return False
        if m.get("role") == "user":
            break
    return is_light_request(str(msgs[-1].get("content") or ""))


_TOOL_TAG = re.compile(r"\[[A-Z][A-Z_]{2,}(?::|\])")


def light_reply_needs_escalation(text: str, tool_calls) -> bool:
    """Лекият модел поиска инструмент → задачата не е била лека. Отговорът
    му се изхвърля и същият ход отива на силния модел — малкият модел не
    върти работа по машината на оператора."""
    return bool(tool_calls) or bool(_TOOL_TAG.search(text or "")) or (text or "").startswith("Error:")

# ── Команда вместо модел (design note, 2026-09-24) ───────────────────────────
# „napravi backup" минаваше през силния модел с инструменти — поне две
# обръщения по ~2 200 токена основа, за да стигне до това, което `/backup`
# прави мигновено. Хваща се само ЦЯЛО съобщение, което е самото намерение:
# „nameri i napravi backup genesis v disk D: v zip fail" (реално съобщение)
# носи цел и формат и остава за модела. Пропуснато намерение струва колкото
# досега; погрешно хванато — един отговор „не".
_FILLER = re.compile(r"\b(моля|molya|molq|please|pls|хайде|haide|ajde)\b")
_SHOW = r"(покажи|покажи ми|pokaji|pokaji mi|pokazhi|pokazhi mi|show|show me|list)"
_COMMAND_INTENTS = [(cmd, re.compile(rx)) for cmd, rx in (
    ("/backup", (
        r"((направи|пусни|napravi|pusni|make|do|run|take)( ми| mi)?( един| edin| a)? )?"
        r"(бекъп|бекап|backup|back up|bekap|bekup|архив|arhiv)( сега| sega| now)?"
        r"|(архивирай|arhiviraj|arhivirai|arhiviray)( сега| sega| now)?")),
    ("/update", (
        r"(обнови|ъпдейтни|obnovi|updatni|apdeitni|update) "
        r"(се|себе си|se|sebe si|yourself|genesis|генезис)"
        r"|(провери|proveri|check) (за|za|for) (обновления|ъпдейт|ъпдейти|obnovleniq|"
        r"obnovlenia|update|updates|apdeit)"
        r"|(има ли|ima li) (нова версия|обновления|ъпдейт|nova versiq|nova versia|obnovleniq|"
        r"obnovlenia|update|apdeit)")),
    ("/skills", (
        _SHOW + r" (уменията|скиловете|umeniqta|umeniyata|umenijata|skilovete|skills|your skills|the skills)"
        r"|(какви|kakvi) (умения|скилове|umeniq|umenia|skilove) (имаш|imash)"
        r"|what skills do you have")),
    ("/models", (
        _SHOW + r" (моделите|веригата|modelite|verigata|models|the models|the chain)"
        r"|(кои|koi) (модели|modeli) (имаш|ползваш|imash|polzvash)"
        r"|which models do you (have|use)")),
    ("/model",
        r"(смени|смяна на|smeni|sameni|smqna na|change|switch)( на| na| the)? (модела|модел|modela|model)"),
    ("/clear", (
        r"(изчисти|почисти|izchisti|pochisti|clear)( the)? (разговора|чата|историята|razgovora|chata|"
        r"istoriqta|istoriyata|chat|conversation|history)"
        r"|(нов разговор|нов чат|nov razgovor|nov chat|new chat|new conversation)")),
    ("/tasks",
        _SHOW + r" (задачите|нишките|zadachite|nishkite|tasks|the tasks|open tasks)"),
    ("/help", (
        r"помощ|pomosht|pomosh|help"
        r"|(какви|kakvi) (команди|komandi) (има|имаш|ima|imash)"
        r"|what commands (are there|do you have)"
        r"|" + _SHOW + r" (командите|komandite|commands|the commands)")),
)]
# Тези променят нещо (архив с --delete в целта; изгубена история) — питат.
CONFIRM_COMMANDS = frozenset({"/backup", "/clear"})


def command_for_request(text: str) -> str | None:
    """Чат команда, която съобщението иска изцяло, или None (→ модела)."""
    t = (text or "").strip().lower()
    if not t or t.startswith("/") or len(t) > 60 or "\n" in t:
        return None
    t = _FILLER.sub(" ", t.replace("ё", "е"))
    t = " ".join(re.sub(r"[?!.,;]+", " ", t).split())
    for cmd, rx in _COMMAND_INTENTS:
        if rx.fullmatch(t):
            return cmd
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
