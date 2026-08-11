"""Autonomous loop — Brain → Executor → self-correct → Skills Library (GENESIS DNA enforced)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from genesis_agent import dna
from genesis_agent.brain import Brain
from genesis_agent.config import MAX_LLM_RETRIES, PROJECT_ROOT
from genesis_agent.executor import format_failure_for_brain, run_python_subprocess
from genesis_agent.local_repair_agent import emergency_repair
from genesis_agent.skill_loader import SKILLS_ROOT
from genesis_agent.skills_manager import save_skill, slugify
from genesis_agent.storage_monitor import check_storage, human_gb
from genesis_agent import provider_stats
from genesis_agent.tool_schemas import MISSION_TOOLS

# Колко ДОПЪЛНИТЕЛНИ кандидата (отвъд първия опит) да генерираме от локалния
# tier, когато той не излезе перфектен от първия път (design note, 2026-08-11).
# Локалният inference е безплатен — цената е само латентност, затова малко,
# но ненулево число: достатъчно за реален "best of N" ефект без да удвоим-
# утроим латентността на всеки рунд без нужда.
LOCAL_EXTRA_CANDIDATES = 2


def _score_local_candidate(code: str) -> tuple[int, str]:
    """Резултат за РЕЙТИНГ на кандидат от локалния candidate pool: 2 =
    self_test_passed, 1 = runs_clean, 0 = lint/execution провал (по-високо е
    по-добре). Умишлено дублира изпълнението, което основният pipeline прави
    веднага след избора на победител — приемлив компромис: локалният sandbox
    run е евтин/безплатен, простотата/коректността тежат повече от микро-
    оптимизацията да се избегне повторно изпълнение. Никога не хвърля."""
    try:
        from genesis_agent.code_validate import validate_code_with_ruff
        lint_ok, lint_detail = validate_code_with_ruff(code)
        if not lint_ok:
            return (0, code)
        if lint_detail:
            code = lint_detail
        result = run_python_subprocess(code)
        if not result.ok:
            return (0, code)
        from genesis_agent.verifier import verify_skill
        vres = verify_skill(code)
        score = 2 if vres.method == "self_test_passed" else (1 if vres.verified else 0)
        return (score, code)
    except Exception:
        return (0, code)


def _note_quality_failure(brain: Brain, count: int, threshold: int) -> int:
    """Verifier/критик отхвърлиха резултата — различно от HTTP/execution грешка,
    но досега невидимо за provider_stats (само `success=False` на мрежово ниво
    се записваше, `brain.py` reда 1026/1042/1047). HTTP 200 с боклук код се
    броеше за "успех" завинаги. Записваме провала на КАЧЕСТВОТО срещу текущия
    доставчик (deprioritize_flaky вижда го при следваща мисия) и, при
    достигнат праг, качваме към по-силната безплатна кодинг верига веднага —
    не чакаме операторът да сложи GENESIS_QUALITY=coding ръчно."""
    count += 1
    current = getattr(brain, "current", None)
    prov = current.get("provider") if isinstance(current, dict) else None
    if prov:
        try:
            provider_stats.record_call(prov, 0.0, False)
        except Exception:
            pass
    if count >= threshold:
        try:
            brain.escalate_to_coding_chain()
        except AttributeError:
            pass
    return count


def _research_for_weak_model(goal: str) -> str:
    """Grounded уеб проучване по целта — инжектирано в контекста ПРЕДИ слабият
    (локален 3B/7B/14B) модел да напише и ред код, вместо да разчита само на
    собствената си, по-плитка памет (design note, 2026-08-11: "прочети въпроса,
    намери решение в интернет, после пиши код" специално за слабия tier —
    облачните модели са достатъчно силни и без това). Ползва
    genesis_agent.research.grounded_research (cross-verified през няколко
    източника, не суров първи snippet). Безопасно (никога не хвърля) — липса
    на резултат просто значи "продължи без проучване", не спира мисията."""
    try:
        from genesis_agent.research import grounded_research
        note = (grounded_research(goal) or "").strip()
    except Exception:
        return ""
    if not note or "Няма намерени резултати" in note or "Грешка при търсене" in note:
        return ""
    return ("## ПРОУЧВАНЕ ОТ ИНТЕРНЕТ (автоматично, преди да пишеш код — слаб "
            "локален модел, затова първо реален контекст, после код)\n" + note)


def _few_shot_example_for_weak_model(goal: str) -> str:
    """Един РЕАЛЕН, верифициран пример (задача + код) от skill библиотеката —
    демонстрира ОЧАКВАНИЯ ФОРМАТ на верен отговор (design note, 2026-08-11:
    reliability review-то установи нула few-shot примери в промпта досега).
    Различно от Brain.build_context, който инжектира код за ПРЕИЗПОЛЗВАНЕ —
    тук целта е "ето как изглежда правилен отговор", не непременно решение на
    ТАЗИ конкретна задача. Безопасно — празен низ при провал."""
    try:
        from genesis_agent.skill_loader import search_skills, skill_view
        hits = search_skills(goal, top_n=1)
    except Exception:
        return ""
    if not hits or not hits[0].get("verification", {}).get("verified"):
        return ""
    hit = hits[0]
    try:
        code = skill_view(hit["name"])["code"]
    except Exception:
        return ""
    if not code.strip():
        return ""
    lines = code.splitlines()[:40]
    example_task = hit.get("description") or hit.get("name", "")
    return ("## ПРИМЕР ОТ МИНАЛА ВЕРИФИЦИРАНА ЗАДАЧА (за ФОРМАТ на отговора, "
            "не непременно решение на ТАЗИ цел)\nЗадача: " + str(example_task) +
            "\nПравилен отговор:\n```python\n" + "\n".join(lines) + "\n```")


def _plan_for_weak_model(brain: Brain, goal: str) -> str:
    """Кратък план (2-5 стъпки) за целта — task decomposition, специално за
    слабия tier (design note, 2026-08-11): слаб модел се справя много по-добре
    с "направи стъпка X" отколкото с цялата задача накуп. Извиква ДИРЕКТНО
    _call_local (не brain.complete) — не искаме да пробваме отново вече
    изчерпания облачен chain само за да планираме, когато вече знаем, че сме
    на локалния tier. Безопасно — празен низ при провал/липса на локален модел."""
    if not brain.local:
        return ""
    plan_messages = [
        {"role": "system", "content": "You break a coding goal into 2-5 short, concrete, "
                                       "ordered steps. One line per step, no code, no explanation."},
        {"role": "user", "content": f"Goal: {goal}"},
    ]
    try:
        hit = brain._call_local(plan_messages, attempts=1)
    except Exception:
        return ""
    if not hit:
        return ""
    plan = (hit[0] or "").strip()
    if not plan:
        return ""
    return "## ПЛАН (следвай стъпка по стъпка)\n" + plan


def _context_boost_for_weak_model(brain: Brain, goal: str) -> str:
    """Целият "прочети → провери в интернет → виж пример → планирай → пиши
    код" пакет за слабия (локален) tier — вместо да разчита само на
    собствената си, по-плитка памет и инстинкт да пише код веднага. Всяка
    съставка е независимо безопасна; комбинацията просто пропуска частите,
    които не сработят, никога не спира мисията заради това."""
    parts = [p for p in (
        _research_for_weak_model(goal),
        _few_shot_example_for_weak_model(goal),
        _plan_for_weak_model(brain, goal),
    ) if p]
    return "\n\n".join(parts)


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
    _escalated = False  # виж проверката на дъното на цикъла защо е флаг, а не `==`
    # Провал на КАЧЕСТВОТО (verifier/критик отхвърлят) е различен сигнал от HTTP/
    # execution грешка — свободен модел може да връща HTTP 200 с боклук код
    # безкрайно, без това някога да го деприоритизира или да качи веригата
    # (design note, 2026-08-11). Броим го отделно и ескалираме към по-силната
    # безплатна кодинг верига, вместо да чакаме операторът ръчно да сложи
    # GENESIS_QUALITY=coding.
    _quality_escalate_after = max(2, max_rounds // 3)
    _quality_failures = 0
    # Живо хванат бъг (2026-08-11, реален 10/10-рунда провал на мини-DB
    # мисия): native tool_calls клонът по-долу прави `continue` БЕЗУСЛОВНО —
    # нищо не пречи на модела да вика USE_SKILL/RESEARCH рунд след рунд, без
    # НИКОГА да стигне до писане на код, изгаряйки целия max_rounds бюджет
    # на чисто търсене. Полезно (RAG-preамбюл), но само до определен праг —
    # след него моделът трябва изрично да спре и да пише.
    _tool_only_rounds = 0
    _force_code_after = max(3, (max_rounds * 2) // 3)

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

    # Задължително проучване за СЛАБИЯ (локален) tier (design note, 2026-08-11):
    # ако мисията ще тръгне директно от локалния 3B/7B/14B модел — офлайн
    # режим (GENESIS_LOCAL_ONLY=1) или изобщо няма конфигурирана облачна верига
    # — инжектираме grounded web research по целта ПРЕДИ първия рунд, вместо да
    # чакаме слабия модел сам да се сети да го поиска (обичайно не се сеща).
    # За нормални мисии (облакът пробва пръв) същото се случва РЕАКТИВНО долу,
    # в момента на реален fallback към локалния модел — тук не можем да знаем
    # предварително дали облакът ще откаже.
    _local_research_injected = False
    if brain.local and (os.environ.get("GENESIS_LOCAL_ONLY") == "1" or not brain.chain):
        boost = _context_boost_for_weak_model(brain, goal)
        if boost:
            report_thought("🔎 Локален tier от старта — проучвам, търся пример, планирам...")
            messages.append({"role": "system", "content": boost})
        _local_research_injected = True

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

        # ─── ПАДНАХМЕ НА ЛОКАЛНИЯ TIER — проучи преди да продължиш (design note,
        # 2026-08-11): облакът се пробва пръв за всяка нормална мисия, така че
        # горе (преди рунд 0) не можехме да знаем предварително дали ще стигнем
        # дотук. Веднага щом brain.current реално сочи локалния модел за първи
        # път тази мисия — това round-и отговор е писан "на сляпо" от слаб
        # модел без реален контекст; изхвърляме го и force-ваме нов опит,
        # информиран от grounded research, вместо да продължим с код от нищото.
        if (not _local_research_injected and brain.local
                and brain.current == brain.local
                and not str(reply.raw_text or "").startswith("Error:")):
            _local_research_injected = True
            boost = _context_boost_for_weak_model(brain, goal)
            if boost:
                report_thought("🔎 Паднахме на локален модел — проучвам, търся пример, планирам...")
                messages.append({"role": "assistant", "content": reply.raw_text or ""})
                messages.append({"role": "user", "content": boost +
                                 "\n\nИзползвай горното (ако е relevantно за целта) и напиши "
                                 "финалния Python скрипт със self-test, който печата OK."})
                continue

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
                import json as _json

                import genesis_skills
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

            _tool_only_rounds += 1
            if _tool_only_rounds >= _force_code_after:
                report_thought(f"⏱️ {_tool_only_rounds} рунда само tool calls, без код — "
                               "принуждавам писане сега.")
                messages.append({
                    "role": "user",
                    "content": (
                        "STOP calling tools. You have used most of the round budget "
                        "searching/looking things up without writing any code. Whatever "
                        "you have found so far is enough — write the final Python script "
                        "NOW, in a single ```python``` fence, with the required self-test. "
                        "Do not call USE_SKILL or any other tool in your next reply."
                    ),
                })
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

            # ─── LOCAL BEST-OF-N + ансамбъл от модели (design note, 2026-08-11) ───
            # Локалният inference е безплатен (собствен GPU) — вместо да приемем
            # сляпо първия отговор на слаб 3B/7B/14B модел, ако той не излезе
            # перфектен, вземаме LOCAL_EXTRA_CANDIDATES още опита (различен
            # инсталиран tier и/или temperature, виж Brain.generate_local_
            # candidates) и избираме най-добре верифицирания. Само когато вече
            # знаем, че сме на локалния tier тази мисия — облачните рундове са
            # достатъчно силни без това.
            if brain.local and brain.current == brain.local and _local_research_injected:
                best_score, best_code = _score_local_candidate(reply.code)
                best_raw = reply.raw_text
                if best_score < 2:
                    report_thought("🎲 Първият локален опит не е перфектен — пробвам още кандидати...")
                    for raw, code, _model in brain.generate_local_candidates(
                        messages, n=LOCAL_EXTRA_CANDIDATES
                    ):
                        if not code or best_score == 2:
                            continue
                        score, scored_code = _score_local_candidate(code)
                        if score > best_score:
                            best_score, best_code, best_raw = score, scored_code, raw
                reply.code, reply.raw_text = best_code, best_raw
                last_generated_code = reply.code

            # Ruff pre-check ПРЕДИ sandbox-а (design note, 2026-07-29): явен
            # синтактичен/lint проблем не се нуждае от истинско subprocess
            # изпълнение, за да се хване — same fail-open convention навсякъде
            # другаде (ако ruff липсва, validate_code_with_ruff връща (True,"")
            # и нищо не се променя). Auto-fix-натата версия минава напред към
            # sandbox-а вместо оригинала; unfixable проблем спестява целия
            # sandbox рунд — обратно към модела веднага, без subprocess такса.
            from genesis_agent.code_validate import validate_code_with_ruff
            lint_ok, lint_detail = validate_code_with_ruff(reply.code)
            if lint_ok and lint_detail:
                reply.code = lint_detail
                last_generated_code = reply.code
            elif not lint_ok:
                messages.append({"role": "assistant", "content": reply.raw_text})
                messages.append({"role": "user", "content":
                    f"{lint_detail}\n\nПоправи и върни ЦЕЛИЯ коригиран скрипт "
                    "в един ```python``` fence, преди да го пробваме."})
                continue

        if not reply.code:
            raw = str(reply.raw_text)
            if raw.startswith("Error:"):
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
                _quality_failures = _note_quality_failure(brain, _quality_failures, _quality_escalate_after)
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
            # avoid=writer_pair (design note, 2026-08-11): без това критикът много
            # често пада на СЪЩИЯ provider/model, който написа кода (chain-ът винаги
            # обхожда отгоре-надолу) — "второто мнение" тогава просто повтаря
            # слепите петна на първото. brain.current още сочи towards писателя тук
            # (нищо не го е сменило между генерирането на кода и този ред).
            writer = getattr(brain, "current", None)
            writer_pair = ((writer.get("provider"), writer.get("model"))
                           if isinstance(writer, dict) and writer.get("provider") else None)
            critic_eval = brain.complete(critic_msg, avoid=writer_pair).raw_text.strip()

            if critic_eval.upper().startswith("NO"):
                _quality_failures = _note_quality_failure(brain, _quality_failures, _quality_escalate_after)
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
        # `>=` + флаг, НЕ `== _escalate_after` (bug fix, 2026-08-12): този ред
        # стои на дъното на цикъла, а над него има шест `continue` пътя (native
        # tool calls, read-only tool tags, lint провал, липсващ код блок,
        # verifier отказ, критик отказ). При проверка за точно равенство беше
        # достатъчно ТОЧНО този рунд да мине по някой от тях, за да се пропусне
        # ескалацията завинаги — номерът на рунда не се връща обратно. При
        # подразбиращите се max_rounds=8 прагът е рунд 2, тоест един-единствен
        # tool-call рунд там изключваше ескалацията за цялата мисия.
        # Флагът се вдига при ОПИТ, не при успех: escalate() връща False, ако
        # няма по-голям инсталиран локален tier, а това не се променя по средата
        # на мисията — повтарянето му всеки следващ рунд би било само излишен
        # HTTP poll към Ollama на всеки кръг.
        if not _escalated and round_i + 1 >= _escalate_after:
            brain.escalate()
            _escalated = True

        # BROADCAST THOUGHT: FAILURE / SELF-CORRECT
        report_thought("❌ Грешка при изпълнението. Анализирам проблема и започвам самокорекция...")
        
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

        # Верифицирай ремонта преди да го запишеш трайно (design note, 2026-08-11):
        # emergency_repair.fixed досега значеше само "_test_code излезе с код 0" -
        # pattern фиксовете (fix_key_error: d["k"] -> d.get("k"); fix_undefined_name
        # -> None) могат тихо да МАСКИРАТ грешката вместо да я поправят ("не гърми"
        # != "прави правилното нещо"). Такъв код се записваше с verification_stdout=""
        # - т.е. НУЛЕВА верификация - и после се преизползва от бъдещи мисии през
        # RAG (Brain.build_context), пренасяйки бъга нататък. Минава през СЪЩИЯ
        # sandbox verify_skill гейт като нормалния успешен path по-горе.
        repair_verified = False
        vres = None
        if repair.fixed:
            from genesis_agent.verifier import verify_skill
            vres = verify_skill(repair.code)
            repair_verified = vres.verified
            if not repair_verified:
                print(f"  [РЕМОНТ ОТХВЪРЛЕН] Поправеният код не мина verify_skill "
                      f"({vres.method}) - вероятно маскира грешката вместо да я "
                      "поправя; НЕ се записва в библиотеката непроверен.")

        if repair_verified:
            print(f"\n  [\u2705 \u0410\u0412\u0410\u0420\u0418\u0415\u041d \u0420\u0415\u041c\u041e\u041d\u0422 \u0423\u0421\u041f\u0415\u0428\u0415\u041d] {repair.fix_desc}")
            print(f"  Метод: {repair.method} | Рундове: {repair.rounds} | verify: {vres.method}")

            slug = (skill_slug or slugify(goal)) + "_repaired"
            try:
                path = save_skill(
                    slug=slug,
                    code=repair.code,
                    goal=goal + " [repaired by LocalRepairAgent]",
                    verification_stdout=vres.detail,
                    extra={"repair_method": repair.method,
                           "repair_rounds": repair.rounds,
                           "verify_method": vres.method,
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
            print("  [\u0420\u0415\u041c\u041e\u041d\u0422 \u041d\u0415\u0423\u0421\u041f\u0415\u0428\u0415\u041d] \u041d\u0438\u0442\u043e pattern fixes, \u043d\u0438\u0442\u043e LLM \u043d\u0435 \u043f\u043e\u043c\u043e\u0433\u043d\u0430\u0445а.")

    return LoopOutcome(
        success=False,
        rounds=max_rounds,
        skill_path=None,
        last_stdout=last_stdout,
        last_stderr=last_stderr,
        storage_note=storage_note,
        dna_audit=audit,
    )
