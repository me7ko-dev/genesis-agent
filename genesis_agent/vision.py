#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genesis_agent.vision — Genesis получава зрение (read-only screen intelligence).

Периодичен screenshot (на всеки 10с) +
vision модел за чат), но БЕЗ зависимост от неговия код — само същия доказан
подход (mss за capture), приложен през СЪЩЕСТВУВАЩИЯ tool-use pattern на
Genesis (genesis_skills.py [READ_FILE]/[WEB_SEARCH]/[LIST_DIR]).

БЕЗОПАСНОСТ:
  - Изключено по подразбиране. Изисква изричен GENESIS_VISION_ENABLED=1
    (същата конвенция като red zone/strict authority в genesis_agent.dna) —
    каквото е на екрана може да е чувствително (пароли, лични съобщения),
    затова НЕ се capture-ва automatically без съзнателно включване.
  - Само НАБЛЮДЕНИЕ. Няма мишка/клавиатура контрол тук — умишлено извън
    обхват (sandbox.py няма прегледани CONFIRM/BLOCKED правила за GUI
    автоматизация; screen control е несравнимо по-рисково от screen reading).
  - Локален vision модел (Ollama `moondream`, ~1.7GB) — офлайн, без изтичане
    на screenshot към облачен API.

Употреба:
    from genesis_agent.vision import describe_screen
    text = describe_screen("какво пише на екрана?")
"""
from __future__ import annotations

import os

import requests

VISION_MODEL = os.environ.get("GENESIS_VISION_MODEL", "moondream")
_OLLAMA_URL = "http://localhost:11434/api/chat"
_TIMEOUT = 60


def vision_enabled() -> bool:
    return os.environ.get("GENESIS_VISION_ENABLED") == "1"


def capture_screen() -> bytes:
    """PNG bytes на текущия екран (през mss)."""
    import mss
    import mss.tools

    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[0])
        return mss.tools.to_png(shot.rgb, shot.size)


def describe_screen(question: str = "") -> str:
    """
    Прави screenshot и го описва през локален vision модел. Безопасно (не
    хвърля) — при изключена способност или недостъпен модел връща ясно
    съобщение вместо да гърми, същия pattern като genesis_skills.web_search.
    """
    if not vision_enabled():
        return (
            "Зрението е ИЗКЛЮЧЕНО по подразбиране (privacy). За да го включиш: "
            "задай GENESIS_VISION_ENABLED=1 в средата преди да пуснеш Genesis."
        )

    try:
        png_bytes = capture_screen()
    except Exception as e:
        return f"Грешка при screenshot: {e}"

    import base64
    img_b64 = base64.b64encode(png_bytes).decode()
    prompt = question.strip() or "Опиши какво виждаш на екрана, кратко и по същество."

    try:
        r = requests.post(
            _OLLAMA_URL,
            json={
                "model": VISION_MODEL,
                "messages": [{"role": "user", "content": prompt, "images": [img_b64]}],
                "stream": False,
            },
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return f"Vision модел ({VISION_MODEL}) грешка: HTTP {r.status_code}"
        content = r.json().get("message", {}).get("content", "").strip()
        return content or "(празен отговор от vision модела)"
    except requests.exceptions.RequestException as e:
        return (
            f"Vision модел ({VISION_MODEL}) недостъпен през Ollama: {e}. "
            f"Провери дали Ollama върви и моделът е pull-нат (`ollama pull {VISION_MODEL}`)."
        )


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:])
    print(describe_screen(q))
