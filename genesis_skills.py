#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genesis_skills.py — Мостът между genesis_terminal_agent.py и genesis_agent/ ядрото.

Терминалният агент праща отговора на LLM тук; ние извличаме тул-таговете и ги
изпълняваме — но всяко реално изпълнение минава през genesis_agent.sandbox (SAFE/
CONFIRM/BLOCKED бариерата) и се записва в genesis_agent.memory / episodic_memory.

Поддържани тагове (както са описани в config.yaml system_prompt):
    [READ_FILE: /път/до/файл]
    [WRITE_FILE: /път/до/файл]съдържание[END_WRITE]
    [RUN_CMD: команда]                 (или: RUN_CMD: bash -c '...')
    [WEB_SEARCH: заявка]
    [LIST_DIR: /път/до/директория]
    [USE_SKILL: име_или_заявка]опционален Python driver, вика функциите на умението директно по име[END_USE_SKILL]
    [DELEGATE: описание на задача]
    [BROWSE: url]                      — зарежда страница (истински headless браузър)
    [BROWSER_READ]                     — препрочита текущата страница
    [BROWSER_CLICK: индекс_или_текст]  — клик (CONFIRM; BLOCKED за плащане/поръчка)
    [BROWSER_TYPE: индекс_или_текст | текст]  — попълва поле (BLOCKED за пароли/карти)

Публичен интерфейс (какъвто genesis_terminal_agent.py очаква):
    set_workspace(path)
    parse_and_execute_tools(response_text) -> list[str]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Уверяваме се, че genesis_agent/ пакетът е импортируем (мостът стои в root-а).
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from genesis_agent import sandbox  # noqa: E402

# Паметта/уеб търсенето са meko-опционални — ако липсва зависимост (напр.
# scikit-learn), мостът пак работи, само без логване/търсене.
try:
    from genesis_agent.memory import memory_record_episode
except Exception:  # pragma: no cover
    memory_record_episode = None  # type: ignore

try:
    from genesis_agent import web_search
except Exception:  # pragma: no cover
    web_search = None  # type: ignore

try:
    from genesis_agent import browser as _browser_mod
except Exception:  # pragma: no cover
    _browser_mod = None  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# Състояние
# ─────────────────────────────────────────────────────────────────────────────

_WORKSPACE = _PROJECT_ROOT


def set_workspace(path) -> None:
    """Задава работната директория (вика се от genesis_terminal_agent.py)."""
    global _WORKSPACE
    _WORKSPACE = Path(path)


def _resolve(path_str: str) -> Path:
    """Разрешава път — относителните са спрямо workspace-а."""
    p = Path(path_str.strip()).expanduser()
    return p if p.is_absolute() else (_WORKSPACE / p)


def _log_episode(goal: str, outcome: str, tags: list[str]) -> None:
    if memory_record_episode is None:
        return
    try:
        memory_record_episode(goal=goal, outcome=outcome[:2000],
                              skill_path="genesis_skills.bridge", tags=tags)
    except Exception:
        pass  # логването никога не бива да чупи изпълнението


# ─────────────────────────────────────────────────────────────────────────────
# Реализация на отделните тулове
# ─────────────────────────────────────────────────────────────────────────────

def _tool_read_file(arg: str) -> str:
    path = _resolve(arg)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"[READ_FILE] Файлът не съществува: {path}"
    except OSError as e:
        return f"[READ_FILE] Грешка: {e}"
    if len(text) > 8000:
        text = text[:8000] + f"\n… [отрязано, общо {len(text)} символа]"
    _log_episode(f"READ_FILE {path}", "прочетен", ["tool", "read_file"])
    return f"[READ_FILE: {path}]\n{text}"


def _tool_write_file(arg: str, content: str) -> str:
    path = _resolve(arg)
    # Пишем ли извън workspace-а? Това е операция за потвърждение.
    try:
        inside = path.resolve().is_relative_to(_WORKSPACE.resolve())
    except (ValueError, OSError):
        inside = False
    if not inside:
        verdict = sandbox.RiskVerdict(sandbox.RiskLevel.CONFIRM,
                                      [f"запис извън workspace: {path}"])
        allowed, reason = sandbox._decide(f"WRITE_FILE {path}", verdict, sandbox.get_policy())
        if not allowed:
            return f"[WRITE_FILE] {reason}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"[WRITE_FILE] Грешка: {e}"
    _log_episode(f"WRITE_FILE {path}", f"записани {len(content)} символа",
                 ["tool", "write_file"])
    return f"[WRITE_FILE: {path}] ✓ записани {len(content)} символа"


def _tool_run_cmd(arg: str) -> str:
    command = arg.strip()
    res = sandbox.run_shell(command, cwd=_WORKSPACE)
    if res.blocked:
        _log_episode(f"RUN_CMD {command}", f"отказан: {res.stderr}", ["tool", "run_cmd", "blocked"])
        # Security alert в Discord/Telegram — блокирана опасна операция.
        try:
            from genesis_agent.notifier import notify
            notify(f"🛡️ **Genesis Sandbox** блокира опасна команда:\n`{command[:300]}`\n{res.stderr[:200]}")
        except Exception:
            pass
        return f"[RUN_CMD: {command}]\n{res.stderr}"
    out = res.stdout.strip()
    err = res.stderr.strip()
    parts = [f"[RUN_CMD: {command}]  (rc={res.returncode})"]
    if out:
        parts.append(out[:6000])
    if err:
        parts.append("stderr:\n" + err[:2000])
    _log_episode(f"RUN_CMD {command}", "ok" if res.ok else f"rc={res.returncode}",
                 ["tool", "run_cmd"])
    return "\n".join(parts)


# Маркер, по който агентният цикъл разпознава "агентът чака отговор" и спира,
# вместо да продължи да гадае. Проверява се в genesis_agent/agent_core.py.
ASK_USER_MARKER = "__GENESIS_ASK_USER__"


def _tool_ask_user(question: str, options=None) -> str:
    """Питане при неяснота (design note, 2026-07-27).

    Нищо не се изпълнява — този инструмент СПИРА цикъла. Реалното "изчакване"
    е връщането на контрола към човека: фронтендът показва въпроса, а
    следващото съобщение на потребителя е отговорът. Затова тук няма input() —
    той би увиснал в Discord/GUI/systemd, където няма кой да пише в stdin.
    """
    q = (question or "").strip()
    if not q:
        return "[ASK_USER] Празен въпрос — формулирай какво точно ти е неясно."
    lines = [f"{ASK_USER_MARKER}❓ {q}"]
    if options:
        if isinstance(options, str):
            options = [options]
        for i, opt in enumerate(options, 1):
            lines.append(f"   {i}. {opt}")
    _log_episode(f"ASK_USER {q[:120]}", "изчаква отговор", ["tool", "ask_user"])
    return "\n".join(lines)


def _tool_web_search(arg: str) -> str:
    query = arg.strip()
    if web_search is None:
        return f"[WEB_SEARCH: {query}] web_search модулът не е наличен."
    try:
        results = web_search.search(query, max_results=5)
    except Exception as e:
        return f"[WEB_SEARCH: {query}] Грешка: {e}"
    if not results:
        return f"[WEB_SEARCH: {query}] Няма резултати."
    lines = [f"[WEB_SEARCH: {query}]"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title','')}\n   {r.get('url','')}\n   {r.get('snippet','')}")
    _log_episode(f"WEB_SEARCH {query}", f"{len(results)} резултата", ["tool", "web_search"])
    return "\n".join(lines)


def _tool_research(arg: str) -> str:
    """Grounded research: за разлика от WEB_SEARCH (сурови snippet-и), тук
    отговорът се извлича ОТДЕЛНО от всеки източник и се cross-check-ва —
    вместо моделът просто да "повярва" на първия snippet."""
    try:
        from genesis_agent.research import grounded_research
    except Exception as e:
        return f"[RESEARCH] research модулът не е наличен: {e}"
    return grounded_research(arg.strip())


def _tool_list_dir(arg: str) -> str:
    path = _resolve(arg)
    if not path.is_dir():
        return f"[LIST_DIR] Не е директория: {path}"
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as e:
        return f"[LIST_DIR] Грешка: {e}"
    lines = [f"[LIST_DIR: {path}]"]
    for e in entries[:200]:
        marker = "📁" if e.is_dir() else "📄"
        lines.append(f"  {marker} {e.name}")
    if len(entries) > 200:
        # Съкратеният списък е азбучен, значи цели буквени "опашки" изчезват —
        # в папка с 2157 .md файла `skills.json` (буква "s") просто не се
        # показва. Наблюдавано на живо: агентът заключи "няма skills.json",
        # което е грешно, и построи цял план върху грешния извод. Затова при
        # отрязване даваме и обобщение по разширение + редките типове изцяло:
        # то веднага показва, че ИМА .json файл, дори да не е в първите 200.
        from collections import Counter
        files = [e for e in entries if e.is_file()]
        by_ext = Counter((e.suffix.lower() or "(без разширение)") for e in files)
        summary = ", ".join(f"{n}× {ext}" for ext, n in by_ext.most_common(8))
        lines.append(f"  … още {len(entries) - 200} записа (списъкът е ОТРЯЗАН, азбучно)")
        lines.append(f"  Общо {len(entries)} записа. По тип: {summary}")
        rare = [e.name for ext, n in by_ext.items() if n <= 3
                for e in files if (e.suffix.lower() or "(без разширение)") == ext]
        if rare:
            lines.append("  Редки/единични файлове (виждат се изцяло): " + ", ".join(sorted(rare)[:20]))
    return "\n".join(lines)


def _tool_look_at_screen(arg: str) -> str:
    try:
        from genesis_agent.vision import describe_screen
    except Exception as e:
        return f"[LOOK_AT_SCREEN] vision модулът не е наличен: {e}"
    return f"[LOOK_AT_SCREEN] {describe_screen(arg.strip())}"


def _tool_browse(arg: str) -> str:
    if _browser_mod is None:
        return "[BROWSE] browser модулът не е наличен (Playwright не е инсталиран)."
    _log_episode(f"BROWSE {arg.strip()}", "навигация", ["tool", "browser"])
    return _browser_mod.navigate(arg)


def _tool_browser_read(_arg: str = "") -> str:
    if _browser_mod is None:
        return "[BROWSER_READ] browser модулът не е наличен."
    return _browser_mod.read()


def _tool_browser_click(arg: str) -> str:
    if _browser_mod is None:
        return "[BROWSER_CLICK] browser модулът не е наличен."
    res = _browser_mod.click(arg)
    _log_episode(f"BROWSER_CLICK {arg.strip()}",
                 "declined" if "DECLINED" in res or "BLOCKED" in res else "ok",
                 ["tool", "browser", "click"])
    return res


def _tool_browser_type(arg: str) -> str:
    if _browser_mod is None:
        return "[BROWSER_TYPE] browser модулът не е наличен."
    res = _browser_mod.type_text(arg)
    _log_episode(f"BROWSER_TYPE {arg.split('|')[0].strip()}",
                 "declined" if "DECLINED" in res or "BLOCKED" in res else "ok",
                 ["tool", "browser", "type"])
    return res


def _tool_use_skill(name_arg: str, driver: str) -> str:
    """Реално изпълнение на съществуващо умение (не просто регенерация от нула)."""
    try:
        from genesis_agent.skill_loader import use_skill
    except Exception as e:
        return f"[USE_SKILL] skill_loader модулът не е наличен: {e}"
    result = use_skill(name_arg, driver)
    _log_episode(f"USE_SKILL {name_arg.strip()}", result[:300], ["tool", "use_skill"])
    return result


def _tool_remember(arg: str) -> str:
    """Записва траен факт в workspace паметта. Формат:
        [REMEMBER: решение | какво е решено | защо]
        [REMEMBER: предпочитание | тема | стойност]
    Досега Genesis нямаше НИКАКЪВ начин да запише нещо за потребителя — можеше само
    да чете инжектираното при старт."""
    try:
        from genesis_agent import workspace_memory as wm
    except Exception as e:
        return f"[REMEMBER] workspace_memory не е наличен: {e}"
    parts = [p.strip() for p in arg.split("|")]
    kind = parts[0].lower() if parts else ""
    if kind.startswith(("предпочитание", "preference", "pref")):
        if len(parts) < 3:
            return "[REMEMBER] Формат: [REMEMBER: предпочитание | тема | стойност]"
        return wm.set_preference(parts[1], parts[2])
    if kind.startswith(("решение", "decision")):
        if len(parts) < 2:
            return "[REMEMBER] Формат: [REMEMBER: решение | какво | защо]"
        return wm.add_decision(parts[1], parts[2] if len(parts) > 2 else "")
    # Без изричен вид — третираме целия текст като решение (по-полезно от отказ).
    return wm.add_decision(arg.strip())


def _tool_task_add(arg: str) -> str:
    """[TASK_ADD: заглавие | следваща стъпка]"""
    try:
        from genesis_agent import workspace_memory as wm
    except Exception as e:
        return f"[TASK_ADD] workspace_memory не е наличен: {e}"
    parts = [p.strip() for p in arg.split("|")]
    return wm.add_thread(parts[0], parts[1] if len(parts) > 1 else "")


def _tool_task_update(arg: str) -> str:
    """[TASK_UPDATE: id | статус | следваща стъпка]  (статус: open/blocked/done)"""
    try:
        from genesis_agent import workspace_memory as wm
    except Exception as e:
        return f"[TASK_UPDATE] workspace_memory не е наличен: {e}"
    parts = [p.strip() for p in arg.split("|")]
    return wm.update_thread(parts[0],
                            parts[1] if len(parts) > 1 else "",
                            parts[2] if len(parts) > 2 else "")


def _tool_task_list(arg: str = "") -> str:
    """[TASK_LIST] или [TASK_LIST: all|open|blocked|done]"""
    try:
        from genesis_agent import workspace_memory as wm
    except Exception as e:
        return f"[TASK_LIST] workspace_memory не е наличен: {e}"
    status = (arg or "open").strip().lower() or "open"
    rows = wm.list_threads(status, 30)
    if not rows:
        return f"[TASK_LIST] Няма нишки със статус «{status}»."
    lines = [f"[TASK_LIST: {status}]"]
    for t in rows:
        nxt = f" → СЛЕДВА: {t['next_step']}" if t["next_step"] else ""
        lines.append(f"  #{t['id']} [{t['status']}] {t['title']}{nxt}")
    return "\n".join(lines)


def _tool_delegate(arg: str) -> str:
    goal = arg.strip()
    try:
        from genesis_agent.delegate import delegate_task, wait_all
    except Exception as e:
        return f"[DELEGATE: {goal}] delegate модулът не е наличен: {e}"
    task = delegate_task(goal, agent="autonomous", timeout=600)
    wait_all([task], timeout=600)
    _log_episode(f"DELEGATE {goal}", task.status.value, ["tool", "delegate"])
    detail = task.result or task.error or "(без резултат)"
    return f"[DELEGATE: {goal}]  статус={task.status.value}\n{detail}"


# ─────────────────────────────────────────────────────────────────────────────
# Парсване
# ─────────────────────────────────────────────────────────────────────────────

# WRITE_FILE е специален (има тяло до [END_WRITE]); останалите са едноредови.
_WRITE_RE = re.compile(r"\[WRITE_FILE:\s*(?P<path>[^\]]+)\](?P<body>.*?)\[END_WRITE\]",
                       re.DOTALL)
# USE_SKILL е също двучастен — умение + опционален driver код до [END_USE_SKILL].
_USE_SKILL_RE = re.compile(r"\[USE_SKILL:\s*(?P<name>[^\]]+)\](?P<body>.*?)\[END_USE_SKILL\]",
                           re.DOTALL)
_SIMPLE_RE = re.compile(
    r"\[(?P<tool>READ_FILE|RUN_CMD|WEB_SEARCH|LIST_DIR|DELEGATE|RESEARCH|BROWSE|ASK_USER|"
    r"BROWSER_CLICK|BROWSER_TYPE|REMEMBER|TASK_ADD|TASK_UPDATE|TASK_LIST):"
    r"\s*(?P<arg>[^\]]+)\]"
)
# BROWSER_READ е без аргумент (като LOOK_AT_SCREEN) — отделен pattern.
_BROWSER_READ_RE = re.compile(r"\[BROWSER_READ\]")
# TASK_LIST без аргумент — най-честата форма ([TASK_LIST] = отворените нишки).
_TASK_LIST_RE = re.compile(r"\[TASK_LIST\]")

_SIMPLE_DISPATCH = {
    "READ_FILE": _tool_read_file,
    "RUN_CMD": _tool_run_cmd,
    "ASK_USER": _tool_ask_user,
    "WEB_SEARCH": _tool_web_search,
    "LIST_DIR": _tool_list_dir,
    "DELEGATE": _tool_delegate,
    "RESEARCH": _tool_research,
    "BROWSE": _tool_browse,
    "BROWSER_CLICK": _tool_browser_click,
    "BROWSER_TYPE": _tool_browser_type,
    "REMEMBER": _tool_remember,
    "TASK_ADD": _tool_task_add,
    "TASK_UPDATE": _tool_task_update,
    "TASK_LIST": _tool_task_list,
}


_READONLY_RE = re.compile(
    r"\[(?P<tool>READ_FILE|WEB_SEARCH|LIST_DIR|RESEARCH):\s*(?P<arg>[^\]]+)\]"
)
_READONLY_DISPATCH = {
    "READ_FILE": _tool_read_file,
    "WEB_SEARCH": _tool_web_search,
    "RESEARCH": _tool_research,
    "LIST_DIR": _tool_list_dir,
}
# LOOK_AT_SCREEN е с ОПЦИОНАЛЕН аргумент ([LOOK_AT_SCREEN] или с въпрос) —
# различен pattern от другите read-only тулове, които изискват ":arg]".
_VISION_RE = re.compile(r"\[LOOK_AT_SCREEN(?::\s*(?P<arg>[^\]]*))?\]")


def parse_and_execute_readonly_tools(response_text: str) -> list[str]:
    """
    Изпълнява САМО безопасните read-only инструменти (READ_FILE/WEB_SEARCH/
    LIST_DIR/LOOK_AT_SCREEN). За автономния цикъл — Brain-ът може да събере
    информация по време на мисия, без риск от RUN_CMD/WRITE_FILE/DELEGATE.
    Връща списък от резултати.
    """
    if not response_text:
        return []
    results: list[tuple[int, str]] = []
    for m in _READONLY_RE.finditer(response_text):
        fn = _READONLY_DISPATCH[m.group("tool")]
        results.append((m.start(), fn(m.group("arg"))))
    for m in _VISION_RE.finditer(response_text):
        results.append((m.start(), _tool_look_at_screen(m.group("arg") or "")))
    results.sort(key=lambda t: t[0])
    return [r for _, r in results]


def parse_and_execute_tools(response_text: str) -> list[str]:
    """
    Извлича и изпълнява всички тул-тагове в реда, в който се появяват.
    Връща списък от резултати (по един низ на тул). Празен списък = няма тулове.
    """
    if not response_text:
        return []

    results: list[str] = []
    consumed_spans: list[tuple[int, int]] = []

    # 1. WRITE_FILE блокове (с тяло).
    for m in _WRITE_RE.finditer(response_text):
        results.append((m.start(), _tool_write_file(m.group("path"), m.group("body"))))
        consumed_spans.append((m.start(), m.end()))

    # 1b. USE_SKILL блокове (умение + опционален driver код).
    for m in _USE_SKILL_RE.finditer(response_text):
        results.append((m.start(), _tool_use_skill(m.group("name"), m.group("body"))))
        consumed_spans.append((m.start(), m.end()))

    # 2. Едноредови тулове — прескачаме тези вътре във WRITE_FILE блок.
    def _inside_write(pos: int) -> bool:
        return any(s <= pos < e for s, e in consumed_spans)

    for m in _SIMPLE_RE.finditer(response_text):
        if _inside_write(m.start()):
            continue
        fn = _SIMPLE_DISPATCH[m.group("tool")]
        results.append((m.start(), fn(m.group("arg"))))

    # 3. BROWSER_READ — без аргумент (като LOOK_AT_SCREEN).
    for m in _BROWSER_READ_RE.finditer(response_text):
        if _inside_write(m.start()):
            continue
        results.append((m.start(), _tool_browser_read()))

    # 4. TASK_LIST без аргумент — [TASK_LIST] показва отворените нишки.
    for m in _TASK_LIST_RE.finditer(response_text):
        if _inside_write(m.start()):
            continue
        results.append((m.start(), _tool_task_list("open")))

    # Подреждаме по позиция в текста, връщаме само низовете.
    results.sort(key=lambda t: t[0])
    return [r for _, r in results]


# ─────────────────────────────────────────────────────────────────────────────
# Native tool-calling dispatch
# ─────────────────────────────────────────────────────────────────────────────
# Същите _tool_* имплементации като регекс-таговете по-горе, но извикани със
# структурирани args (от истински OpenAI tool_calls JSON) вместо от парснат
# текст — за модели, потвърдени да поддържат native function-calling
# (config.yaml supports_tools). Двата пътя никога не се разминават в
# ПОВЕДЕНИЕ, само в това как аргументите стигат до тях.

def dispatch_tool_call(name: str, arguments) -> str:
    """Изпълнява един native tool_call. Никога не хвърля — грешка връща като низ,
    така че цикълът може да я подаде обратно на модела и той да опита пак.

    `arguments` приема и dict, и суровия JSON низ, който OpenAI API-то реално
    връща — всички текущи callers правят json.loads() сами, но подаването на
    низ иначе гърми с неясното "'str' object has no attribute 'get'"."""
    if isinstance(arguments, str):
        import json as _json
        try:
            arguments = _json.loads(arguments or "{}")
        except (ValueError, TypeError):
            return f"[{name}] Невалидни аргументи (не са валиден JSON): {arguments[:200]}"
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        if name == "READ_FILE":
            return _tool_read_file(arguments.get("path", ""))
        if name == "WRITE_FILE":
            return _tool_write_file(arguments.get("path", ""), arguments.get("content", ""))
        if name == "RUN_CMD":
            return _tool_run_cmd(arguments.get("command", ""))
        if name == "ASK_USER":
            return _tool_ask_user(arguments.get("question", ""),
                                  arguments.get("options"))
        if name == "WEB_SEARCH":
            return _tool_web_search(arguments.get("query", ""))
        if name == "RESEARCH":
            return _tool_research(arguments.get("question", ""))
        if name == "LIST_DIR":
            return _tool_list_dir(arguments.get("path", ""))
        if name == "USE_SKILL":
            return _tool_use_skill(arguments.get("name_or_query", ""), arguments.get("driver_code", "") or "")
        if name == "DELEGATE":
            return _tool_delegate(arguments.get("goal", ""))
        if name == "BROWSE":
            return _tool_browse(arguments.get("url", ""))
        if name == "BROWSER_READ":
            return _tool_browser_read()
        if name == "BROWSER_CLICK":
            return _tool_browser_click(arguments.get("index_or_text", ""))
        if name == "BROWSER_TYPE":
            idx = arguments.get("index_or_text", "")
            text = arguments.get("text", "")
            return _tool_browser_type(f"{idx} | {text}")
        # Памет за работата — структурираните аргументи тук са по-надеждни от
        # "|"-разделения текстов формат, затова викаме workspace_memory директно.
        if name in ("REMEMBER", "TASK_ADD", "TASK_UPDATE", "TASK_LIST"):
            from genesis_agent import workspace_memory as wm
            if name == "REMEMBER":
                kind = str(arguments.get("kind", "decision")).lower()
                if kind.startswith(("pref", "предпочит")):
                    return wm.set_preference(arguments.get("topic", ""),
                                             arguments.get("value", ""))
                return wm.add_decision(arguments.get("value", "") or arguments.get("topic", ""),
                                       arguments.get("why", ""))
            if name == "TASK_ADD":
                return wm.add_thread(arguments.get("title", ""),
                                     arguments.get("next_step", ""))
            if name == "TASK_UPDATE":
                return wm.update_thread(arguments.get("id", ""),
                                        arguments.get("status", ""),
                                        arguments.get("next_step", ""))
            return _tool_task_list(arguments.get("status", "open"))
        return f"[{name}] Непознат tool."
    except Exception as e:
        return f"[{name}] Грешка при изпълнение: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Self-check
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    set_workspace(_PROJECT_ROOT)
    demo = (
        "Ще прочета README и ще пусна команда.\n"
        "[READ_FILE: README.md]\n"
        "[RUN_CMD: echo мост-работи && python3 -c 'print(2**10)']\n"
        "[LIST_DIR: genesis_agent]\n"
        "[WRITE_FILE: .sandbox_run/bridge_selftest.txt]здравей от моста[END_WRITE]\n"
        "[RUN_CMD: rm -rf /]\n"  # трябва да е BLOCKED
    )
    for i, out in enumerate(parse_and_execute_tools(demo), 1):
        print(f"\n─── тул {i} ─────────────────────────────")
        print(out[:400])
