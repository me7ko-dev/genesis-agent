"""
genesis_agent.resume — `/resume` и `/rewind` (2026-10-15), както в Claude Code.

/resume — по-ранните разговори на ТАЗИ папка (session_index), най-новият
пръв: дата, първото съобщение, брой реплики; избраният се зарежда в чата.
`genesis -c` е същото за последния.

/rewind — назад до избрано съобщение на оператора: разговорът се реже преди
него, а файловете, които Genesis промени след него, се връщат — с /undo на
всеки ход поред (edit_history.rewind), не с отделен механизъм. Създаденото
от Genesis се трие; каквото операторът е пипнал междувременно, остава и се
казва преди потвърждението.

Без rich и без конзола: `out` печата, `ask` пита да/не, `choose` пита за
номер. Телефонът няма `choose` — там номерът идва след командата (`/rewind 3`).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from genesis_agent.chat_commands import _YES, Conversations, Result

_SHOWN = 20
# Служебните реплики с роля user: бележката на /undo и /rewind, подканата на
# hook Stop и резултатите на текстовите инструменти — не са точки за връщане.
_NOT_OPERATOR = ("[оператор]", "[hook Stop", "[Резултат от tool]")


def _short(text: str, limit: int = 70) -> str:
    flat = " ".join(text.split("\n\n", 1)[0].split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def _number(text: str, top: int) -> int | None:
    try:
        n = int(text.strip().lstrip("#"))
    except ValueError:
        return None
    return n if 1 <= n <= top else None


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# ── /resume ─────────────────────────────────────────────────────────────────

def resume(rest: str, *, messages: Any, workspace: Path, out: Callable[[str], None],
           choose: Callable[[str], str] | None,
           conversations: Conversations | None) -> Result:
    from genesis_agent import session_index
    if conversations is None or messages is None:
        out("/resume не е достъпна оттук.")
        return Result()
    found, bad = session_index.entries(conversations.history_dir, workspace,
                                       exclude=conversations.current())
    broken = (f"\n⚠ {_plural(bad, 'запазен разговор не се чете', 'запазени разговора не се четат')}"
              " (повреден файл) — пропуснат." if bad else "")
    if not found:
        out(f"Няма запазени разговори в тази папка ({workspace}).{broken}")
        return Result()
    pick = rest.strip()
    if not pick:
        lines = [f"Разговори в {workspace} (най-новият пръв):"]
        for i, e in enumerate(found, 1):
            when = datetime.fromtimestamp(e.modified).strftime("%Y-%m-%d %H:%M")
            lines.append(f"{i:>3}. {when} · {_plural(e.count, 'реплика', 'реплики')} · „{e.first}“")
        out("\n".join(lines) + broken)
        if choose is None:
            out("Избери с /resume <номер>.")
            return Result()
        pick = choose("Кой разговор да заредя? (номер, Enter = отказ) > ").strip()
        if not pick:
            out("Нищо не е заредено.")
            return Result()
    n = _number(pick, len(found))
    if n is None:
        out(f"Няма разговор с номер {pick!r} — избери от 1 до {len(found)} (/resume ги показва).")
        return Result()
    entry = found[n - 1]
    try:
        data = session_index.read(entry.path)
    except (OSError, ValueError) as e:
        out(f"⚠ {entry.path.name} не се зарежда: {e}")
        return Result()
    from genesis_agent.agent_core import HISTORY_MAXLEN, restored_history
    # Системният промпт е СЕГАШНИЯТ (свежи факти за машината), не записаният.
    system = str(messages[0].get("content") or "") if messages and \
        messages[0].get("role") == "system" else ""
    restored = restored_history(data, system, maxlen=getattr(messages, "maxlen", None)
                                or HISTORY_MAXLEN)
    # Продължението — в нов файл, старият остава (както /history и -c).
    conversations.new_file()
    when = datetime.fromtimestamp(entry.modified).strftime("%Y-%m-%d %H:%M")
    out(f"✓ Зареден разговорът от {when} ({_plural(entry.count, 'реплика', 'реплики')}) — "
        "продължавай оттам.")
    last = next((m["content"] for m in reversed(restored) if m.get("role") == "assistant"
                 and isinstance(m.get("content"), str) and m["content"].strip()), "")
    if last:
        out("Последният отговор:\n" + last[:600] + ("…" if len(last) > 600 else ""))
    return Result(messages=restored)


# ── /rewind ─────────────────────────────────────────────────────────────────

def _is_operator(m: Any) -> bool:
    content = m.get("content") if isinstance(m, dict) else None
    return (m.get("role") == "user" and isinstance(content, str) and bool(content.strip())
            and not content.startswith(_NOT_OPERATOR))


def rewind(rest: str, *, messages: Any, out: Callable[[str], None], ask: Callable[[str], str],
           choose: Callable[[str], str] | None,
           conversations: Conversations | None) -> Result:
    from genesis_agent import edit_history
    if messages is None:
        out("/rewind не е достъпна оттук.")
        return Result()
    items = list(messages)
    points = [(i, m) for i, m in enumerate(items) if _is_operator(m)]
    if not points:
        out("Няма твои съобщения в този разговор, към които да се върна.")
        return Result()
    pick = rest.strip()
    if not pick:
        first = max(0, len(points) - _SHOWN)
        lines = [("Към кое твое съобщение да се върна? Разговорът се реже преди него, а "
                  "файловете, които Genesis промени след него, се връщат:")]
        lines += [f"{n:>3}. „{_short(m['content'])}“"
                  for n, (_i, m) in enumerate(points[first:], first + 1)]
        out("\n".join(lines))
        if choose is None:
            out("Избери с /rewind <номер>.")
            return Result()
        pick = choose("Номер (Enter = отказ) > ").strip()
        if not pick:
            out("Нищо не е върнато.")
            return Result()
    n = _number(pick, len(points))
    if n is None:
        out(f"Няма съобщение с номер {pick!r} — избери от 1 до {len(points)} (/rewind ги показва).")
        return Result()
    idx, chosen = points[n - 1]
    cut = items[idx:]
    count = edit_history.turns_since(cut)
    plan = edit_history.rewind_plan(count)
    untracked = any(not edit_history.tracked(m) for _i, m in points[n - 1:])
    said = [(f"Връщам разговора до преди съобщение {n} „{_short(chosen['content'])}“ — "
             f"махат се {_plural(len(cut), 'съобщение', 'съобщения')} от историята.")]
    if plan:
        said.append("Файлове:\n  " + "\n  ".join(plan))
    else:
        said.append("Файловете не се пипат — след тази точка Genesis не ги е променял"
                    + (" в това пускане." if untracked else "."))
    if untracked:
        said.append("⚠ Част от съобщенията са от предишно пускане (/resume, genesis -c) — "
                    "техните промени по файлове не се следят и не се връщат.")
    out("\n".join(said))
    if ask("Да върна ли? (Enter = да / не) > ").strip().lower() not in _YES:
        out("Нищо не е върнато.")
        return Result()
    done = edit_history.rewind(count) if count else []
    from genesis_agent.agent_core import bounded_history
    kept = items[:idx]
    left = [d for d in done if d.startswith(("⚠", "❌"))]
    if left:
        # Моделът не бива да мисли, че файлът е както преди точката.
        kept.append({"role": "user", "content":
                     "[оператор] /rewind върна разговора и файловете до по-ранна точка; "
                     "тези файлове НЕ са върнати (променени извън Genesis или грешка):\n"
                     + "\n".join(left)})
    new = bounded_history(kept, getattr(messages, "maxlen", None))
    if conversations is not None:
        # Отрязаното остава в стария файл (/resume го връща); оттук — нов файл.
        conversations.new_file()
        if conversations.save is not None:
            try:
                conversations.save(new)
            except OSError as e:
                out(f"⚠ разговорът не се записа: {e}")
    out(f"↶ Върнато до преди съобщение {n}."
        + ("\n  " + "\n  ".join(done) if done else ""))
    text = str(chosen["content"])
    out("Съобщението беше:\n" + text[:1500] + ("…" if len(text) > 1500 else ""))
    return Result(messages=new)
