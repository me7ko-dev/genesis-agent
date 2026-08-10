"""
BG↔EN преводна обвивка около локален coding модел.

Защо съществува: качеството на coding моделите (qwen2.5-coder и др.) в
разговорния текст на български е нестабилно — живо измерено вмъкване на
руската дума "Вот" вместо българска в противен случай верен отговор
(genesis-agent-public-release паметта, 2026-07-31). Самият КОД е коректен
независимо от езика на подканата — проблемът е в естествения текст около
кода. Решение: превеждаме BG→EN подканата, викаме coding модела на English
(където е най-силен), после превеждаме EN→BG отговора — но пазим код
блоковете (```...```) недокоснати, защото превод на код чупи синтаксис.

Преводачът е специализиран малък модел, не самия coding модел — превод е
много по-лека задача от писане на код, не му трябва 7B+ за нея.

**Избор на преводач (2026-07-31, живо сравнено):** `qwen2.5:3b` (общ модел)
преведе "pair" като "дупа" вместо "двойка" в по-дълъг технически текст —
грозна грешка. `zongwei/gemma3-translator:1b` (специализиран Gemma3
преводач) даде коректно "двойка" и само дребна родова грешка. Студен старт
~55с (еднократно, при първо зареждане на модела), но **топъл — 0.48с** за
кратък текст — практически мигновен, докато е в паметта.
"""
from __future__ import annotations

import json
import re
import urllib.request

OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
TRANSLATOR_MODEL = "zongwei/gemma3-translator:1b"

_CODE_FENCE = re.compile(r"(```.*?```)", re.DOTALL)


def _call(model: str, prompt: str, timeout: int = 60) -> str:
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"].strip()


def translate_bg_to_en(text: str) -> str:
    """Превежда българска подкана на английски. Технически термини/имена
    на функции/променливи остават непокътнати.

    Промптът е нарочно "втвърден" (design note, 2026-08-11, живо
    възпроизведено): когато текстът звучи като coding задача, малкият
    1B преводач с оригиналния (мек) промпт понякога решава да "помогне" и
    директно пише (грешен) код вместо да преведе изречението — потвърдено
    2/2 пъти. Изричното "you do not solve problems, you do not write code"
    + "Bulgarian text: ... / English translation:" формат го връща обратно
    към чист превод (потвърдено 2/2 пъти със същия тест)."""
    prompt = (
        "You are a TRANSLATOR ONLY. You do not answer questions, solve "
        "problems, write code, or explain anything. Your ONLY job is "
        "literal translation.\n"
        "Translate this Bulgarian text to English, preserving its meaning "
        "as an instruction/sentence. Keep code identifiers, function names, "
        "and technical terms unchanged. Do NOT write any code. Do NOT add "
        "commentary. Output ONLY the translated sentence.\n\n"
        f'Bulgarian text: "{text}"\n\nEnglish translation:'
    )
    return _call(TRANSLATOR_MODEL, prompt)


def translate_en_to_bg(text: str) -> str:
    """Превежда английски отговор на български, като пази код блоковете
    (```...```) абсолютно недокоснати — превод на код чупи синтаксиса."""
    parts = _CODE_FENCE.split(text)
    out = []
    for part in parts:
        if part.startswith("```"):
            out.append(part)  # код — недокоснат
            continue
        if not part.strip():
            out.append(part)
            continue
        prompt = (
            "You are a TRANSLATOR ONLY. You do not answer questions, solve "
            "problems, write code, or explain anything. Your ONLY job is "
            "literal translation.\n"
            "Translate this English text to Bulgarian, preserving its "
            "meaning. Do not translate code snippets, function/variable "
            "names in backticks, or technical identifiers — leave them "
            "as-is. Do NOT add commentary. Output ONLY the translated "
            "text.\n\n"
            f'English text: "{part}"\n\nBulgarian translation:'
        )
        out.append(_call(TRANSLATOR_MODEL, prompt))
    return "".join(out)


def ask_coding_model_bg(bg_prompt: str, coding_model: str = "qwen2.5-coder:7b") -> dict:
    """Пълен пайплайн: BG подкана → EN → coding модел → EN отговор → BG.
    Връща dict с всяка стъпка за прозрачност/дебъг."""
    en_prompt = translate_bg_to_en(bg_prompt)
    en_answer = _call(coding_model, en_prompt, timeout=120)
    bg_answer = translate_en_to_bg(en_answer)
    return {
        "bg_prompt": bg_prompt,
        "en_prompt": en_prompt,
        "en_answer": en_answer,
        "bg_answer": bg_answer,
    }


if __name__ == "__main__":
    import sys
    import time

    q = " ".join(sys.argv[1:]) or (
        "Напиши Python функция merge_intervals(intervals), която обединява "
        "застъпващи се интервали в списък от двойки [начало, край]."
    )
    t0 = time.time()
    result = ask_coding_model_bg(q)
    elapsed = time.time() - t0

    print(f"[BG подкана]  {result['bg_prompt']}")
    print(f"[EN подкана]  {result['en_prompt']}")
    print(f"[EN отговор]  {result['en_answer']}")
    print(f"[BG отговор]  {result['bg_answer']}")
    print(f"\n⏱  общо {elapsed:.1f}с")
