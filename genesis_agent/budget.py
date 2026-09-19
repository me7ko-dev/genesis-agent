#!/usr/bin/env python3
"""
genesis_agent.budget — token/call observability слой.

Единствената обща дупка във всичко построено в тази сесия (мисии, ensemble,
self-modify, 24/7 цикъл): всички минават през Brain.complete(),
но никой досега не четеше 'usage' полето от API отговора. Този модул го
пази — просто JSONL лог, никакви external dependencies, никога не хвърля.

Употреба:
    from genesis_agent.budget import record_usage, today_totals, daily_totals
    record_usage(provider="huggingface", model="Qwen2.5-Coder-32B-Instruct",
                 prompt_tokens=120, completion_tokens=340)
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any

log = logging.getLogger("genesis.budget")

from genesis_agent.config import (
    DATA_DIR,
    FRESH_TOOL_RESULTS,
    STALE_TOOL_RESULT_MAX_CHARS,
    TOOL_RESULT_MAX_CHARS,
)

LOG_PATH = DATA_DIR / "budget_log.jsonl"

# Маркерът, с който текстовият tool път (genesis_skills.parse_and_execute_tools
# → "[Резултат]:\n...") инжектира резултати като system съобщение. Native
# пътят ги слага като role="tool"; и двата трябва да минават през бюджета,
# иначе половината фронтенди го заобикалят тихо.
_TEXT_RESULT_PREFIX = "[Резултат]:"


def clip_for_context(text: str, limit: int | None = None) -> str:
    """Реже ЕДИН tool резултат до config.TOOL_RESULT_MAX_CHARS, преди да влезе
    в историята на разговора. Операторът вижда пълния изход както винаги —
    конзолата и GUI-то не минават оттук; пести се само контекстът на модела.

    Защо изобщо: до момента целият изход на един tool влизаше в messages и
    оттам се препращаше пак на ВСЕКИ следващ рунд, докато не изпадне от
    прозореца. Един `cat` на голям лог или шумен `pip install` така се плаща
    по десет пъти. record_usage() по-долу го МЕРИ; това е другата половина —
    да го ограничи.

    Реже средата, не края: началото казва какво е тръгнало да се прави, краят
    носи изхода, който решава нещо (traceback, "Successfully installed",
    последните редове на лога). Средата на дълъг изход е точно частта, която
    никой не чете. Обичайното `text[:limit]` изхвърля именно грешката накрая и
    после моделът гадае защо е паднало.
    """
    if limit is None:
        limit = TOOL_RESULT_MAX_CHARS
    if limit <= 0 or len(text) <= limit:
        return text
    cut = len(text) - limit
    head = limit * 2 // 3
    tail = limit - head
    return (
        text[:head]
        + f"\n\n… [отрязани {cut} символа от средата — операторът вижда пълния изход] …\n\n"
        + text[-tail:]
    )


def _is_tool_result(msg: dict) -> bool:
    """Съобщение, което носи ИЗХОД от инструмент — по който и от двата пътя."""
    if msg.get("role") == "tool":
        return True
    return (msg.get("role") == "system"
            and str(msg.get("content", "")).startswith(_TEXT_RESULT_PREFIX))


def budget_history(messages, *, fresh: int | None = None,
                   stale_limit: int | None = None) -> list[dict]:
    """Свива СТАРИТЕ tool резултати точно преди заявката тръгне към модела.

    Това е другата половина на clip_for_context() и същинската икономия.
    clip_for_context пази историята от абсурдни размери на входа, но не решава
    основния разход: един tool резултат се праща наново на ВСЕКИ следващ рунд,
    докато не изпадне от прозореца. На рунд 10 първият `pytest` изход се плаща
    за десети път, макар моделът да е реагирал на него още на рунд 2 — оттам
    нататък от него е нужно само "какво беше пуснато и как завърши".

    Затова: последните `fresh` резултата остават както са (моделът работи
    върху тях СЕГА), всичко по-старо пада до `stale_limit`. Свиването е
    същото middle-out — началото казва какво е било пуснато, краят как е
    завършило; изяжда се средата, която на този етап никой не чете.

    Никога не мутира входа и никога не пипа system промпта, ролите или
    tool_call_id-тата — връща нов списък с нови dict-ове само за съобщенията,
    които реално се свиват. Извикващият (Brain.complete) праща резултата;
    неговата собствена история остава пълна, за да може операторът да я
    запише/прегледа непокътната.
    """
    fresh = FRESH_TOOL_RESULTS if fresh is None else fresh
    stale_limit = STALE_TOOL_RESULT_MAX_CHARS if stale_limit is None else stale_limit
    msgs = list(messages)
    if stale_limit <= 0:
        return msgs

    result_idx = [i for i, m in enumerate(msgs) if isinstance(m, dict) and _is_tool_result(m)]
    stale = set(result_idx[:-fresh] if fresh > 0 else result_idx)
    if not stale:
        return msgs

    out = []
    for i, m in enumerate(msgs):
        if i in stale:
            content = str(m.get("content", ""))
            if len(content) > stale_limit:
                m = {**m, "content": clip_for_context(content, limit=stale_limit)}
        out.append(m)
    return out


def record_usage(*, provider: str, model: str, prompt_tokens: int,
                  completion_tokens: int, context: str = "") -> None:
    """Append-only запис. Безопасно — никога не хвърля (логването не бива
    да чупи мисии)."""
    try:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "provider": provider,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "context": context[:120],
        }
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read_entries():
    if not LOG_PATH.exists():
        return
    try:
        with LOG_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception as e:
                    log.debug("budget: пропускам развален JSONL ред: %s", e)
                    continue
    except OSError as e:
        log.debug("budget: не мога да прочета %s: %s", LOG_PATH, e)
        return


def daily_totals(day: date | None = None) -> dict:
    """Обобщение за конкретен ден (по подразбиране днес, UTC — записите се
    пазят с UTC timestamp, сравнение с локална дата дава грешен резултат
    точно около границата на деня): calls/tokens общо + разбивка по
    доставчик. Безопасно — при повреден/липсващ лог връща нули."""
    day = day or datetime.now(timezone.utc).date()
    day_str = day.isoformat()
    # Смесени стойности (броячи + вложена разбивка), затова Any.
    totals: dict[str, Any] = {
        "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "by_provider": defaultdict(lambda: {"calls": 0, "total_tokens": 0})}
    for e in _read_entries():
        ts = e.get("ts", "")
        if not ts.startswith(day_str):
            continue
        totals["calls"] += 1
        totals["prompt_tokens"] += e.get("prompt_tokens", 0)
        totals["completion_tokens"] += e.get("completion_tokens", 0)
        totals["total_tokens"] += e.get("total_tokens", 0)
        prov = e.get("provider", "?")
        totals["by_provider"][prov]["calls"] += 1
        totals["by_provider"][prov]["total_tokens"] += e.get("total_tokens", 0)
    totals["by_provider"] = dict(totals["by_provider"])
    return totals


def today_totals() -> dict:
    return daily_totals(datetime.now(timezone.utc).date())


def range_totals(days: int = 7) -> dict:
    """Обобщение за последните N дни (по подразбиране седмица, UTC)."""
    from datetime import timedelta
    totals: dict[str, Any] = {
        "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "by_provider": defaultdict(lambda: {"calls": 0, "total_tokens": 0})}
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    for e in _read_entries():
        ts = e.get("ts", "")
        try:
            d = date.fromisoformat(ts[:10])
        except ValueError:
            log.debug("budget: невалиден timestamp в лог реда: %r", ts)
            continue
        if d < cutoff:
            continue
        totals["calls"] += 1
        totals["prompt_tokens"] += e.get("prompt_tokens", 0)
        totals["completion_tokens"] += e.get("completion_tokens", 0)
        totals["total_tokens"] += e.get("total_tokens", 0)
        prov = e.get("provider", "?")
        totals["by_provider"][prov]["calls"] += 1
        totals["by_provider"][prov]["total_tokens"] += e.get("total_tokens", 0)
    totals["by_provider"] = dict(totals["by_provider"])
    return totals


if __name__ == "__main__":
    print("=== Днес ===")
    print(json.dumps(today_totals(), indent=2, ensure_ascii=False))
    print("\n=== Последните 7 дни ===")
    print(json.dumps(range_totals(7), indent=2, ensure_ascii=False))
