#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genesis_agent.orchestrator — мулти-агентна оркестрация (L2-1).

Вместо един монолитен цикъл, задачата минава през РОЛИ, всяка отделен разговор
с мозъка (Brain, който е локален-пръв + облачен fallback):

    Planner  → разбива целта на кратък план/спецификация.
    Coder    → пише кода по плана (с RAG контекст + уроци от грешки).
    Tester   → изпълнява в sandbox + verifier (реален self-test, не мнение).
    Reviewer → при провал дава КОНКРЕТНИ инструкции за поправка → обратно към Coder.

При успех умението се записва (verified). Всичко изпълнимо минава през sandbox-а.

Употреба:
    from genesis_agent.orchestrator import run_orchestrated
    outcome = run_orchestrated("Implement an LRU cache with a self-test that prints OK")
"""
from __future__ import annotations

from dataclasses import dataclass

from genesis_agent.brain import Brain
from genesis_agent.executor import run_python_subprocess
from genesis_agent.verifier import verify_skill
from genesis_agent.skills_manager import save_skill, slugify
from genesis_agent.skill_loader import SKILLS_ROOT
from genesis_agent.config import PROJECT_ROOT, MAX_LLM_RETRIES
from genesis_agent import dna
from genesis_agent.tool_schemas import MISSION_TOOLS


@dataclass
class OrchestratedOutcome:
    success: bool
    rounds: int
    skill_path: str = ""
    plan: str = ""
    last_error: str = ""


_PLANNER_SYS = (
    "Ти си Planner агент на Genesis. По дадена цел върни КРАТЪК номериран план (3-6 стъпки) "
    "за един Python скрипт, който я решава и има assert self-test, печатащ 'OK'. "
    "Само планът — БЕЗ код."
)
_REVIEWER_SYS = (
    "Ти си строг Reviewer агент. По дадени цел, код и грешка върни КОНКРЕТНИ инструкции "
    "какво да се промени, за да мине. Кратко, на точки. БЕЗ код."
)
_TESTER_SYS = (
    "Ти си Tester агент (TDD). По дадена цел върни САМО списък от assert-тестове на Python, "
    "които проверяват решението (напр. assert func(вход)==очаквано). Използвай името, което "
    "целта предполага за функцията. Върни ги в един ```python``` блок. БЕЗ имплементация."
)


def _write_tests_first(brain: Brain, goal: str) -> str:
    """TDD: Tester агентът пише assert-тестове ПРЕДИ кода."""
    reply = brain.complete([
        {"role": "system", "content": _TESTER_SYS},
        {"role": "user", "content": f"Цел:\n{goal}"},
    ])
    tests = reply.code or _extract_code(reply.raw_text or "")
    return tests.strip()


def _extract_code(raw: str) -> str:
    if "```python" in raw:
        return raw.split("```python")[1].split("```")[0].strip()
    if raw.lstrip().startswith(("def ", "import ", "from ", "class ")):
        return raw
    return ""


def run_orchestrated(goal: str, *, max_rounds: int | None = None,
                     operator_id: str | None = None, tdd: bool | None = None,
                     prefer_provider: str | None = None) -> OrchestratedOutcome:
    import os as _os
    max_rounds = max_rounds or MAX_LLM_RETRIES
    if tdd is None:
        tdd = _os.environ.get("GENESIS_TDD") == "1"
    # min_size_b=120 (design note, 2026-07-25): умения (вкл. през forge/ensemble
    # pinned worker-и) искат само големи модели — виж config.yaml size_b.
    brain = Brain(prefer_provider=prefer_provider, min_size_b=120)
    if not prefer_provider:
        brain.route_for_goal(goal)  # адаптивен избор на модел според сложността

    try:
        dna.validate_goal_ethics(goal)
        dna.assert_operator_if_strict(operator_id)
    except dna.GenesisDNAError as e:
        return OrchestratedOutcome(success=False, rounds=0, last_error=str(e))

    # 1. PLANNER — кратка спецификация.
    plan_reply = brain.complete([
        {"role": "system", "content": _PLANNER_SYS},
        {"role": "user", "content": f"Цел:\n{goal}"},
    ])
    plan = (plan_reply.raw_text or "").strip()[:1500]

    # 1b. TDD: Tester пише тестовете ПРЕДИ кода.
    tests_block = ""
    if tdd:
        tests = _write_tests_first(brain, goal)
        if tests:
            tests_block = ("\n\nЗАДЪЛЖИТЕЛНИ ТЕСТОВЕ (кодът ти ТРЯБВА да ги минава — вгради ги "
                           f"най-долу и добави print('OK')):\n{tests}")

    # RAG контекст + уроци за Coder-а.
    rag = brain.build_context(goal)
    try:
        from genesis_agent.reflection import lessons_for_prompt
        lessons = lessons_for_prompt()
    except Exception:
        lessons = ""

    coder_sys = brain.system_prompt_base()
    if lessons:
        coder_sys += "\n\n" + lessons

    messages = [
        {"role": "system", "content": coder_sys},
        {"role": "user", "content":
            f"Цел:\n{goal}\n\nПлан:\n{plan}"
            + (f"\n\n## КОНТЕКСТ\n{rag}" if rag else "")
            + tests_block
            + "\n\nA USE_SKILL tool is also available (native function-calling) — prefer "
              "calling an existing verified skill directly over reimplementing it when one "
              "already covers part of the goal."
            + "\n\nНапиши един Python скрипт по плана, с assert self-test, който печата 'OK'."},
    ]

    last_error = ""
    for round_i in range(max_rounds):
        # 2. CODER
        messages = Brain.trim_round_history(messages)
        reply = brain.complete(messages, tools=MISSION_TOOLS)

        # NATIVE TOOL USE (design note, 2026-07-25, "мисиите с реални умения" —
        # разширено и към оркестратора): същият pattern като autonomous_loop.py.
        if reply.tool_calls:
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
                    tool_out = genesis_skills.dispatch_tool_call(name, args)
                    messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                      "name": name, "content": tool_out[:4000]})
            except Exception as _e:
                messages.append({"role": "tool", "tool_call_id": "error",
                                  "name": "error", "content": f"[tool грешка: {_e}]"})
            continue

        code = reply.code or _extract_code(reply.raw_text or "")
        if not code:
            if str(reply.raw_text).startswith("Error:"):
                return OrchestratedOutcome(False, round_i + 1, plan=plan, last_error=reply.raw_text)
            messages.append({"role": "assistant", "content": reply.raw_text})
            messages.append({"role": "user", "content": "Върни само един ```python``` блок с кода."})
            continue

        # 3. TESTER — реално изпълнение + verifier.
        result = run_python_subprocess(code)
        vres = verify_skill(code)

        if result.ok and vres.method == "self_test_passed":
            slug = slugify(goal)
            try:
                path = save_skill(slug=slug, code=code, goal=goal,
                                  verification_stdout=result.stdout,
                                  extra={"orchestrated": True, "rounds": round_i + 1,
                                         "plan": plan[:500]})
            except dna.GenesisDNAError as e:
                last_error = str(e)
                messages.append({"role": "assistant", "content": reply.raw_text})
                messages.append({"role": "user", "content": f"DNA отказа: {e}. Пренапиши."})
                continue
            rel = str(path.relative_to(SKILLS_ROOT)).replace("\\", "/")
            return OrchestratedOutcome(True, round_i + 1, skill_path=rel, plan=plan)

        # 4. REVIEWER — конкретни инструкции за поправка.
        err = result.stderr or f"verifier: {vres.method} — {vres.detail}"
        last_error = err[:300]
        review = brain.complete([
            {"role": "system", "content": _REVIEWER_SYS},
            {"role": "user", "content": f"Цел:\n{goal}\n\nКод:\n{code[:2000]}\n\nГрешка:\n{err[:600]}"},
        ])
        fix = (review.raw_text or "").strip()[:800]
        messages.append({"role": "assistant", "content": reply.raw_text})
        messages.append({"role": "user", "content":
            f"Кодът не мина. Reviewer казва:\n{fix}\n\nПоправи и върни ПЪЛНИЯ коригиран скрипт "
            "с assert self-test, който печата 'OK'."})

    return OrchestratedOutcome(False, max_rounds, plan=plan, last_error=last_error)


if __name__ == "__main__":
    out = run_orchestrated("Implement a function that returns the nth Fibonacci number, "
                           "with an assert self-test that prints OK.")
    print("успех:", out.success, "| рундове:", out.rounds)
    print("план:\n", out.plan[:300])
    if out.success:
        print("умение:", out.skill_path)
    else:
        print("грешка:", out.last_error)
