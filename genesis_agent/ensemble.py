#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genesis_agent.ensemble — Ensemble качество (Scale-4): ЕДНА цел, НЯКОЛКО модела
паралелно, пази се само НАЙ-ДОБРИЯТ verified резултат.

Разлика с parallel_forge (Scale-2): parallel_forge пуска МНОГО РАЗЛИЧНИ цели
едновременно, всяка на свой доставчик — throughput. Ensemble пуска ЕДНАТА И
СЪЩА цел през няколко доставчика едновременно — качество/надеждност: ако един
модел се провали или изпише грешен код, друг може да успее; ако няколко
успеят, печели critic-одобреният с най-кратък (най-чист) код.

Единичен опит на доставчик (без self-repair рундове) — ширината на ансамбъла
Е retry механизмът, вместо последователни рундове на един модел.

Употреба:
    from genesis_agent.ensemble import run_ensemble
    result = run_ensemble("напиши функция за...")

CLI:
    python3 -m genesis_agent.ensemble "цел тук"
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from genesis_agent import dna
from genesis_agent.brain import Brain
from genesis_agent.config import PROJECT_ROOT
from genesis_agent.executor import run_python_subprocess
from genesis_agent.skills_manager import save_skill, slugify
from genesis_agent.verifier import verify_skill
from genesis_agent.tool_schemas import MISSION_TOOLS

# Ensemble е single-shot по дизайн (ширината на ансамбъла Е retry
# механизмът — виж модулния docstring) — но native USE_SKILL резолюция
# ПРЕДИ финалния код не чупи това, само отговаря на "с какво разполагам"
# преди единствения реален опит за код. Малък таван, не пълноценен
# self-repair цикъл (design note, 2026-07-25, "мисиите с реални умения").
_TOOL_ROUND_CAP = 3

# Доставчици с ОТДЕЛНИ квоти — гласуват паралелно за същата цел.
# min_size_b=120 (design note, 2026-07-25) стеснява всеки pinned worker до само
# неговите ≥120B модели — Cohere/NVIDIA нямат нито един такъв (max 111B/47B),
# затова отпадат от цикъла тук (иначе worker-ът би останал с празна верига).
PROVIDERS_CYCLE = ["huggingface", "openrouter", "ollama_cloud"]


@dataclass
class Candidate:
    provider: str
    code: str = ""
    verified: bool = False
    method: str = ""
    stdout: str = ""
    critic_ok: bool = False
    seconds: float = 0.0
    error: str = ""


@dataclass
class EnsembleResult:
    goal: str
    success: bool
    winner_provider: str = ""
    skill_path: str = ""
    reused_existing: bool = False
    candidates: list[Candidate] = field(default_factory=list)


def _generate_one(goal: str, provider: str, rag_context: str, system_content: str) -> Candidate:
    """Единичен опит: генерира → изпълнява в sandbox → test-gate → критик."""
    t0 = time.time()
    cand = Candidate(provider=provider)
    try:
        brain = Brain(prefer_provider=provider, min_size_b=120)
        rag_block = f"\n\n## КОНТЕКСТ ОТ ПАМЕТТА\n{rag_context}\n" if rag_context else ""
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": (
                f"High-level goal:\n{goal}{rag_block}\n\n"
                "A USE_SKILL tool is also available (native function-calling) — prefer "
                "calling an existing verified skill directly over reimplementing it when "
                "one already covers part of the goal.\n\n"
                "Implement as a single Python script. CRITICAL: include verification code "
                "at the bottom (asserts) that explicitly verifies the goal was achieved, "
                "and print 'OK' on success."
            )},
        ]
        reply = brain.complete(messages, tools=MISSION_TOOLS)

        # Ограничена native tool резолюция ПРЕДИ единствения реален опит за
        # код (не self-repair цикъл — виж _TOOL_ROUND_CAP по-горе).
        tool_round = 0
        while reply.tool_calls and tool_round < _TOOL_ROUND_CAP:
            messages.append({"role": "assistant", "content": reply.raw_text or "",
                              "tool_calls": reply.tool_calls})
            try:
                import sys as _sys
                _sys.path.insert(0, str(PROJECT_ROOT))
                import genesis_skills
                import json as _json
                for tc in reply.tool_calls:
                    fn = tc.get("function", {}) or {}
                    name = fn.get("name", "")
                    try:
                        args = _json.loads(fn.get("arguments") or "{}")
                    except (_json.JSONDecodeError, TypeError):
                        args = {}
                    result = genesis_skills.dispatch_tool_call(name, args)
                    messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                      "name": name, "content": result[:4000]})
            except Exception as _e:
                messages.append({"role": "tool", "tool_call_id": "error",
                                  "name": "error", "content": f"[tool грешка: {_e}]"})
            tool_round += 1
            reply = brain.complete(messages, tools=MISSION_TOOLS)

        if not reply.code:
            cand.error = f"без код блок: {(reply.raw_text or '')[:150]}"
            return cand
        cand.code = reply.code

        result = run_python_subprocess(reply.code)
        cand.stdout = result.stdout
        if not result.ok:
            cand.error = (result.stderr or "")[:300]
            return cand

        vres = verify_skill(reply.code)
        cand.method = vres.method
        cand.verified = vres.method == "self_test_passed"
        if not cand.verified:
            cand.error = f"без преминаващ self-test ({vres.method})"
            return cand

        critic_msg = [
            {"role": "system", "content": "You are a strict code reviewer. You ONLY reply with YES or NO: <reason>."},
            {"role": "user", "content": (
                f"Goal: {goal}\n\nCode Output:\n{cand.stdout}\n\nCode:\n{cand.code}\n\n"
                "Did the code FULLY accomplish the goal? Reply exactly 'YES' or 'NO: <reason>'."
            )},
        ]
        critic_eval = brain.complete(critic_msg).raw_text.strip()
        cand.critic_ok = critic_eval.upper().startswith("YES")
    except Exception as e:
        cand.error = str(e)
    finally:
        cand.seconds = round(time.time() - t0, 1)
    return cand


def run_ensemble(
    goal: str,
    *,
    providers: list[str] | None = None,
    skill_slug: str | None = None,
) -> EnsembleResult:
    """
    Генерира решения за ЕДНА цел паралелно през няколко доставчика, пази само
    най-добрия verified резултат. Победител: critic-одобрените първо, после
    най-кратък код (proxy за чистота) сред тях.
    """
    providers = providers or PROVIDERS_CYCLE
    brain0 = Brain()
    rag_context = brain0.build_context(goal)
    system_content = brain0.system_prompt_base()
    try:
        from genesis_agent.reflection import lessons_for_prompt
        lessons = lessons_for_prompt()
        if lessons:
            system_content += "\n\n" + lessons
    except Exception:
        pass

    candidates: list[Candidate] = []
    with ThreadPoolExecutor(max_workers=len(providers)) as ex:
        futs = {ex.submit(_generate_one, goal, p, rag_context, system_content): p for p in providers}
        for fut in as_completed(futs):
            candidates.append(fut.result())

    verified = [c for c in candidates if c.verified]
    if not verified:
        return EnsembleResult(goal=goal, success=False, candidates=candidates)

    verified.sort(key=lambda c: (not c.critic_ok, len(c.code)))
    winner = verified[0]

    slug = skill_slug or slugify(goal)
    try:
        path = save_skill(
            slug=slug, code=winner.code, goal=goal,
            verification_stdout=winner.stdout,
            extra={
                "ensemble": True,
                "ensemble_providers": [c.provider for c in candidates],
                "ensemble_verified_count": len(verified),
                "winner_provider": winner.provider,
                "winner_critic_ok": winner.critic_ok,
                "test_gated": True,
            },
        )
    except dna.GenesisDNAError:
        return EnsembleResult(goal=goal, success=False, candidates=candidates)

    try:
        from genesis_agent.reflection import detect_reuse, record_mission
        reused = detect_reuse(rag_context, winner.code)
        record_mission(goal, True, "", reused_existing=reused)
    except Exception:
        reused = False

    rel = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    return EnsembleResult(
        goal=goal, success=True, winner_provider=winner.provider,
        skill_path=rel, reused_existing=reused, candidates=candidates,
    )


def main() -> None:
    if len(sys.argv) < 2:
        print("Употреба: python3 -m genesis_agent.ensemble \"цел тук\"")
        sys.exit(1)
    goal = " ".join(sys.argv[1:])
    print(f"=== ENSEMBLE: {goal} ===")
    res = run_ensemble(goal)
    for c in sorted(res.candidates, key=lambda c: c.provider):
        icon = "✅" if c.verified else "❌"
        tag = " 🏆" if res.success and c.provider == res.winner_provider else ""
        critic = " critic:YES" if c.critic_ok else ""
        print(f"  {icon} [{c.provider:11}] {c.seconds:5}s{critic}{tag}  {c.error[:80]}")
    if res.success:
        print(f"\n  ПОБЕДИТЕЛ: {res.winner_provider} → {res.skill_path}"
              f" ({sum(1 for c in res.candidates if c.verified)}/{len(res.candidates)} verified)")
    else:
        print("\n  ❌ Нито един доставчик не успя.")


if __name__ == "__main__":
    main()
