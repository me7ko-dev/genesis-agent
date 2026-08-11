#!/usr/bin/env python3
"""
genesis_skills.py — Мостът между genesis_terminal_agent.py и genesis_agent/ ядрото.

Терминалният агент праща отговора на LLM тук; ние извличаме тул-таговете и ги
изпълняваме — но всяко реално изпълнение минава през genesis_agent.sandbox (SAFE/
CONFIRM/BLOCKED бариерата) и се записва в genesis_agent.memory / episodic_memory.

Поддържани тагове (както са описани в config.yaml system_prompt):
    [READ_FILE: /път/до/файл]  или  [READ_FILE: /път | offset | limit]  (диапазон от редове)
    [GLOB: шаблон]  или  [GLOB: шаблон | /път]  — намира файлове по ИМЕ (rglob), не по съдържание
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
from collections.abc import Callable
from pathlib import Path

# Уверяваме се, че genesis_agent/ пакетът е импортируем (мостът стои в root-а).
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from genesis_agent import sandbox

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

# Пътища, за които моделът вече е видял РЕАЛНОТО съдържание в тази сесия —
# чрез READ_FILE, или защото самият той току-що го записа/редактира (design
# note, 2026-08-11). Пази WRITE_FILE от тихо унищожаване на файл, чието
# съдържание моделът никога не е видял: досега единствената защита беше ред в
# промпта ("EDIT_FILE, NOT WRITE_FILE... only for a file you are genuinely
# creating") — точно класът бъг, който проектът навсякъде другаде (malformed
# tag, unverified completion claim, tautological assert, stall nudge) вече
# третира като механизъм, а не молба към модела. Нарочно НЯМА bypass флаг:
# цената на "прочети първо" е един евтин tool call, а изключение би обезсмислило
# гаранцията точно за случая, в който тя има значение.
_SEEN_PATHS: set[Path] = set()


def set_workspace(path) -> None:
    """Задава работната директория (вика се от genesis_terminal_agent.py)."""
    global _WORKSPACE
    _WORKSPACE = Path(path)
    _SEEN_PATHS.clear()


def _resolve(path_str: str) -> Path:
    """Разрешава път — относителните са спрямо workspace-а."""
    p = Path(path_str.strip()).expanduser()
    return p if p.is_absolute() else (_WORKSPACE / p)


def _strip_one_newline(part: str) -> str:
    """Маха ЕДИН водещ и ЕДИН завършващ нов ред от част на EDIT_FILE блок.

    Само по един: тагът и разделителят са на собствени редове, така че точно
    един \\n от всяка страна принадлежи на синтаксиса, а не на кода. По-агресивен
    strip() би изял отстъпа на първия ред и anchor-ът никога не би съвпаднал.
    """
    part = part.removeprefix("\n")
    part = part.removesuffix("\n")
    return part


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

def _tool_read_file(arg: str, offset=None, limit=None) -> str:
    """Чете файл. Без offset/limit: първите 8000 символа (старото поведение,
    непроменено за малки файлове). С тях: конкретен диапазон от РЕДОВЕ.

    Защо изобщо: твърдото отрязване на 8000 символа означаваше, че всичко
    след тази граница беше буквално невидимо за модела — а EDIT_FILE изисква
    точен anchor от СЪЩЕСТВУВАЩИЯ текст. За файл над ~150 реда моделът не
    можеше НИКОГА да построи валидна редакция отвъд началото, независимо
    колко пъти опиташе. Текстовият таг приема двата параметъра pipe-разделени
    (както BROWSER_TYPE/TASK_UPDATE): `[READ_FILE: път | offset | limit]`.
    """
    parts = [p.strip() for p in arg.split("|")] if "|" in arg else [arg]
    path_str = parts[0]
    if offset is None and len(parts) > 1 and parts[1]:
        offset = parts[1]
    if limit is None and len(parts) > 2 and parts[2]:
        limit = parts[2]
    try:
        offset_i = int(offset) if offset not in (None, "") else None
    except (TypeError, ValueError):
        offset_i = None
    try:
        limit_i = int(limit) if limit not in (None, "") else None
    except (TypeError, ValueError):
        limit_i = None

    path = _resolve(path_str)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"[READ_FILE] Файлът не съществува: {path}"
    except OSError as e:
        return f"[READ_FILE] Грешка: {e}"
    _SEEN_PATHS.add(path.resolve())

    if offset_i is not None or limit_i is not None:
        lines = text.splitlines()
        start = max(0, (offset_i or 1) - 1)
        end = start + limit_i if limit_i else len(lines)
        # Номерирани редове (както `cat -n`) — тук, за разлика от режима по
        # подразбиране, точната позиция е ЦЯЛАТА цел на извикването: моделът
        # е поискал точно този диапазон, защото гради EDIT_FILE anchor или
        # обяснява нещо на потребителя по конкретен ред.
        numbered = [f"{i:>5}\t{ln}" for i, ln in enumerate(lines[start:end], start + 1)]
        chunk = "\n".join(numbered)
        if len(chunk) > 8000:
            chunk = chunk[:8000] + f"\n… [отрязано, диапазонът е по-голям от 8000 символа]"
        _log_episode(f"READ_FILE {path}", "прочетен (диапазон)", ["tool", "read_file"])
        return (f"[READ_FILE: {path}]  (редове {start + 1}-{min(end, len(lines))} от {len(lines)})\n"
                + chunk)

    if len(text) > 8000:
        total_lines = text.count("\n") + 1
        text = (text[:8000]
                + f"\n… [отрязано, общо {len(text)} символа, {total_lines} реда — "
                  f"за конкретен диапазон: READ_FILE: {path_str} | offset | limit]")
    _log_episode(f"READ_FILE {path}", "прочетен", ["tool", "read_file"])
    return f"[READ_FILE: {path}]\n{text}"


def _tool_write_file(arg: str, content: str) -> str:
    path = _resolve(arg)
    resolved = path.resolve() if path.exists() else None
    # WRITE_FILE презаписва ЦЕЛИЯ файл. Върху нещо, което моделът никога не е
    # видяло в тази сесия, това е сляпо унищожаване на неизвестно съдържание —
    # досега единствената спирачка беше промпт текст ("EDIT_FILE, NOT
    # WRITE_FILE"), а не нещо, което кодът реално проверява (design note,
    # 2026-08-11, виж _SEEN_PATHS по-горе). Нарочно БЕЗ bypass: цената на
    # "прочети първо" е един евтин READ_FILE tool call.
    if path.is_file() and resolved not in _SEEN_PATHS:
        return (f"[WRITE_FILE: {path}] ❌ Файлът вече съществува и не си го чел в тази сесия — "
                "WRITE_FILE презаписва ЦЯЛОТО му съдържание. Прочети го първо с READ_FILE (за да "
                "видиш какво ще загубиш), или по-добре ползвай EDIT_FILE за частична промяна.")
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
    # Ruff pre-check преди диска, само за .py (design note, 2026-07-29): не
    # блокираме записа при unfixable проблеми (моделът изрично поиска точно
    # това съдържание) — но ако ruff го оправи автоматично, пишем ФИКСНАТАТА
    # версия, и ВИНАГИ показваме находките в резултата, за да се самокоригира
    # моделът в следващия рунд вместо да чака sandbox изпълнение да ги хване.
    lint_note = ""
    if path.suffix == ".py":
        from genesis_agent.code_validate import validate_code_with_ruff
        ok, detail = validate_code_with_ruff(content)
        if ok and detail:
            content = detail
        elif not ok:
            lint_note = f"\n{detail}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"[WRITE_FILE] Грешка: {e}"
    _SEEN_PATHS.add(path.resolve())
    _log_episode(f"WRITE_FILE {path}", f"записани {len(content)} символа",
                 ["tool", "write_file"])
    return f"[WRITE_FILE: {path}] ✓ записани {len(content)} символа{lint_note}"


def _tool_edit_file(path_arg: str, old: str, new: str, replace_all: bool = False) -> str:
    """Закотвена замяна в СЪЩЕСТВУВАЩ файл (виж genesis_agent/code_edit.py).

    Минава през същата CONFIRM бариера като WRITE_FILE при запис извън
    workspace-а — редакцията е по-малка по обхват, но не е по-малко реална.
    """
    path = _resolve(path_arg)
    try:
        inside = path.resolve().is_relative_to(_WORKSPACE.resolve())
    except (ValueError, OSError):
        inside = False
    if not inside:
        verdict = sandbox.RiskVerdict(sandbox.RiskLevel.CONFIRM,
                                      [f"редакция извън workspace: {path}"])
        allowed, reason = sandbox._decide(f"EDIT_FILE {path}", verdict, sandbox.get_policy())
        if not allowed:
            return f"[EDIT_FILE] {reason}"

    from genesis_agent.code_edit import edit_file
    res = edit_file(path, old, new, replace_all=replace_all)
    if not res.ok:
        _log_episode(f"EDIT_FILE {path}", f"отказана: {res.detail[:200]}",
                     ["tool", "edit_file", "rejected"])
        return f"[EDIT_FILE: {path}] ❌ {res.detail}"
    _SEEN_PATHS.add(path.resolve())
    _log_episode(f"EDIT_FILE {path}", res.detail, ["tool", "edit_file"])
    # Диффът се връща на модела нарочно: така следващият рунд вижда какво РЕАЛНО
    # се е променило, вместо да разчита на спомена си какво е искал да промени.
    diff = res.diff if len(res.diff) <= 4000 else res.diff[:4000] + "\n… [диффът е отрязан]"
    # Check-only ruff (design note, 2026-08-11): WRITE_FILE вече получаваше lint
    # обратна връзка, EDIT_FILE — не, макар да е ПРЕДПОЧИТАНИЯТ инструмент.
    # Нарочно без --fix тук (виж code_validate.lint_note) — auto-fix би пипнал
    # части от файла отвъд самата редакция, което чупи обещанието на EDIT_FILE.
    ruff_note = ""
    if path.suffix == ".py":
        try:
            from genesis_agent.code_validate import lint_note
            ruff_note = lint_note(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return f"[EDIT_FILE: {path}] {res.detail}\n{diff}{ruff_note}"


def _tool_search_code(arg: str, path: str = "", glob: str = "") -> str:
    pattern = arg.strip()
    if not pattern:
        return "[SEARCH_CODE] Празен шаблон."
    from genesis_agent.repo_map import search_code
    root = _resolve(path) if path else _WORKSPACE
    try:
        hits = search_code(pattern, root, glob or None)
    except (ValueError, FileNotFoundError) as e:
        return f"[SEARCH_CODE: {pattern}] {e}"
    if not hits:
        return (f"[SEARCH_CODE: {pattern}] Няма съвпадения в {root}. "
                "Това означава, че низът наистина го няма — не предполагай, че е скрит.")
    lines = [f"[SEARCH_CODE: {pattern}] {len(hits)} съвпадения в {root}"]
    lines += [f"  {h.path}:{h.line}: {h.text.strip()}" for h in hits]
    _log_episode(f"SEARCH_CODE {pattern}", f"{len(hits)} съвпадения", ["tool", "search_code"])
    return "\n".join(lines)


def _tool_glob(arg: str) -> str:
    """[GLOB: шаблон] или [GLOB: шаблон | път] — намира файлове по ИМЕ
    (напр. '**/*.tsx', 'test_*.py'), за разлика от SEARCH_CODE, което търси
    в СЪДЪРЖАНИЕТО. Ползва Path.rglob под капака (repo_map.find_files)."""
    parts = [p.strip() for p in arg.split("|")] if "|" in arg else [arg.strip(), ""]
    pattern, path = parts[0], (parts[1] if len(parts) > 1 else "")
    if not pattern:
        return "[GLOB] Празен шаблон."
    from genesis_agent.repo_map import find_files
    root = _resolve(path) if path else _WORKSPACE
    try:
        hits = find_files(pattern, root)
    except (ValueError, FileNotFoundError) as e:
        return f"[GLOB: {pattern}] {e}"
    if not hits:
        return f"[GLOB: {pattern}] Няма файлове по този шаблон в {root}."
    _log_episode(f"GLOB {pattern}", f"{len(hits)} файла", ["tool", "glob"])
    lines = [f"[GLOB: {pattern}] {len(hits)} файла в {root}"] + [f"  {h}" for h in hits]
    return "\n".join(lines)


def _tool_repo_map(arg: str = "") -> str:
    from genesis_agent.repo_map import repo_map
    root = _resolve(arg) if arg.strip() else _WORKSPACE
    try:
        out = repo_map(root)
    except OSError as e:
        return f"[REPO_MAP] Грешка: {e}"
    _log_episode(f"REPO_MAP {root}", "картиран", ["tool", "repo_map"])
    return out


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
    # Не `e` — точно това име държи изключението по-горе.
    for entry in entries[:200]:
        marker = "📁" if entry.is_dir() else "📄"
        lines.append(f"  {marker} {entry.name}")
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
# EDIT_FILE е тричастен — файл + anchor + замяна. Разделителят е дълъг и
# нетипичен нарочно: и двете половини са СУРОВ код, така че всичко по-късо
# (--- или ===) рано или късно се среща вътре в самия код и реже редакцията
# на грешното място.
_EDIT_SEPARATOR = "---GENESIS-REPLACE-WITH---"
_EDIT_RE = re.compile(r"\[EDIT_FILE:\s*(?P<path>[^\]]+)\](?P<body>.*?)\[END_EDIT\]",
                      re.DOTALL)
_SIMPLE_RE = re.compile(
    r"\[(?P<tool>READ_FILE|RUN_CMD|WEB_SEARCH|LIST_DIR|DELEGATE|RESEARCH|BROWSE|ASK_USER|"
    r"SEARCH_CODE|REPO_MAP|GLOB|"
    r"BROWSER_CLICK|BROWSER_TYPE|REMEMBER|TASK_ADD|TASK_UPDATE|TASK_LIST):"
    r"\s*(?P<arg>[^\]]+)\]"
)
# REPO_MAP без аргумент = текущият workspace (както BROWSER_READ/TASK_LIST).
_REPO_MAP_RE = re.compile(r"\[REPO_MAP\]")
# BROWSER_READ е без аргумент (като LOOK_AT_SCREEN) — отделен pattern.
_BROWSER_READ_RE = re.compile(r"\[BROWSER_READ\]")
# TASK_LIST без аргумент — най-честата форма ([TASK_LIST] = отворените нишки).
_TASK_LIST_RE = re.compile(r"\[TASK_LIST\]")

_SIMPLE_DISPATCH: dict[str, Callable[..., str]] = {
    "READ_FILE": _tool_read_file,
    "RUN_CMD": _tool_run_cmd,
    "ASK_USER": _tool_ask_user,
    "WEB_SEARCH": _tool_web_search,
    "LIST_DIR": _tool_list_dir,
    "DELEGATE": _tool_delegate,
    "RESEARCH": _tool_research,
    "SEARCH_CODE": _tool_search_code,
    "REPO_MAP": _tool_repo_map,
    "GLOB": _tool_glob,
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

    # (позиция_в_текста, изход) — сортира се по позиция, после се връщат само изходите.
    results: list[tuple[int, str]] = []
    consumed_spans: list[tuple[int, int]] = []

    # 1. WRITE_FILE блокове (с тяло).
    for m in _WRITE_RE.finditer(response_text):
        results.append((m.start(), _tool_write_file(m.group("path"), m.group("body"))))
        consumed_spans.append((m.start(), m.end()))

    # 1a. EDIT_FILE блокове (anchor + замяна, разделени с _EDIT_SEPARATOR).
    for m in _EDIT_RE.finditer(response_text):
        body = m.group("body")
        if _EDIT_SEPARATOR not in body:
            results.append((m.start(),
                            (f"[EDIT_FILE: {m.group('path').strip()}] ❌ Липсва разделителят "
                             f"{_EDIT_SEPARATOR} между стария и новия текст.")))
        else:
            old_part, new_part = body.split(_EDIT_SEPARATOR, 1)
            results.append((m.start(), _tool_edit_file(m.group("path"),
                                                       _strip_one_newline(old_part),
                                                       _strip_one_newline(new_part))))
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

    # 3b. REPO_MAP без аргумент — картира текущия workspace.
    for m in _REPO_MAP_RE.finditer(response_text):
        if _inside_write(m.start()):
            continue
        results.append((m.start(), _tool_repo_map("")))

    # 4. TASK_LIST без аргумент — [TASK_LIST] показва отворените нишки.
    for m in _TASK_LIST_RE.finditer(response_text):
        if _inside_write(m.start()):
            continue
        results.append((m.start(), _tool_task_list("open")))

    # Подреждаме по позиция в текста, връщаме само низовете.
    results.sort(key=lambda t: t[0])
    return [r for _, r in results]


# Всяко скоби-подобно "[ГЛАВНИ_БУКВИ:" или "[ГЛАВНИ_БУКВИ]" в текста — независимо
# дали е познат таг. Ползва се САМО след като parse_and_execute_tools вече е
# върнал [], за да различим "моделът приключи" от "моделът се опита да викне
# tool, но обърка синтаксиса/името" (грешно име, липсващо двоеточие, липсващ
# [END_WRITE] и т.н.) — вторият случай иначе тихо се третираше като финален
# отговор (виж git история: "Fix local model narrating tool use without ever
# executing" / "Fix Genesis handing work back instead of doing it").
_ATTEMPTED_TAG_RE = re.compile(r"\[[A-Z][A-Z_]{2,30}(?::|\])")


def looks_like_attempted_tool_tag(response_text: str) -> bool:
    """True ако текстът съдържа нещо, което прилича на tool tag, но нито един
    истински таг не е разпознат от parse_and_execute_tools за него."""
    if not response_text:
        return False
    return bool(_ATTEMPTED_TAG_RE.search(response_text))


# Правилото "NEVER report an action as done unless a tool result above actually
# shows it happening" (config.yaml system_prompt) досега съществуваше САМО като
# промпт текст — нищо в кода не проверяваше дали слаб модел реално го спазва.
# Списъкът е нарочно тесен (глаголи за завършено действие, минало време/
# perfect), за да не гърми на легитимни финални резюмета след РЕАЛНО изпълнени
# tool-ове ("инсталирах пакета" е ОК, ако предният рунд реално е викнал
# RUN_CMD — проверката по-долу се извиква само в рундове БЕЗ никакъв изпълнен
# tool в текущия разговор).
_COMPLETION_CLAIM_RE = re.compile(
    r"\b(инсталирах|преместих|изтрих|създадох|запазих|качих|конфигурирах|"
    r"поправих|инсталирано е|готово е|направено е|свършено е|"
    r"i(?:'ve| have) (?:installed|moved|created|deleted|saved|written|"
    r"uploaded|configured|fixed|updated)|"
    r"successfully (?:installed|moved|created|deleted|saved|updated|fixed))\b",
    re.IGNORECASE,
)


def looks_like_unverified_completion_claim(response_text: str) -> bool:
    """True ако текстът твърди, че действие е ИЗПЪЛНЕНО (инсталирах/преместих/
    done/installed...). Извиквай само в рундове без никакъв реален tool
    резултат зад отговора — иначе легитимни резюмета след истински изпълнени
    tool-ове ще фалшиво-положат."""
    if not response_text:
        return False
    return bool(_COMPLETION_CLAIM_RE.search(response_text))


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
            return _tool_read_file(arguments.get("path", ""),
                                   arguments.get("offset"), arguments.get("limit"))
        if name == "WRITE_FILE":
            return _tool_write_file(arguments.get("path", ""), arguments.get("content", ""))
        if name == "EDIT_FILE":
            return _tool_edit_file(arguments.get("path", ""),
                                   arguments.get("old", ""),
                                   arguments.get("new", ""),
                                   bool(arguments.get("replace_all", False)))
        if name == "SEARCH_CODE":
            return _tool_search_code(arguments.get("pattern", ""),
                                     arguments.get("path", "") or "",
                                     arguments.get("glob", "") or "")
        if name == "REPO_MAP":
            return _tool_repo_map(arguments.get("path", "") or "")
        if name == "GLOB":
            pattern = arguments.get("pattern", "")
            path = arguments.get("path", "") or ""
            return _tool_glob(f"{pattern} | {path}" if path else pattern)
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
