"""Autonomous loop — Brain → Executor → self-correct → Skills Library (GENESIS DNA enforced)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from genesis_agent import dna
from genesis_agent.brain import Brain
from genesis_agent.skill_loader import SKILLS_ROOT
from genesis_agent.config import MAX_LLM_RETRIES, PROJECT_ROOT
from genesis_agent.executor import format_failure_for_brain, run_python_subprocess
from genesis_agent.skills_manager import save_skill, slugify
from genesis_agent.storage_monitor import check_storage, human_gb
from genesis_agent.local_repair_agent import emergency_repair
from genesis_agent.tool_schemas import MISSION_TOOLS


@dataclass
class LoopOutcome:
    success: bool
    rounds: int
    skill_path: str | None
    last_stdout: str
    last_stderr: str
    storage_note: str
    dna_audit: dict[str, object]
    reused_existing: bool = False


def run_autonomous_loop(
    goal: str,
    *,
    max_rounds: int | None = None,
    skill_slug: str | None = None,
    operator_id: str | None = None,
) -> LoopOutcome:
    """
    Публична обвивка: изпълнява мисията и известява резултата в Discord/Telegram
    (ако са конфигурирани). Известията никога не чупят цикъла.
    """
    outcome = _run_autonomous_loop_impl(
        goal, max_rounds=max_rounds, skill_slug=skill_slug, operator_id=operator_id
    )
    # Мета-обучение: запиши изхода, за да се учи от грешките си.
    try:
        from genesis_agent.reflection import record_mission
        record_mission(goal, outcome.success, outcome.last_stderr or "",
                        reused_existing=outcome.reused_existing)
    except Exception:
        pass
    try:
        from genesis_agent.notifier import notify
        if outcome.success:
            notify(f"✅ **Genesis** изпълни мисия за {outcome.rounds} рунда\n"
                   f"🎯 {goal[:200]}\n📦 {outcome.skill_path}")
        else:
            notify(f"❌ **Genesis** не успя с мисия след {outcome.rounds} рунда\n"
                   f"🎯 {goal[:200]}")
    except Exception:
        pass
    return outcome


def _run_autonomous_loop_impl(
    goal: str,
    *,
    max_rounds: int | None = None,
    skill_slug: str | None = None,
    operator_id: str | None = None,
) -> LoopOutcome:
    """
    Iterate: ask Brain for Python → execute in subprocess → on failure feed traceback back
    until success or max rounds. On success, register the script in the Skills Library.

    GENE-ETHICS: goal screened before any LLM call.
    GENE-AUTHORITY: optional GENESIS_STRICT_AUTHORITY requires an operator listed in GENESIS_OPERATOR.
    GENE-SECURITY: executor + skills gate Red Zone patterns unless elevation token is set.
    """
    report = check_storage()
    storage_note = (
        f"Storage {human_gb(report.total_bytes)} GB / threshold {human_gb(report.threshold_bytes)} GB"
        + (
            f" — COMPRESSION_REQUIRED (see {report.log_path})"
            if report.compression_required
            else " — OK"
        )
    )

    audit = dna.format_operator_audit(operator_id)
    try:
        dna.validate_goal_ethics(goal)
        dna.assert_operator_if_strict(operator_id)
    except dna.GenesisDNAError as e:
        return LoopOutcome(
            success=False,
            rounds=0,
            skill_path=None,
            last_stdout="",
            last_stderr=str(e),
            storage_note=storage_note,
            dna_audit=audit,
        )

    from genesis_agent.telemetry import report_thought
    
    report_thought(f"🚀 Инициирам мисия: {goal}")
    
    # min_size_b=120 (design note, 2026-07-25): умения се пазят в библиотеката трайно
    # — качеството тежи повече от скоростта тук. Локалният мозък остава последна
    # резерва независимо от размера (виж Brain.__init__).
    brain = Brain(min_size_b=120)
    brain.route_for_goal(goal)  # адаптивен избор на модел според сложността
    max_rounds = max_rounds or MAX_LLM_RETRIES
    _escalate_after = max(2, max_rounds // 3)  # след 1/3 неуспешни рундове → по-голям модел

    red_note = ""
    if dna.red_zone_elevation_granted():
        red_note = (
            "\n\n[SYSTEM] GENE-SECURITY: Red Zone elevation token is ACTIVE for this session. "
            "Registry/system code is still discouraged unless strictly necessary for the goal.\n"
        )

    # RAG: инжектираме релевантен контекст (подобни умения + минали уроци).
    rag_context = brain.build_context(goal)
    rag_block = f"\n\n## КОНТЕКСТ ОТ ПАМЕТТА\n{rag_context}\n" if rag_context else ""

    # Мета-обучение: дестилирани уроци от минали грешки → в system prompt-а.
    system_content = brain.system_prompt_base()
    try:
        from genesis_agent.reflection import lessons_for_prompt
        lessons = lessons_for_prompt()
        if lessons:
            system_content += "\n\n" + lessons
    except Exception:
        pass

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_content},
        {
            "role": "user",
            "content": f"High-level goal:\n{goal}{rag_block}\n\nIf you need external info FIRST, you may reply with ONLY a read-only tool tag ([WEB_SEARCH: query] for raw results, [RESEARCH: question] for a cross-verified grounded answer across multiple sources when accuracy matters, [READ_FILE: /path], [LIST_DIR: /path], or [LOOK_AT_SCREEN] / [LOOK_AT_SCREEN: question] to see the current screen if screen context would help) and I will return the result before you write code. A USE_SKILL tool is also available (native function-calling) — prefer calling an existing verified skill directly over reimplementing it from scratch when one already covers part of the goal. Otherwise implement as a single Python script. CRITICAL: You MUST include verification code at the bottom of the script (e.g. asserts or checks) that explicitly verifies the goal was achieved. If verification fails, raise an Exception.{red_note}",
        },
    ]

    # ─── АВАРИЕН РЕМОНТ: задейства ако имаме код от последен рунд но е бракувал ───
    last_generated_code = ""
    last_stdout = ""
    last_stderr = ""
    for round_i in range(max_rounds):
        try:
            from genesis_agent.config import stop_event
            if stop_event.is_set():
                print("\n[!] Изпълнението е ПРЕКЪСНАТО от потребителя (ПАУЗА)!")
                last_stderr = "Изпълнението е прекъснато от потребителя."
                break
        except ImportError:
            pass

        messages = Brain.trim_round_history(messages)
        reply = brain.complete(messages, tools=MISSION_TOOLS)

        # ─── NATIVE TOOL USE (design note, 2026-07-25, "мисии с реални умения") ───
        # Ако моделът поддържа function-calling (config.yaml supports_tools),
        # tool_calls е структуриран — извикваме СЪЩИЯ backend като терминалния
        # чат (genesis_skills.dispatch_tool_call), най-важно USE_SKILL: Brain-ът
        # вече може РЕАЛНО да изпълни съществуващо умение, не само да получи
        # кода му инжектиран в промпта (build_context композицията по-горе).
        # Verifier-ът/критикът никога не виждат тези рундове — continue-ваме
        # обратно към върха на цикъла ПРЕДИ да стигнем до код-екстракция.
        if reply.tool_calls:
            report_thought("🔧 Brain вика инструмент (native)...")
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

        # ─── TOOL USE ПО ВРЕМЕ НА МИСИЯ (стар text-tag режим, само read-only) ───
        # За модели БЕЗ native function-calling (fallback опашката в Brain) —
        # ако поискат информация (WEB_SEARCH/READ_FILE/LIST_DIR) вместо код,
        # изпълняваме я и я връщаме, за да напише кода информирано. Не е провал.
        if not reply.code and any(t in reply.raw_text for t in ("[WEB_SEARCH:", "[RESEARCH:", "[READ_FILE:", "[LIST_DIR:", "[LOOK_AT_SCREEN")):
            try:
                import sys as _sys
                _sys.path.insert(0, str(PROJECT_ROOT))
                import genesis_skills
                tool_results = genesis_skills.parse_and_execute_readonly_tools(reply.raw_text)
            except Exception as _e:
                tool_results = [f"[tool грешка: {_e}]"]
            if tool_results:
                report_thought("🔎 Brain ползва инструмент за информация...")
                messages.append({"role": "assistant", "content": reply.raw_text})
                messages.append({"role": "user", "content":
                    "Резултат от инструментите:\n" + "\n\n".join(tool_results)[:4000] +
                    "\n\nСега напиши финалния Python скрипт със self-test, който печата OK."})
                continue

        if reply.code:
            last_generated_code = reply.code  # Запазва последния генериран код

        if not reply.code:
            raw = str(reply.raw_text)
            if raw.startswith("Error:"):
                import time
                print(f"\n[КРИТИЧНА ГРЕШКА] Сървърът върна: {raw}")
                # Спира веднага - няма смисъл да въртим 8 рунда при грешка на връзка
                print("[!] Прекратявам опитите. Провери модела и повтори (/модел)")
                break

            messages.append({"role": "assistant", "content": reply.raw_text})
            messages.append(
                {
                    "role": "user",
                    "content": "No ```python``` block found. Respond with exactly one ```python ... ``` fence containing the full script.",
                }
            )
            continue

        
        result = run_python_subprocess(reply.code)
        last_stdout = result.stdout
        last_stderr = result.stderr

        if result.ok:
            # ─── ТЕСТ-ГЕЙТ (реална проверка, не само мнение на LLM) ───
            from genesis_agent.verifier import verify_skill
            vres = verify_skill(reply.code)
            if vres.method != "self_test_passed":
                report_thought(f"🧪 Тест-гейт отхвърли: няма преминаващ self-test ({vres.method})")
                messages.append({"role": "assistant", "content": reply.raw_text})
                messages.append({
                    "role": "user",
                    "content": (
                        "The code ran but has NO passing self-test. Add assert-based checks at "
                        "the bottom that verify the goal was actually achieved and print 'OK' on "
                        f"success, then return the FULL corrected script. (verifier: {vres.method})"
                    ),
                })
                continue

            # ─── КРИТИК (семантично второ мнение) ───
            critic_prompt = (
                f"Goal: {goal}\n\n"
                f"Code Output:\n{last_stdout}\n\n"
                f"Code:\n{reply.code}\n\n"
                "Did the code FULLY accomplish the specific goal? For example, if it was asked to save to a file, does the code actually write to a file?\n"
                "If YES, reply exactly 'YES'.\n"
                "If NO (it missed a requirement or just printed instead of saving), reply 'NO: <reason>'. Do not write code."
            )
            critic_msg = [
                {"role": "system", "content": "You are a strict code reviewer. You ONLY reply with YES or NO: <reason>."},
                {"role": "user", "content": critic_prompt}
            ]
            critic_eval = brain.complete(critic_msg).raw_text.strip()

            if critic_eval.upper().startswith("NO"):
                report_thought(f"🔍 Критикът отхвърли резултата: {critic_eval}")
                messages.append({"role": "assistant", "content": reply.raw_text})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Code ran and self-test passed, but a reviewer says it does not meet the "
                        f"goal: {critic_eval}. Fix and return the FULL corrected script."
                    ),
                })
                continue

            report_thought("✅ Тест-гейт + критик одобриха резултата.")
            slug = skill_slug or slugify(goal)
            ex: dict[str, Any] = {"rounds": round_i + 1, "test_gated": True}
            ex.update(audit)
            try:
                path = save_skill(
                    slug=slug,
                    code=reply.code,
                    goal=goal,
                    verification_stdout=result.stdout,
                    extra=ex,
                )
            except dna.GenesisDNAError as e:
                last_stderr = str(e)
                messages.append({"role": "assistant", "content": reply.raw_text})
                messages.append(
                    {
                        "role": "user",
                        "content": "Skills Library rejected the script under GENESIS DNA (ethics/red zone). "
                        "Rewrite to comply: no harm to humans, no registry/system Red Zone without approval token, "
                        "single ```python``` block.\n\n"
                        + str(e),
                    }
                )
                continue
            rel = str(path.relative_to(SKILLS_ROOT)).replace("\\", "/")
            try:
                from genesis_agent.reflection import detect_reuse
                reused = detect_reuse(rag_context, reply.code)
            except Exception:
                reused = False
            return LoopOutcome(
                success=True,
                rounds=round_i + 1,
                skill_path=rel,
                last_stdout=last_stdout,
                last_stderr=last_stderr,
                storage_note=storage_note,
                dna_audit=audit,
                reused_existing=reused,
            )

        # ЕСКАЛАЦИЯ: ако малкият модел се мъчи, качи на по-голям (3b→7b→14b).
        if round_i + 1 == _escalate_after:
            brain.escalate()

        # BROADCAST THOUGHT: FAILURE / SELF-CORRECT
        report_thought(f"❌ Грешка при изпълнението. Анализирам проблема и започвам самокорекция...")
        
        messages.append({"role": "assistant", "content": reply.raw_text})
        messages.append(
            {
                "role": "user",
                "content": "The code failed when executed. Fix ALL issues and return the complete corrected script in one ```python``` block.\n\n"
                + format_failure_for_brain(result),
            }
        )

    try:
        from genesis_agent.config import stop_event
        is_stopped = stop_event.is_set()
    except ImportError:
        is_stopped = False

    if last_generated_code and last_stderr and not is_stopped:
        print("\n" + "\u2550" * 55)
        print("  [\u26a0\ufe0f  \u0410\u0412\u0410\u0420\u0418\u0415\u041d \u0420\u0415\u041c\u041e\u041d\u0422] Brain \u0435 \u043d\u0435\u0434\u043e\u0441\u0442\u044a\u043f\u0435\u043d. \u0410\u043a\u0442\u0438\u0432\u0438\u0440\u0430\u043c LocalRepairAgent...")
        print("  [\u041c\u0410\u041b\u042a\u041a \u041c\u041e\u0414\u0415\u041b] \u041f\u0430\u0442\u0435\u0440\u043d \u0430\u043d\u0430\u043b\u0438\u0437 + 1-3B \u043c\u043e\u0434\u0435\u043b")
        print("\u2550" * 55)

        repair = emergency_repair(last_generated_code, last_stderr, last_stdout)

        if repair.fixed:
            print(f"\n  [\u2705 \u0410\u0412\u0410\u0420\u0418\u0415\u041d \u0420\u0415\u041c\u041e\u041d\u0422 \u0423\u0421\u041f\u0415\u0428\u0415\u041d] {repair.fix_desc}")
            print(f"  Метод: {repair.method} | Рундове: {repair.rounds}")

            slug = (skill_slug or slugify(goal)) + "_repaired"
            try:
                path = save_skill(
                    slug=slug,
                    code=repair.code,
                    goal=goal + " [repaired by LocalRepairAgent]",
                    verification_stdout="",
                    extra={"repair_method": repair.method,
                           "repair_rounds": repair.rounds,
                           "operator": operator_id or "operator"}
                )
                rel = str(path.relative_to(SKILLS_ROOT)).replace("\\", "/")
                return LoopOutcome(
                    success=True,
                    rounds=max_rounds + repair.rounds,
                    skill_path=rel,
                    last_stdout=last_stdout,
                    last_stderr=last_stderr,
                    storage_note=storage_note,
                    dna_audit=audit,
                )
            except Exception as save_err:
                print(f"  [\u0420\u0415\u041c\u041e\u041d\u0422] \u0413\u0440\u0435\u0448\u043a\u0430 \u043f\u0440\u0438 \u0437\u0430\u043f\u0430\u0437\u0432\u0430\u043d\u0435: {save_err}")
        else:
            print(f"  [\u0420\u0415\u041c\u041e\u041d\u0422 \u041d\u0415\u0423\u0421\u041f\u0415\u0428\u0415\u041d] \u041d\u0438\u0442\u043e pattern fixes, \u043d\u0438\u0442\u043e LLM \u043d\u0435 \u043f\u043e\u043c\u043e\u0433\u043d\u0430\u0445а.")

    return LoopOutcome(
        success=False,
        rounds=max_rounds,
        skill_path=None,
        last_stdout=last_stdout,
        last_stderr=last_stderr,
        storage_note=storage_note,
        dna_audit=audit,
    )
