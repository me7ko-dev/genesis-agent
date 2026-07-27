"""
genesis_agent.thread_worker — 24/7 цикълът работи по РЕАЛНИТЕ нишки на потребителя
(design note, 2026-07-26), а не само по генерични goal_engine теми.

ЗАЩО НЕ ПРЕЗ run_autonomous_loop: той е генератор на УМЕНИЯ — иска цел с
формата "напиши функция X със self-test, който печата OK", изпълнява я в
sandbox и я записва в библиотеката. Реалните нишки на потребителя не са с тази
форма ("проучване на текущата схема", "интеграция със Stripe"). Прекараш ли
ги през него, произвежда безсмислени умения и замърсява библиотеката.

КАКВО ПРАВИ ВМЕСТО ТОВА: подготвителен пас. За една отворена нишка Genesis
свършва предварителната работа, която не иска решения — проучва, чете, събира
факти — и оставя КОНКРЕТНИ находки плюс предложена следваща стъпка. потребителят
сутрин намира свършена подготовка, не чака.

ГРАНИЦА НА БЕЗОПАСНОСТТА (умишлена и тясна, това върви без надзор):
  • САМО read-only инструменти — READ_FILE / LIST_DIR / WEB_SEARCH / RESEARCH.
  • БЕЗ RUN_CMD, БЕЗ WRITE_FILE, БЕЗ browser действия, БЕЗ DELEGATE.
  • Единствената промяна, която прави, е в собствената workspace памет
    (находки + предложена следваща стъпка по нишката).
Тоест: Genesis може да СЪБИРА информация без надзор, но не и да променя
машината или проектите на потребителя, докато той спи. Изпълнението остава
негово решение — предложението го чака в `!tasks`.
"""
from __future__ import annotations

from dataclasses import dataclass

_MAX_TOOL_ROUNDS = 4  # горна граница на един подготвителен пас

_SYSTEM = """Ти си Genesis. Работиш САМ, без надзор, по една отворена работна нишка на потребителя.

Задачата ти НЕ е да свършиш работата — а да подготвиш почвата, за да може потребителят
да продължи бързо. Събери конкретното, което липсва: прочети релевантни файлове,
провери как стоят нещата, потърси в мрежата ако е нужно.

Имаш САМО инструменти за четене/търсене. Не можеш да променяш нищо — това е
нарочно, работиш без надзор.

КАК ДА НЕ СЕ ИЗЛЪЖЕШ (наблюдавани реални грешки в този режим):
- Неуспешен READ_FILE НЕ значи, че файлът не съществува — най-често пътят е
  сгрешен. Преди да заключиш, че нещо липсва, пусни LIST_DIR на директорията
  и виж какво реално има там. "Не го намерих на този път" ≠ "не съществува".
- LIST_DIR реже дългите списъци. Не изкарвай общ брой от отрязан списък —
  ако ти трябва точен брой, кажи че не си го установил.
- По-добре "не успях да установя X" отколкото уверено грешен отговор. потребителят
  ще действа по това, което напишеш, докато ти вече не си там да се поправиш.

Когато си събрал достатъчно, върни КРАТКО (под 1200 символа):
НАХОДКИ: 2-5 конкретни неща, които научи (реални факти от инструментите, не общи
приказки; ако нищо полезно не си намерил — кажи го честно).
СЛЕДВА: една конкретна следваща стъпка за потребителя.

Никога не измисляй факти. Цитирай това, което инструментите реално върнаха."""


import re as _re

# Находките отиват в Discord И се пазят в нишката. Този режим върви БЕЗ надзор
# и може да чете файлове (READ_FILE няма гейт за чувствителни пътища), така че
# ако модел цитира парче от .env или ключ в "находките си", то би изтекло в
# Discord. Затова всичко минава през редакция преди запис/изпращане.
_SECRET_PATTERNS: list[_re.Pattern[str]] = [
    _re.compile(r"\b(sk|pk|rk)[-_][A-Za-z0-9_\-]{16,}"),          # OpenAI-подобни
    _re.compile(r"\bhf_[A-Za-z0-9]{16,}"),                         # HuggingFace
    _re.compile(r"\bgsk_[A-Za-z0-9]{16,}"),                        # Groq
    _re.compile(r"\bnvapi-[A-Za-z0-9_\-]{16,}"),                   # NVIDIA
    _re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),                  # GitHub
    _re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}"),                     # Google
    _re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),               # Slack
    # KEY=стойност / TOKEN: стойност — общият случай в .env файлове
    _re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|WEBHOOK))\s*[=:]\s*\S{8,}"),
    _re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"),                       # дълги непрозрачни низове
]


def redact_secrets(text: str) -> str:
    """Маха всичко, което прилича на ключ/токен. По-скоро прекалено агресивно,
    отколкото недостатъчно — находките са проза за човек, не данни за машина,
    така че загубата на един дълъг идентификатор е приемлива цена."""
    if not text:
        return text
    out = text
    for rx in _SECRET_PATTERNS:
        out = rx.sub(lambda m: (m.group(1) + "=<скрито>") if m.re.groups else "<скрито>", out)
    return out


@dataclass
class ThreadWorkResult:
    thread_id: int
    title: str
    findings: str
    ok: bool
    rounds: int = 0
    error: str = ""


def _readonly_tools():
    from genesis_agent.tool_schemas import READONLY_TOOLS
    return READONLY_TOOLS


def prepare_thread(thread: dict, max_rounds: int = _MAX_TOOL_ROUNDS) -> ThreadWorkResult:
    """Един read-only подготвителен пас по нишка. Никога не хвърля."""
    tid = int(thread.get("id", 0))
    title = str(thread.get("title", ""))
    next_step = str(thread.get("next_step", ""))
    notes = str(thread.get("notes", ""))

    try:
        from genesis_agent.brain import Brain
        import genesis_skills
        from genesis_agent import workspace_memory as wm
    except Exception as e:
        return ThreadWorkResult(tid, title, "", False, error=f"import: {e}")

    user_msg = f"Нишка: {title}"
    if next_step:
        user_msg += f"\nПланирана следваща стъпка: {next_step}"
    if notes:
        user_msg += f"\nБележки от преди: {notes[:600]}"

    messages = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_msg}]

    brain = Brain(min_size_b=120)
    tools = _readonly_tools()
    rounds = 0

    try:
        for _ in range(max_rounds):
            reply = brain.complete(messages, tools=tools)
            text = (reply.raw_text or "").strip()
            calls = getattr(reply, "tool_calls", None)

            if calls:
                rounds += 1
                messages.append({"role": "assistant", "content": text, "tool_calls": calls})
                for tc in calls:
                    fn = tc.get("function", {}) or {}
                    name = fn.get("name", "")
                    result = genesis_skills.dispatch_tool_call(name, fn.get("arguments"))
                    messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                     "name": name, "content": str(result)[:3000]})
                continue

            # Моделът може да ползва и текстовите тагове (моделите в ротацията
            # се различават) — изпълняваме САМО read-only подмножеството.
            tag_results = genesis_skills.parse_and_execute_readonly_tools(text)
            if tag_results:
                rounds += 1
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user",
                                 "content": "Резултат:\n" + "\n\n".join(tag_results)[:3000] +
                                            "\n\nСега дай НАХОДКИ и СЛЕДВА."})
                continue

            if text.startswith("Error:"):
                return ThreadWorkResult(tid, title, "", False, rounds, error=text[:200])

            findings = redact_secrets(text[:1500])
            _persist(wm, tid, findings, notes)
            return ThreadWorkResult(tid, title, findings, True, rounds)

        # Изчерпани рундове — питаме за заключение с наличното. Ако нишката
        # изобщо не е решима само с четене (напр. "провери дали този API ключ
        # работи" иска реално извикване), това само по себе си е полезен
        # резултат за потребителя — по-добре от мълчалив провал.
        messages.append({"role": "user", "content":
            "Стига проучване. Дай НАХОДКИ и СЛЕДВА с наученото дотук. "
            "Ако тази задача НЕ може да се придвижи само с четене/търсене — кажи го "
            "изрично в НАХОДКИ (какво точно е нужно: команда, ключ, достъп, решение от "
            "потребителят), и нека СЛЕДВА е точно това действие. Празен отговор не е опция."})
        reply = brain.complete(messages)
        findings = redact_secrets((reply.raw_text or "").strip()[:1500])
        if findings and not findings.startswith("Error:"):
            _persist(wm, tid, findings, notes)
            return ThreadWorkResult(tid, title, findings, True, rounds)
        return ThreadWorkResult(tid, title, "", False, rounds, error="без заключение")
    except Exception as e:
        return ThreadWorkResult(tid, title, "", False, rounds, error=str(e)[:200])


def _persist(wm, tid: int, findings: str, old_notes: str) -> None:
    """Записва находките в бележките и вдига предложената следваща стъпка.
    Пази предишните бележки (подрязани) — историята на нишката е полезна."""
    proposed = ""
    for line in findings.splitlines():
        s = line.strip()
        if s.upper().startswith(("СЛЕДВА", "NEXT")):
            proposed = s.split(":", 1)[-1].strip()
            break
    note = (f"[авто-подготовка] {findings[:700]}\n\n" + old_notes)[:1800]
    try:
        wm.update_thread(tid, next_step=proposed or "", notes=note)
    except Exception:
        pass


def prepare_open_threads(limit: int = 2) -> list[ThreadWorkResult]:
    """Подготвителен пас по най-скоро пипнатите отворени нишки."""
    try:
        from genesis_agent import workspace_memory as wm
        threads = wm.list_threads("open", limit)
    except Exception:
        return []
    return [prepare_thread(t) for t in threads]


if __name__ == "__main__":
    for r in prepare_open_threads(1):
        print(f"#{r.thread_id} {r.title} — {'OK' if r.ok else 'ПРОВАЛ: ' + r.error}")
        print(r.findings or "")
