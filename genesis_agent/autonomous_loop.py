"""Autonomous loop — Brain → Executor → self-correct → Skills Library (GENESIS DNA enforced)."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

from genesis_agent import dna, provider_stats
from genesis_agent.brain import Brain
from genesis_agent.config import MAX_LLM_RETRIES, PROJECT_ROOT
from genesis_agent.executor import format_failure_for_brain, run_python_subprocess
from genesis_agent.local_repair_agent import emergency_repair
from genesis_agent.repeat_guard import RepeatGuard
from genesis_agent.skill_loader import SKILLS_ROOT
from genesis_agent.skills_manager import save_skill, slugify
from genesis_agent.storage_monitor import check_storage, human_gb
from genesis_agent.tool_schemas import MISSION_TOOLS, load_tool_arguments

log = logging.getLogger("genesis.mission")

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
        log.debug("локален кандидат: оценката падна — 0 точки", exc_info=True)
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
            log.debug("провалът на качеството не се записа в provider_stats", exc_info=True)
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
        log.debug("проучването за слабия модел падна — без него", exc_info=True)
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
        log.debug("търсенето на пример от библиотеката падна", exc_info=True)
        return ""
    if not hits or not hits[0].get("verification", {}).get("verified"):
        return ""
    hit = hits[0]
    try:
        code = skill_view(hit["name"])["code"]
    except Exception:
        log.debug("кодът на примерното умение не се зареди", exc_info=True)
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
        log.debug("планът от локалния модел падна — без план", exc_info=True)
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
    Публична обвивка: изпълнява мисията и известява резултата през notifier
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
        log.debug("изходът на мисията не се записа за рефлексия", exc_info=True)
    try:
        from genesis_agent.notifier import notify
        if outcome.success:
            notify(f"✅ **Genesis** изпълни мисия за {outcome.rounds} рунда\n"
                   f"🎯 {goal[:200]}\n📦 {outcome.skill_path}")
        else:
            notify(f"❌ **Genesis** не успя с мисия след {outcome.rounds} рунда\n"
                   f"🎯 {goal[:200]}")
    except Exception:
        log.debug("известието за мисията не тръгна", exc_info=True)
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
    return _Mission(goal, max_rounds or MAX_LLM_RETRIES, skill_slug=skill_slug,
                    operator_id=operator_id, audit=audit, storage_note=storage_note).run()


def _stop_requested() -> bool:
    try:
        from genesis_agent.config import stop_event
    except ImportError:
        return False
    return stop_event.is_set()


def _genesis_skills():
    """genesis_skills живее в корена на проекта, не в пакета."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    import genesis_skills
    return genesis_skills


_READONLY_TAGS = ("[WEB_SEARCH:", "[RESEARCH:", "[READ_FILE:", "[LIST_DIR:")

_FIRST_TASK = (
    "If you need external info FIRST, you may reply with ONLY a read-only tool tag "
    "([WEB_SEARCH: query] for raw results, [RESEARCH: question] for a cross-verified grounded "
    "answer across multiple sources when accuracy matters, [READ_FILE: /path], or "
    "[LIST_DIR: /path]) and I will return the result before you write code. A USE_SKILL tool "
    "is also available (native function-calling) — prefer calling an existing verified skill "
    "directly over reimplementing it from scratch when one already covers part of the goal. "
    "Otherwise implement as a single Python script. CRITICAL: You MUST include verification "
    "code at the bottom of the script (e.g. asserts or checks) that explicitly verifies the "
    "goal was achieved. If verification fails, raise an Exception.")

_STOP_CALLING_TOOLS = (
    "STOP calling tools. You have used most of the round budget "
    "searching/looking things up without writing any code. Whatever "
    "you have found so far is enough — write the final Python script "
    "NOW, in a single ```python``` fence, with the required self-test. "
    "Do not call USE_SKILL or any other tool in your next reply.")


class _Mission:
    """Една мисия: състоянието между рундовете и стъпките на един рунд.

    Всеки рунд: модел → (инструменти | код) → изпълнение → тест-гейт → критик
    → запис в библиотеката. Стъпка, която връща модела за още работа,
    добавя бележката си към историята и рундът свършва; следващият пита пак.
    """

    def __init__(self, goal: str, max_rounds: int, *, skill_slug: str | None,
                 operator_id: str | None, audit: dict[str, object], storage_note: str) -> None:
        from genesis_agent.telemetry import report_thought
        self.think = report_thought
        self.goal = goal
        self.max_rounds = max_rounds
        self.skill_slug = skill_slug
        self.operator_id = operator_id
        self.audit = audit
        self.storage_note = storage_note
        # min_size_b=120 (design note, 2026-07-25): умения се пазят в библиотеката трайно
        # — качеството тежи повече от скоростта тук. Локалният мозък остава последна
        # резерва независимо от размера (виж Brain.__init__).
        self.brain = Brain(min_size_b=120)
        self.brain.route_for_goal(goal)  # адаптивен избор на модел според сложността
        self.escalate_after = max(2, max_rounds // 3)  # 1/3 неуспешни рунда → по-голям модел
        self.escalated = False  # виж _escalate_if_due защо е флаг, а не `==`
        # Провал на КАЧЕСТВОТО (verifier/критик отхвърлят) е различен сигнал от HTTP/
        # execution грешка — свободен модел може да връща HTTP 200 с боклук код
        # безкрайно, без това някога да го деприоритизира или да качи веригата
        # (design note, 2026-08-11). Броим го отделно и ескалираме към по-силната
        # безплатна кодинг верига, вместо да чакаме операторът ръчно да сложи
        # GENESIS_QUALITY=coding.
        self.quality_escalate_after = max(2, max_rounds // 3)
        self.quality_failures = 0
        # Живо хванат бъг (2026-08-11, реален 10/10-рунда провал на мини-DB
        # мисия): моделът може да вика USE_SKILL/RESEARCH рунд след рунд, без
        # НИКОГА да стигне до писане на код. Полезно, но само до определен праг.
        self.tool_only_rounds = 0
        self.force_code_after = max(3, (max_rounds * 2) // 3)
        # Заповедта „пиши код“ се издава веднъж — и в system съобщението, което
        # trim_round_history пази, не само като пореден user ред (bug fix,
        # 2026-09-20): user редът оцелява ровно един рунд.
        self.forced_code = False
        # Същият извик, същият изход, пореден път — виж genesis_agent.repeat_guard.
        # Тук е по-остро, отколкото в чата: рундовете на мисия са 8, не 25, така че
        # три изгорени в кръг са над една трета от целия бюджет за задачата.
        self.spin_guard = RepeatGuard()
        self.halted = False
        self.last_generated_code = ""
        self.last_stdout = ""
        self.last_stderr = ""
        # RAG: релевантен контекст (подобни умения + минали уроци).
        self.rag_context = self.brain.build_context(goal)
        self.messages: list[dict[str, Any]] = self._first_messages()
        self.local_research_injected = self._boost_a_local_start()

    def _first_messages(self) -> list[dict[str, Any]]:
        red_note = ""
        if dna.red_zone_elevation_granted():
            red_note = (
                "\n\n[SYSTEM] GENE-SECURITY: Red Zone elevation token is ACTIVE for this session. "
                "Registry/system code is still discouraged unless strictly necessary for the goal.\n"
            )
        rag_block = (f"\n\n## КОНТЕКСТ ОТ ПАМЕТТА\n{self.rag_context}\n"
                     if self.rag_context else "")
        # Мета-обучение: дестилирани уроци от минали грешки → в system prompt-а.
        system_content = self.brain.system_prompt_base()
        try:
            from genesis_agent.reflection import lessons_for_prompt
            lessons = lessons_for_prompt()
            if lessons:
                system_content += "\n\n" + lessons
        except Exception:
            log.debug("уроците от минали мисии не влязоха в промпта", exc_info=True)
        return [
            {"role": "system", "content": system_content},
            {"role": "user",
             "content": f"High-level goal:\n{self.goal}{rag_block}\n\n{_FIRST_TASK}{red_note}"},
        ]

    def _boost_a_local_start(self) -> bool:
        """Задължително проучване за СЛАБИЯ (локален) tier (design note,
        2026-08-11): тръгне ли мисията директно от локалния модел — офлайн режим
        (GENESIS_LOCAL_ONLY=1) или няма облачна верига — grounded web research
        по целта влиза ПРЕДИ първия рунд. За нормални мисии същото става
        реактивно, в _fell_to_local, при реален fallback. True = вече е сторено."""
        b = self.brain
        if not (b.local and (os.environ.get("GENESIS_LOCAL_ONLY") == "1" or not b.chain)):
            return False
        boost = _context_boost_for_weak_model(b, self.goal)
        if boost:
            self.think("🔎 Локален tier от старта — проучвам, търся пример, планирам...")
            self.messages.append({"role": "system", "content": boost})
        return True

    # ── Рундовете ───────────────────────────────────────────────────────────

    def run(self) -> LoopOutcome:
        for round_i in range(self.max_rounds):
            if _stop_requested():
                print("\n[!] Изпълнението е ПРЕКЪСНАТО от потребителя (ПАУЗА)!")
                self.last_stderr = "Изпълнението е прекъснато от потребителя."
                break
            outcome = self._round(round_i)
            if outcome is not None:
                return outcome
            if self.halted:
                break
        if self.last_generated_code and self.last_stderr and not _stop_requested():
            repaired = self._emergency_repair()
            if repaired is not None:
                return repaired
        return self._outcome(success=False, rounds=self.max_rounds)

    def _round(self, round_i: int) -> LoopOutcome | None:
        """Един рунд. LoopOutcome — мисията е готова; None — следващ рунд
        (или край, ако self.halted)."""
        self.messages = Brain.trim_round_history(self.messages)
        reply = self.brain.complete(self.messages, tools=MISSION_TOOLS)
        if self._fell_to_local(reply):
            return None
        if reply.tool_calls:
            self._native_tools(reply)
            return None
        if self._readonly_tags(reply):
            return None
        if reply.code and not self._prepare_code(reply):
            return None
        if not reply.code:
            self._no_code(reply)
            return None
        result = run_python_subprocess(reply.code)
        self.last_stdout, self.last_stderr = result.stdout, result.stderr
        if result.ok:
            return self._judge(reply, result, round_i)
        self._escalate_if_due(round_i)
        # BROADCAST THOUGHT: FAILURE / SELF-CORRECT
        self.think("❌ Грешка при изпълнението. Анализирам проблема и започвам самокорекция...")
        self._reply_and_ask(reply.raw_text,
                            "The code failed when executed. Fix ALL issues and return the "
                            "complete corrected script in one ```python``` block.\n\n"
                            + format_failure_for_brain(result))
        return None

    def _reply_and_ask(self, reply_text: str, request: str) -> None:
        self.messages.append({"role": "assistant", "content": reply_text})
        self.messages.append({"role": "user", "content": request})

    def _fell_to_local(self, reply: Any) -> bool:
        """Паднахме на локалния tier — проучи преди да продължиш (design note,
        2026-08-11). Облакът се пробва пръв, така че преди рунд 0 не можехме да
        знаем дали ще стигнем дотук. Щом brain.current за първи път сочи
        локалния модел, отговорът е писан „на сляпо“ от слаб модел — изхвърля се
        и следва нов опит, информиран от grounded research. True = рундът свършва."""
        b = self.brain
        if (self.local_research_injected or not b.local or b.current != b.local
                or str(reply.raw_text or "").startswith("Error:")):
            return False
        self.local_research_injected = True
        boost = _context_boost_for_weak_model(b, self.goal)
        if not boost:
            return False
        self.think("🔎 Паднахме на локален модел — проучвам, търся пример, планирам...")
        self._reply_and_ask(reply.raw_text or "", boost +
                            "\n\nИзползвай горното (ако е relevantно за целта) и напиши "
                            "финалния Python скрипт със self-test, който печата OK.")
        return True

    def _native_tools(self, reply: Any) -> None:
        """Native tool use (design note, 2026-07-25, „мисии с реални умения“): същият
        backend като чата (genesis_skills.dispatch_tool_call), най-важно USE_SKILL —
        Brain-ът може РЕАЛНО да изпълни съществуващо умение. Verifier-ът и
        критикът не виждат тези рундове."""
        self.think("🔧 Brain вика инструмент (native)...")
        self.messages.append({"role": "assistant", "content": reply.raw_text or "",
                              "tool_calls": reply.tool_calls})
        spinning = self._dispatch(reply.tool_calls)
        self.tool_only_rounds += 1
        if not self.forced_code and (self.tool_only_rounds >= self.force_code_after or spinning):
            self._force_code(spinning)

    def _dispatch(self, tool_calls: list[dict]) -> bool:
        """Изпълнява извикванията; True — същият извик върна същото пореден път."""
        spinning = False
        try:
            skills = _genesis_skills()
            for tc in tool_calls:
                fn = tc.get("function", {}) or {}
                name = fn.get("name", "")
                try:
                    args = load_tool_arguments(fn.get("arguments"))
                except (json.JSONDecodeError, TypeError):
                    args = {}
                tool_out = skills.dispatch_tool_call(name, args)
                self.messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                      "name": name, "content": tool_out[:4000]})
                if self.spin_guard.observe(name, args, tool_out).stop:
                    spinning = True
        except Exception as e:
            log.debug("инструментът на мисията падна — грешката отива при модела", exc_info=True)
            self.messages.append({"role": "tool", "tool_call_id": "error",
                                  "name": "error", "content": f"[tool грешка: {e}]"})
        return spinning

    def _force_code(self, spinning: bool) -> None:
        self.forced_code = True
        if spinning:
            self.think("🔁 Същият инструмент, същият резултат, трети пореден път — "
                       "търсенето не води доникъде; принуждавам писане сега.")
        else:
            self.think(f"⏱️ {self.tool_only_rounds} рунда само tool calls, без код — "
                       "принуждавам писане сега.")
        self.messages.append({"role": "user", "content": _STOP_CALLING_TOOLS})
        # ...и в system-а, който trim_round_history никога не реже.
        first = self.messages[0] if self.messages else {}
        if first.get("role") == "system":
            self.messages[0] = {**first,
                                "content": f"{first.get('content', '')}\n\n{_STOP_CALLING_TOOLS}"}

    def _readonly_tags(self, reply: Any) -> bool:
        """Модели БЕЗ native function-calling (fallback опашката в Brain): ако
        поискат информация (WEB_SEARCH/READ_FILE/LIST_DIR) вместо код, тя се
        изпълнява и връща, за да напишат кода информирано. Не е провал."""
        if reply.code or not any(t in reply.raw_text for t in _READONLY_TAGS):
            return False
        try:
            results = _genesis_skills().parse_and_execute_readonly_tools(reply.raw_text)
        except Exception as e:
            log.debug("read-only таговете паднаха — грешката отива при модела", exc_info=True)
            results = [f"[tool грешка: {e}]"]
        if not results:
            return False
        self.think("🔎 Brain ползва инструмент за информация...")
        self._reply_and_ask(reply.raw_text,
                            "Резултат от инструментите:\n" + "\n\n".join(results)[:4000] +
                            "\n\nСега напиши финалния Python скрипт със self-test, който печата OK.")
        return True

    def _no_code(self, reply: Any) -> None:
        raw = str(reply.raw_text)
        if raw.startswith("Error:"):
            print(f"\n[КРИТИЧНА ГРЕШКА] Сървърът върна: {raw}")
            # Спира веднага — няма смисъл да въртим 8 рунда при грешка на връзка.
            print("[!] Прекратявам опитите. Провери модела и повтори (/модел)")
            self.halted = True
            return
        self._reply_and_ask(reply.raw_text, "No ```python``` block found. Respond with exactly "
                                            "one ```python ... ``` fence containing the full script.")

    # ── Кодът преди и след изпълнението ─────────────────────────────────────

    def _prepare_code(self, reply: Any) -> bool:
        """Best-of-N на локалния tier и ruff. False — върнат е на модела."""
        self.last_generated_code = reply.code
        b = self.brain
        if b.local and b.current == b.local and self.local_research_injected:
            self._best_local_candidate(reply)
        # Ruff ПРЕДИ sandbox-а (design note, 2026-07-29): явен синтактичен/lint
        # проблем не се нуждае от истинско изпълнение, за да се хване. Без ruff
        # validate_code_with_ruff връща (True, "") и нищо не се променя;
        # auto-fix-натата версия минава напред вместо оригинала.
        from genesis_agent.code_validate import validate_code_with_ruff
        lint_ok, lint_detail = validate_code_with_ruff(reply.code)
        if not lint_ok:
            self._reply_and_ask(reply.raw_text, f"{lint_detail}\n\nПоправи и върни ЦЕЛИЯ "
                                                "коригиран скрипт в един ```python``` fence, "
                                                "преди да го пробваме.")
            return False
        if lint_detail:
            reply.code = lint_detail
            self.last_generated_code = reply.code
        return True

    def _best_local_candidate(self, reply: Any) -> None:
        """Локалният inference е безплатен (design note, 2026-08-11): вместо да
        приемем първия отговор на слаб модел, ако не е перфектен, пробваме още
        LOCAL_EXTRA_CANDIDATES и вземаме най-добре верифицирания."""
        best_score, best_code = _score_local_candidate(reply.code)
        best_raw = reply.raw_text
        if best_score < 2:
            self.think("🎲 Първият локален опит не е перфектен — пробвам още кандидати...")
            for raw, code, _model in self.brain.generate_local_candidates(
                self.messages, n=LOCAL_EXTRA_CANDIDATES
            ):
                if not code or best_score == 2:
                    continue
                score, scored_code = _score_local_candidate(code)
                if score > best_score:
                    best_score, best_code, best_raw = score, scored_code, raw
        reply.code, reply.raw_text = best_code, best_raw
        self.last_generated_code = reply.code

    def _judge(self, reply: Any, result: Any, round_i: int) -> LoopOutcome | None:
        """Тест-гейт (реална проверка, не мнение на LLM), после критик, после запис."""
        from genesis_agent.verifier import verify_skill
        vres = verify_skill(reply.code)
        if vres.method != "self_test_passed":
            self._quality_failed()
            self._ask_for_a_self_test(reply, vres)
            return None
        verdict = self._critic(reply)
        if verdict.upper().startswith("NO"):
            self._quality_failed()
            self.think(f"🔍 Критикът отхвърли резултата: {verdict}")
            self._reply_and_ask(reply.raw_text,
                                "Code ran and self-test passed, but a reviewer says it does not "
                                f"meet the goal: {verdict}. Fix and return the FULL corrected script.")
            return None
        self.think("✅ Тест-гейт + критик одобриха резултата.")
        return self._save(reply, result, round_i)

    def _quality_failed(self) -> None:
        self.quality_failures = _note_quality_failure(
            self.brain, self.quality_failures, self.quality_escalate_after)

    def _ask_for_a_self_test(self, reply: Any, vres: Any) -> None:
        # Обратната връзка описва ИСТИНСКАТА причина. Когато проверката е отказана
        # заради нужното потвърждение, кодът има преминаващ self-test — просто не е
        # бил пуснат. Общото съобщение („няма self-test, добави assert-и“) оставяше
        # на модела един начин да се подчини: да махне подпроцеса, тоест да
        # обезсмисли умението. Гейтът е същият — сменя се само какво се иска.
        if vres.method == "needs_confirmation":
            self.think("🧪 Тест-гейт: self-testът не може да се пусне без надзор "
                       f"({vres.detail[:120]})")
            self._reply_and_ask(reply.raw_text, (
                "The self-test could NOT be run: verification runs unattended and "
                "the code performs an operation that requires confirmation "
                f"({vres.detail[:200]}). Do NOT remove that capability — it is the "
                "point of the skill. Restructure instead: keep the privileged call "
                "inside a function, and make the `__main__` self-test verify the "
                "logic around it without performing it (assert on argument "
                "assembly, parsing of a sample output, a dry-run flag). Print 'OK' "
                "on success and return the FULL corrected script."))
            return
        self.think(f"🧪 Тест-гейт отхвърли: няма преминаващ self-test ({vres.method})")
        self._reply_and_ask(reply.raw_text, (
            "The code ran but has NO passing self-test. Add assert-based checks at "
            "the bottom that verify the goal was actually achieved and print 'OK' on "
            f"success, then return the FULL corrected script. (verifier: {vres.method})"))

    def _critic(self, reply: Any) -> str:
        """Семантично второ мнение. avoid=авторът (design note, 2026-08-11): без
        това критикът често пада на СЪЩИЯ модел, който написа кода, и „второто
        мнение“ повтаря слепите петна на първото."""
        critic_prompt = (
            f"Goal: {self.goal}\n\n"
            f"Code Output:\n{self.last_stdout}\n\n"
            f"Code:\n{reply.code}\n\n"
            "Did the code FULLY accomplish the specific goal? For example, if it was asked to save to a file, does the code actually write to a file?\n"
            "If YES, reply exactly 'YES'.\n"
            "If NO (it missed a requirement or just printed instead of saving), reply 'NO: <reason>'. Do not write code."
        )
        critic_msg = [
            {"role": "system", "content": "You are a strict code reviewer. You ONLY reply with YES or NO: <reason>."},
            {"role": "user", "content": critic_prompt},
        ]
        writer = getattr(self.brain, "current", None)
        writer_pair: tuple[str, str] | None = None
        if isinstance(writer, dict):
            w_provider, w_model = writer.get("provider"), writer.get("model")
            if isinstance(w_provider, str) and isinstance(w_model, str):
                writer_pair = (w_provider, w_model)
        return self.brain.complete(critic_msg, avoid=writer_pair).raw_text.strip()

    def _save(self, reply: Any, result: Any, round_i: int) -> LoopOutcome | None:
        extra: dict[str, Any] = {"rounds": round_i + 1, "test_gated": True}
        extra.update(self.audit)
        try:
            path = save_skill(slug=self.skill_slug or slugify(self.goal), code=reply.code,
                              goal=self.goal, verification_stdout=result.stdout, extra=extra)
        except dna.GenesisDNAError as e:
            self.last_stderr = str(e)
            self._reply_and_ask(reply.raw_text,
                                "Skills Library rejected the script under GENESIS DNA (ethics/red zone). "
                                "Rewrite to comply: no harm to humans, no registry/system Red Zone without "
                                "approval token, single ```python``` block.\n\n" + str(e))
            return None
        try:
            from genesis_agent.reflection import detect_reuse
            reused = detect_reuse(self.rag_context, reply.code)
        except Exception:
            log.debug("проверката за преизползване падна — брои се като непреизползвано", exc_info=True)
            reused = False
        return self._outcome(success=True, rounds=round_i + 1,
                             skill_path=_library_path(path), reused_existing=reused)

    def _escalate_if_due(self, round_i: int) -> None:
        """Малкият модел се мъчи → по-голям (3b→7b→14b). `>=` + флаг, НЕ `==`
        (bug fix, 2026-08-12): провалите стигат дотук само при неуспешно
        изпълнение, а над него има шест пътя, които прескачат рунда (tool calls,
        тагове, ruff, без код, verifier, критик) — при `==` един такъв рунд на
        прага изключваше ескалацията за цялата мисия. Флагът се вдига при ОПИТ:
        escalate() връща False, ако няма по-голям локален tier, а това не се
        променя по средата на мисията."""
        if not self.escalated and round_i + 1 >= self.escalate_after:
            self.brain.escalate()
            self.escalated = True

    # ── Краят ───────────────────────────────────────────────────────────────

    def _emergency_repair(self) -> LoopOutcome | None:
        """Рундовете свършиха с код и грешка: LocalRepairAgent (шаблони + малък
        модел). Ремонтът минава през СЪЩИЯ verify_skill гейт (design note,
        2026-08-11): „не гърми“ ≠ „прави правилното“ — шаблонен фикс може да
        маскира грешката, а непроверено умение после се преизползва през RAG."""
        print("\n" + "═" * 55)
        print("  [⚠️  АВАРИЕН РЕМОНТ] Brain е недостъпен. Активирам LocalRepairAgent...")
        print("  [МАЛЪК МОДЕЛ] Патерн анализ + 1-3B модел")
        print("═" * 55)
        repair = emergency_repair(self.last_generated_code, self.last_stderr, self.last_stdout)
        if not repair.fixed:
            print("  [РЕМОНТ НЕУСПЕШЕН] Нито pattern fixes, нито LLM не помогнаха.")
            return None
        from genesis_agent.verifier import verify_skill
        vres = verify_skill(repair.code)
        if not vres.verified:
            print(f"  [РЕМОНТ ОТХВЪРЛЕН] Поправеният код не мина verify_skill "
                  f"({vres.method}) - вероятно маскира грешката вместо да я "
                  "поправя; НЕ се записва в библиотеката непроверен.")
            print("  [РЕМОНТ НЕУСПЕШЕН] Нито pattern fixes, нито LLM не помогнаха.")
            return None
        print(f"\n  [✅ АВАРИЕН РЕМОНТ УСПЕШЕН] {repair.fix_desc}")
        print(f"  Метод: {repair.method} | Рундове: {repair.rounds} | verify: {vres.method}")
        try:
            path = save_skill(
                slug=(self.skill_slug or slugify(self.goal)) + "_repaired",
                code=repair.code,
                goal=self.goal + " [repaired by LocalRepairAgent]",
                verification_stdout=vres.detail,
                extra={"repair_method": repair.method,
                       "repair_rounds": repair.rounds,
                       "verify_method": vres.method,
                       "operator": self.operator_id or "operator"})
        except Exception as save_err:
            log.debug("ремонтираното умение не се записа", exc_info=True)
            print(f"  [РЕМОНТ] Грешка при запазване: {save_err}")
            return None
        return self._outcome(success=True, rounds=self.max_rounds + repair.rounds,
                             skill_path=_library_path(path))

    def _outcome(self, *, success: bool, rounds: int, skill_path: str | None = None,
                 reused_existing: bool = False) -> LoopOutcome:
        return LoopOutcome(success=success, rounds=rounds, skill_path=skill_path,
                           last_stdout=self.last_stdout, last_stderr=self.last_stderr,
                           storage_note=self.storage_note, dna_audit=self.audit,
                           reused_existing=reused_existing)


def _library_path(path) -> str:
    return str(path.relative_to(SKILLS_ROOT)).replace("\\", "/")
