#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genesis_agent.parallel_forge — мащабно паралелно производство на умения (Scale-2).

Вместо една мисия наведнъж, тук се изпълняват МНОГО мисии едновременно, всяка
закована към РАЗЛИЧЕН доставчик (виж PROVIDERS_CYCLE по-долу — отделни квоти)
→ няколко пъти по-голяма пропускливост за същото време.

Записът на умения е thread-safe (lock в skills_manager), затова паралелните
worker-и не си повреждат skills.json.

Употреба:
    from genesis_agent.parallel_forge import forge
    results = forge(goals, workers=4)

CLI:
    python3 -m genesis_agent.parallel_forge          # взима цели от goal_engine
    python3 -m genesis_agent.parallel_forge --n 12    # 12 цели, 4 паралелни
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from genesis_agent.orchestrator import run_orchestrated
from genesis_agent.skills_manager import slugify
from genesis_agent.skill_loader import load_skills_index

# Providers with SEPARATE quotas — workers are spread across them so parallel
# missions do not all queue behind one account's rate limit.
# `orchestrator.run_orchestrated` runs with min_size_b=120, and Cohere/NVIDIA
# have no model that large (111B/47B at most), so they are excluded here — a
# worker pinned to them would get an empty chain and fail forever.
PROVIDERS_CYCLE = ["huggingface", "openrouter", "ollama_cloud"]


def _available_providers_cycle() -> list[str]:
    """
    Only the providers that actually have a key configured right now.

    Read live rather than hardcoded, so adding or removing a key from
    ~/.genesis/.env takes effect without editing code. One key per provider —
    parallelism comes from using several providers at once, each within its
    own limits, not from stacking accounts on one.
    """
    from genesis_agent.brain import Brain, _PROVIDERS
    try:
        b = Brain(use_local=False)
        available = []
        for prov in PROVIDERS_CYCLE:
            _, key_env = _PROVIDERS.get(prov, (None, None))
            if not key_env or b._provider_key(key_env):
                available.append(prov)
        return available or list(PROVIDERS_CYCLE)
    except Exception:
        return list(PROVIDERS_CYCLE)


@dataclass
class ForgeResult:
    goal: str
    provider: str
    success: bool
    rounds: int
    skill_path: str = ""
    seconds: float = 0.0


def _one(goal: str, provider: str) -> ForgeResult:
    t0 = time.time()
    try:
        out = run_orchestrated(goal, prefer_provider=provider, max_rounds=4)
        return ForgeResult(goal, provider, out.success, out.rounds,
                           out.skill_path, round(time.time() - t0, 1))
    except Exception as e:
        return ForgeResult(goal, provider, False, 0, f"err:{e}", round(time.time() - t0, 1))


def forge(goals: list[str], *, workers: int | None = None, notify_discord: bool = True) -> list[ForgeResult]:
    """Изпълнява целите паралелно, всяка на ротиращ доставчик (претеглен по
    реален брой ключове — design note, 2026-07-25). workers=None → авто, колкото
    общо ключове има в PROVIDERS_CYCLE (максимална легитимна паралелност)."""
    cycle = _available_providers_cycle()
    if workers is None:
        workers = len(cycle)
    # Прескачаме вече съществуващи умения — но САМО ако slug-ът принадлежи на
    # ТАЗИ СЪЩА цел. slugify() реже на 48 символа, затова проверка само по
    # файлово съществуване лъжливо "намираше" различна цел с общ дълъг префикс
    # за вече завършена и я прескачаше тихо, без никакъв резултат/грешка (реален
    # случай, хванат на живо 2026-07-26: "...converts an integer..." vs
    # "...converts a string to snake_case..." → еднакъв 48-символен префикс).
    # Проверяваме по description в индекса, не само по име на файл.
    index = load_skills_index()
    todo = [g for g in goals if index.get(slugify(g), {}).get("description") != g]
    print(f"=== ПАРАЛЕЛНА КОВАЧНИЦА: {len(todo)} цели, {workers} едновременно "
          f"(доставчици претеглени по ключове: {', '.join(sorted(set(cycle)))}) ===")
    results: list[ForgeResult] = []
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_one, g, cycle[i % len(cycle)]): g
            for i, g in enumerate(todo)
        }
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            icon = "✅" if r.success else "❌"
            print(f"  {icon} [{r.provider:11}] {r.seconds:5}s  {r.goal[:50]}")

    ok = sum(1 for r in results if r.success)
    elapsed = round(time.time() - t0)
    print(f"\n  ГОТОВО: {ok}/{len(results)} успешни за {elapsed}s "
          f"(≈{round(elapsed/max(1,len(results)),1)}s/цел паралелно)")

    if notify_discord:
        try:
            from genesis_agent.notifier import notify
            by_prov: dict[str, int] = {}
            for r in results:
                if r.success:
                    by_prov[r.provider] = by_prov.get(r.provider, 0) + 1
            lines = [f"⚡ **Паралелна ковачница** — {ok}/{len(results)} умения за {elapsed}с",
                     "по доставчик: " + ", ".join(f"{p}:{n}" for p, n in by_prov.items())]
            notify("\n".join(lines))
        except Exception:
            pass
    return results


def main() -> None:
    n = 16  # покрива авто worker-и (сега ~8, по ключове) с резерва за опашка
    if "--n" in sys.argv:
        try:
            n = int(sys.argv[sys.argv.index("--n") + 1])
        except (ValueError, IndexError):
            pass
    workers = None  # None → авто, колкото общо ключове (_available_providers_cycle)
    if "--workers" in sys.argv:
        try:
            workers = int(sys.argv[sys.argv.index("--workers") + 1])
        except (ValueError, IndexError):
            pass
    from genesis_agent.goal_engine import next_goals
    goals = next_goals(n)
    forge(goals, workers=workers)


if __name__ == "__main__":
    main()
