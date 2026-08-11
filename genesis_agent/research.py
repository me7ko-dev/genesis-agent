#!/usr/bin/env python3
"""
genesis_agent.research — grounded web research с cross-verification.

Разлика със суровия [WEB_SEARCH: заявка]: този модул НЕ връща сурови snippet-и
за Brain-а да си "спомни" отговор от тях. Вместо това:
  1. Търси в N различни източника (genesis_agent.web_search, DuckDuckGo).
  2. За ВСЕКИ източник поотделно пита малкия модел: "само от ТОЗИ текст, какъв
     е отговорът? Цитирай буквално, или кажи 'не е тук'." (grounded extraction
     — моделът не може да halюцинира извън дадения му текст).
  3. Сверява N-те отговора: ако се съгласуват → един консенсус отговор +
     цитати. Ако се разминават → ЧЕСТНО показва разминаването по източник,
     не решава произволно кой е прав. Ако никъде не е намерено → казва го.

Целта: много по-висока честна точност от "моделът search-на веднъж и повярва
на първия snippet" — без да преструва перфектна (100%) точност, която не
съществува за никоя система.
"""
from __future__ import annotations

_EXTRACT_SYS = (
    "Ти извличаш факти САМО от дадения ти текст. Никога не ползвай знание извън "
    "него. Ако отговорът го няма в текста, кажи точно 'НЕ Е ОТКРИТО В ТОЗИ ИЗТОЧНИК' "
    "и нищо друго. Ако го има, дай кратък отговор И буквален цитат от текста, който "
    "го подкрепя."
)

_COMPARE_SYS = (
    "Сравняваш отговори от няколко независими източника на един и същ въпрос. "
    "Ако се СЪГЛАСУВАТ (дори с различни думи) — дай ЕДИН кратък консенсус отговор. "
    "Ако СЕ РАЗМИНАВАТ — не решавай произволно кой е прав; опиши ясно разминаването, "
    "по източник. Ако НИКЪДЕ не е намерено — кажи го честно."
)


def grounded_research(question: str, *, top_n: int = 3) -> str:
    """Безопасно (не хвърля) — при мрежова грешка връща ясно съобщение."""
    question = question.strip()
    if not question:
        return "[RESEARCH] Празен въпрос."

    try:
        from genesis_agent.web_search import search
        results = search(question, max_results=top_n)
    except Exception as e:
        return f"[RESEARCH] Грешка при търсене: {e}"

    if not results:
        return f"[RESEARCH] Няма намерени резултати за: {question}"

    from genesis_agent.brain import Brain
    brain = Brain()

    per_source: list[str] = []
    for r in results:
        title, url, snippet = r.get("title", ""), r.get("url", ""), r.get("snippet", "")
        if not snippet:
            continue
        reply = brain.complete([
            {"role": "system", "content": _EXTRACT_SYS},
            {"role": "user", "content": (
                f"Въпрос: {question}\n\nТекст от източник ({title}, {url}):\n{snippet}"
            )},
        ])
        answer = (reply.raw_text or "").strip()
        # Brain.complete() не хвърля при изчерпана верига — връща обект с
        # raw_text = "Error: ..." (виж brain._error_result). Без тази проверка
        # текстът на грешката влизаше като „отговор от източника" и после
        # излизаше долу с етикет „(проверено през N източника)" — твърдение за
        # cross-check, зад което няма нищо (bug fix, 2026-08-12).
        if not answer or answer.startswith("Error:"):
            continue
        per_source.append(f"[{url}] {answer}")

    if not per_source:
        return f"[RESEARCH] Търсенето върна резултати без съдържание за: {question}"

    if len(per_source) == 1:
        return f"[RESEARCH] {question}\n\n(само 1 източник, без cross-check)\n{per_source[0]}"

    compare_reply = brain.complete([
        {"role": "system", "content": _COMPARE_SYS},
        {"role": "user", "content": (
            f"Въпрос: {question}\n\nОтговори по източник:\n" + "\n\n".join(per_source)
        )},
    ])
    verdict = (compare_reply.raw_text or "").strip()
    if not verdict or verdict.startswith("Error:"):
        # Извличането по източници е минало, но сверяването — не. Връщаме
        # суровите отговори с ясно казано какво липсва, вместо да лепим
        # „(проверено през N източника)" върху текста на грешката. Целият
        # модул съществува заради разликата между „проверено" и „изглежда
        # проверено" (виж модулния docstring).
        return (f"[RESEARCH] {question}\n\n"
                "⚠️ Сверяването между източниците НЕ бе извършено (моделът е "
                "недостъпен) — по-долу са суровите отговори по източник, "
                "БЕЗ cross-check:\n" + "\n".join(per_source))
    return f"[RESEARCH] {question}\n\n{verdict}\n\n(проверено през {len(per_source)} източника)"


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "какво е Python GIL?"
    print(grounded_research(q))
