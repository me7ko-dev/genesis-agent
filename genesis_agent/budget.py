#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genesis_agent.budget — token/call observability слой.

Единствената обща дупка във всичко построено в тази сесия (мисии, ensemble,
self-modify, Discord чат, 24/7 цикъл): всички минават през Brain.complete(),
но никой досега не четеше 'usage' полето от API отговора. Този модул го
пази — просто JSONL лог, никакви external dependencies, никога не хвърля.

Употреба:
    from genesis_agent.budget import record_usage, today_totals, daily_totals
    record_usage(provider="huggingface", model="Qwen2.5-Coder-32B-Instruct",
                 prompt_tokens=120, completion_tokens=340)
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from genesis_agent.config import PROJECT_ROOT

LOG_PATH = PROJECT_ROOT / "budget_log.jsonl"


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
                except Exception:
                    continue
    except Exception:
        return


def daily_totals(day: date | None = None) -> dict:
    """Обобщение за конкретен ден (по подразбиране днес, UTC — записите се
    пазят с UTC timestamp, сравнение с локална дата дава грешен резултат
    точно около границата на деня): calls/tokens общо + разбивка по
    доставчик. Безопасно — при повреден/липсващ лог връща нули."""
    day = day or datetime.now(timezone.utc).date()
    day_str = day.isoformat()
    totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
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
    totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
              "by_provider": defaultdict(lambda: {"calls": 0, "total_tokens": 0})}
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    for e in _read_entries():
        ts = e.get("ts", "")
        try:
            d = date.fromisoformat(ts[:10])
        except Exception:
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
