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

import json
import logging
import os
import re
import stat
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path

# Уверяваме се, че genesis_agent/ пакетът е импортируем (мостът стои в root-а).
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from genesis_agent import sandbox
from genesis_agent.code_edit import decode_text, displayable, encode_text, has_raw_bytes
from genesis_agent.tool_schemas import load_tool_arguments, open_block_tag

log = logging.getLogger("genesis.skills")

# Паметта/уеб търсенето са meko-опционални — ако липсва зависимост (напр.
# scikit-learn), мостът пак работи, само без логване/търсене.
try:
    from genesis_agent.memory import memory_record_episode
except Exception:  # pragma: no cover
    log.debug("паметта не се зареди — епизодите не се записват", exc_info=True)
    memory_record_episode = None  # type: ignore

try:
    from genesis_agent import web_search
except Exception:  # pragma: no cover
    log.debug("web_search не се зареди", exc_info=True)
    web_search = None  # type: ignore

try:
    from genesis_agent import browser as _browser_mod
except Exception:  # pragma: no cover
    log.debug("браузърът не се зареди", exc_info=True)
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

# Докато работи под-агент (genesis_agent.agents): името му и инструментите,
# които има. Проверява се в общата бариера _before_tool — и за текстовите
# тагове, и за native извикванията, — а не само в списъка схеми, който моделът
# вижда: модел, който напише таг извън списъка си, иначе би го изпълнил.
_AGENT_SCOPE: tuple[str, frozenset[str]] | None = None


def _agent_scope() -> tuple[str, frozenset[str]] | None:
    return _AGENT_SCOPE


def _set_agent_scope(scope: tuple[str, frozenset[str]] | None) -> None:
    global _AGENT_SCOPE
    _AGENT_SCOPE = scope


# „Стоп“ на оператора (бутонът на телефона) за текущия ход. Проверява се преди
# ВСЕКИ инструмент — и в под-агентите, и по двата пътя. Дотогава се гледаше
# само веднъж на рунд: след „Стоп“ рундът довършваше всичките си записи, а
# под-агентът — всичките си 15 рунда (одит 2026-10-09).
_CANCEL: Callable[[], bool] | None = None


def _set_cancel(check: Callable[[], bool] | None) -> None:
    global _CANCEL
    _CANCEL = check
    # И за вече работеща команда, не само преди следващия инструмент (2026-10-11).
    sandbox.cancel_check = check


def _cancelled() -> bool:
    try:
        return bool(_CANCEL and _CANCEL())
    except Exception:
        log.debug("проверката за спиране се провали", exc_info=True)
        return False


def set_workspace(path) -> None:
    """Задава работната директория (вика се от genesis_terminal_agent.py)."""
    global _WORKSPACE
    _WORKSPACE = Path(path)
    _SEEN_PATHS.clear()
    try:  # паметта за работата (нишки, решения) е на същата папка
        from genesis_agent import workspace_memory
        workspace_memory.set_workspace(path)
    except Exception:
        log.debug("паметта за работата не смени папката", exc_info=True)


def _resolve(path_str: str) -> Path:
    """Разрешава път — относителните са спрямо workspace-а."""
    return _resolve_noted(path_str)[0]


def _resolve_noted(path_str: str, *, redirect: bool = True) -> tuple[Path, str]:
    """Като _resolve, но абсолютен път с правописна грешка в пътя до
    workspace-а се пренасочва в него — и бележката го казва на модела.

    NEXT_STEPS В.9: моделът пише `...\\Projects\\genittest\\egn.py` при
    workspace `...\\Projects\\genitest`; sandbox-ът правилно спира записа
    извън workspace-а, но рундът се губи, а следващият често повтаря същото.
    Пренасочва се САМО когато папката с грешката не съществува и всяка част
    до нея е буквена грешка на частта от workspace-а (виж _is_typo) — не
    `genitest2` или `genitest-old`, които може да са нарочна съседна папка.

    `redirect=False` (запис и редакция): пътят остава какъвто е — записът
    извън workspace-а пита както винаги, а бележката казва кой е вероятният
    път. Одит 2026-10-07: workspace `api-v1`, WRITE_FILE `…/api-v2/app.py` се
    пренасочваше и ПРЕЗАПИСВАШЕ `api-v1/app.py` без въпрос — запис по догадка
    не бива да минава покрай потвърждението.
    """
    p = Path(path_str.strip()).expanduser()
    if not p.is_absolute():
        return _WORKSPACE / p, ""
    fixed = _typo_into_workspace(p)
    if fixed is None:
        return p, ""
    if not redirect:
        return p, (f"\n[път] {p} изглежда като workspace-а с правописна грешка — "
                   f"вероятно имаше предвид {fixed}. Пиши пътищата спрямо workspace-а "
                   "(относителни).")
    return fixed, (f"\n[път] {p} не съществува — това е workspace-ът с правописна "
                   f"грешка, ползван е {fixed}. Пиши пътищата спрямо workspace-а "
                   "(относителни), не абсолютни.")


def _is_typo(a: str, b: str) -> bool:
    """Две имена на папки, които са една и съща с буквена грешка.

    Разлика само в регистъра, или разстояние ≤2 (вмъкване, изтриване, замяна,
    размяна на съседни) при имена от ≥4 знака. НЕ е грешка, ако едното е
    продължение на другото (`app` → `app2`, `site` → `site-old`): това е друга
    папка със смисъл, а не изпусната буква.
    """
    if a == b:
        return True
    # Различни числа са различни неща, не грешка: `api-v1`/`api-v2`,
    # `report2025`/`report2026`, `proj1`/`proj2`.
    if re.sub(r"\D", "", a) != re.sub(r"\D", "", b):
        return False
    if a.lower() == b.lower():
        return True
    if min(len(a), len(b)) < 4:
        return False
    la, lb = a.lower(), b.lower()
    if la.startswith(lb) or lb.startswith(la) or la.endswith(lb) or lb.endswith(la):
        return False
    return _edit_distance(la, lb, limit=2) <= min(2, max(len(a), len(b)) // 4)


def _edit_distance(a: str, b: str, limit: int) -> int:
    """Damerau-Levenshtein (с размяна на съседни), спира над `limit`."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def _typo_into_workspace(p: Path) -> Path | None:
    """Пътят в workspace-а, който `p` очевидно е имал предвид — или None."""
    if p.exists():
        return None
    try:
        ws = _WORKSPACE.resolve()
    except OSError:
        return None
    anchor = p.parent
    while not anchor.exists():
        if anchor.parent == anchor:
            return None
        anchor = anchor.parent
    try:
        ws_rest = ws.relative_to(anchor.resolve()).parts
        rest = p.relative_to(anchor).parts
    except (ValueError, OSError):
        return None
    if not ws_rest or len(rest) <= len(ws_rest):
        return None
    if not all(_is_typo(a, b) for a, b in zip(rest, ws_rest)):
        return None
    return ws.joinpath(*rest[len(ws_rest):])


# Папки, в които „може би имаше предвид“ не търси: огромни и никога не са
# мястото, където моделът мисли, че е файлът му.
_SKIP_DIRS = {".git", ".hg", ".venv", "venv", "node_modules", "__pycache__",
              ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build"}


def _did_you_mean(path: Path, *, limit: int = 3) -> str:
    """Подсказка за липсващ файл: същото име другаде в workspace-а или
    близко име в същата папка. Празно, ако няма нищо правдоподобно.

    Без нея „Файлът не съществува“ води до GLOB/LIST_DIR рунд само за да се
    намери, че файлът е в `src/`, или че се казва `utils.py`, а не `util.py`.
    """
    import difflib
    import os
    found: list[str] = []
    try:
        ws = _WORKSPACE.resolve()
        seen = 0
        for dirpath, dirnames, filenames in os.walk(ws):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            seen += len(filenames)
            if path.name in filenames:
                cand = Path(dirpath) / path.name
                if cand != path:
                    found.append(str(cand.relative_to(ws)))
            if seen > 5000 or len(found) >= limit:
                break
        # Съседите се изброяват само в workspace-а и никога в папка с ключове:
        # „Може би: ~/.ssh/id_ed25519_work_github?“ издаваше имената им.
        parent_ok = (path.parent.is_dir() and path.parent.resolve().is_relative_to(ws)
                     and not sandbox.sensitive_path_reason(path.parent.as_posix() + "/"))
        if len(found) < limit and parent_ok:
            names = [e.name for e in path.parent.iterdir()][:2000]
            for n in difflib.get_close_matches(path.name, names, n=limit, cutoff=0.75):
                cand = path.parent / n
                rel = str(cand.relative_to(ws)) if cand.is_relative_to(ws) else str(cand)
                if rel not in found:
                    found.append(rel)
    except OSError:
        return ""
    if not found:
        return ""
    return " Може би: " + ", ".join(found[:limit]) + "?"


def _sensitive_root_refusal(tool: str, root: Path) -> str | None:
    """Отказът, ако този КОРЕН е чувствителен — иначе None.

    Общо за SEARCH_CODE и GLOB, защото са две имена на един и същ въпрос:
    „покажи ми какво има там". Измерено преди това, в режим „deny":
    `SEARCH_CODE PRIVATE-KEY-BODY | ~/.ssh` връщаше самия ред от `id_rsa`.
    Не имена — СЪДЪРЖАНИЕ, тоест същото, което READ_FILE вече отказва.

    Разделителят накрая е нужен, защото образецът пази `.ssh/` и `.aws/` с
    наклонена черта — виж sandbox.sensitive_path_reason.
    """
    sensitive = sandbox.sensitive_path_reason(root.as_posix() + "/")
    if not sensitive:
        return None
    verdict = sandbox.RiskVerdict(sandbox.RiskLevel.CONFIRM, [sensitive])
    allowed, reason = sandbox._decide(f"{tool} {root}", verdict, sandbox.get_policy())
    return None if allowed else f"[{tool}] {reason}"


def _strip_one_newline(part: str) -> str:
    """Маха ЕДИН водещ и ЕДИН завършващ нов ред от част на EDIT_FILE блок.

    Само по един: тагът и разделителят са на собствени редове, така че точно
    един \\n от всяка страна принадлежи на синтаксиса, а не на кода. По-агресивен
    strip() би изял отстъпа на първия ред и anchor-ът никога не би съвпаднал.

    Новият ред може да е и CRLF (одит 2026-10-22): с `\\r\\n` в отговора котвата
    започваше с `\\n` и свършваше с `\\r` — „Anchor-ът не е намерен“ срещу
    обикновен LF файл, докато native EDIT_FILE и текстовият WRITE_FILE го приемат.
    """
    part = part.removeprefix("\r\n") if part.startswith("\r\n") else part.removeprefix("\n")
    part = part.removesuffix("\r\n") if part.endswith("\r\n") else part.removesuffix("\n")
    return part


def _log_episode(goal: str, outcome: str, tags: list[str]) -> None:
    if memory_record_episode is None:
        return
    try:
        memory_record_episode(goal=goal, outcome=outcome[:2000],
                              skill_path="genesis_skills.bridge", tags=tags)
    except Exception:
        # логването никога не бива да чупи изпълнението
        log.debug("епизодът не се записа", exc_info=True)


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

    path, redirect = _resolve_noted(path_str)
    # Четенето на ключове минава през СЪЩОТО решение като `cat ~/.genesis/.env`
    # (design note, 2026-09-20). Дотук не минаваше през нищо: shell пътят беше
    # с гейт, а инструментът — не, тоест по-лесният път беше отворен. А
    # съдържанието не остава на машината: то влиза в историята и се праща на
    # следващия доставчик във веригата, който при тази конфигурация е чужда,
    # безплатна услуга. SECURITY.md обещава точно обратното.
    sensitive = sandbox.sensitive_path_reason(path)
    if sensitive:
        verdict = sandbox.RiskVerdict(sandbox.RiskLevel.CONFIRM, [sensitive])
        allowed, reason = sandbox._decide(f"READ_FILE {path}", verdict, sandbox.get_policy())
        if not allowed:
            return f"[READ_FILE] {reason}"
    # Преди четенето: на Windows папка дава PermissionError, не IsADirectoryError,
    # и моделът виждаше „Permission denied“ (CI на Windows, 2026-10-07).
    if path.is_dir():
        return f"[READ_FILE] {path} е папка, не файл — ползвай LIST_DIR."
    # Одит 2026-10-11: READ_FILE четеше ЦЕЛИЯ файл, за да покаже 8000 знака.
    # FIFO в workspace-а висеше вечно (Ctrl-C затваря чата), /dev/zero изяждаше
    # паметта, а лог от 300 MB струваше ~600–800 MB RSS — и с offset/limit.
    # Затова: само обикновен файл (както mentions._read) и четене на парчета.
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return f"[READ_FILE] Файлът не съществува: {path}.{_did_you_mean(path)}"
    except OSError as e:
        return f"[READ_FILE] Грешка: {e}"
    if not stat.S_ISREG(st.st_mode):
        return (f"[READ_FILE] {path} не е обикновен файл (FIFO, устройство или сокет) — "
                "не се чете: четенето би чакало вечно или нямало край.")
    try:
        with open(path, "rb") as fh:
            head = fh.read(_READ_HEAD)
            whole = len(head) >= st.st_size
            # Двоичен файл (PNG, zip, sqlite) излизаше като 8000 знака боклук
            # (одит 2026-10-11); @mentions вече го отказваше по същия признак.
            # UTF-16/32 с BOM носят NUL, но са текст — decode_text ги чете.
            text, encoding = decode_text(head, partial=not whole)
            if not whole and encoding in ("utf-8", "cp1251", "cp1252") and st.st_size <= _STYLE_PROBE:
                # Кодирането — по целия файл, както SEARCH_CODE и WRITE_FILE
                # (ревю 2026-10-11): ASCII в първите 32 KB и cp1251 след тях
                # даваше „�“ в READ_FILE с offset, а SEARCH_CODE — текста.
                # И обратно (2026-10-15): latin-1 начало и UTF-8 след него е
                # UTF-8 файл — „cp1252“ тук значеше, че READ→WRITE пише „é“
                # от началото като C3 A9.
                whole_enc = decode_text(head + fh.read())[1]
                fh.seek(len(head))
                if whole_enc != encoding:
                    encoding = whole_enc
                    text = head.decode(encoding, "surrogateescape")
            if encoding in ("utf-8", "cp1251", "cp1252") and b"\x00" in head[:8192]:
                return (f"[READ_FILE] {path} е двоичен файл ({st.st_size} байта, "
                        f"{path.suffix or 'без разширение'}) — не се показва. За "
                        "съдържанието ползвай RUN_CMD с подходящ инструмент "
                        "(file, unzip -l, sqlite3).")
            enc_note = "" if encoding == "utf-8" else f"  (кодиране {encoding})"
            if offset_i is not None or limit_i is not None:
                return _read_range(fh, path, encoding, offset_i, limit_i,
                                   enc_note, redirect)
            total_lines = None
            if not whole and st.st_size <= _COUNT_LINES_MAX:
                total_lines = head.count(b"\n") + _count_rest(fh)
    except OSError as e:
        return f"[READ_FILE] Грешка: {e}"
    # Кодирането се разпознава (code_edit.decode_text) и се казва в заглавието:
    # cp1251 файл се показваше като „�������“, а моделът копираше това в
    # WRITE_FILE и кирилицата изчезваше от диска (одит 2026-10-11).
    # Самотен `\r` (стар Mac) остава `\r`: превърнат в `\n`, READ→WRITE
    # пренаписваше байтовете на файла (WRITE_FILE връща само чист CRLF), а
    # диапазонът, SEARCH_CODE и прегледът преди WRITE_FILE виждаха един ред
    # (ревю 2026-10-11, трета вълна) — един изглед за всички е по-важен.
    text = displayable(text).replace("\r\n", "\n")
    _SEEN_PATHS.add(path.resolve())
    if whole and not text.strip("\ufeff"):
        text = "(празен файл)"
    if len(text) > 8000 or not whole:
        if whole:
            total_lines = text.count("\n") + 1
        lines_note = f", {total_lines} реда" if total_lines else ""
        text = (text[:8000]
                + f"\n… [отрязано, общо {st.st_size} байта{lines_note} — "
                  f"за конкретен диапазон: READ_FILE: {path_str} | offset | limit]")
    _log_episode(f"READ_FILE {path}", "прочетен", ["tool", "read_file"])
    return f"[READ_FILE: {path}]{enc_note}\n{text}{redirect}"


# Началото на файла, което READ_FILE чете по подразбиране: 8000 знака са
# най-много 32000 байта (UTF-32), така че отрязването е по знаци, не по байтове.
_READ_HEAD = 32_768
# Над това броят на редовете в бележката не се смята — само байтовете.
_COUNT_LINES_MAX = 512 * 1024 * 1024
# Таван за едно парче от ред: минифициран файл от един ред не влиза цял в паметта.
_LINE_PIECE = 1 << 20


def _count_rest(fh) -> int:
    """Редовете в остатъка на `fh` — на парчета, без да се делят."""
    n, last = 0, b"\n"
    while chunk := fh.read(_LINE_PIECE):
        n += chunk.count(b"\n")
        last = chunk[-1:]
    return n + (last != b"\n")


def _iter_lines(fh):
    """Редовете от началото като байтове — делени САМО по `\n` (както rg и
    EDIT_FILE), с най-много _LINE_PIECE байта от всеки."""
    fh.seek(0)
    while piece := fh.readline(_LINE_PIECE):
        line = piece
        while not piece.endswith(b"\n"):
            piece = fh.readline(_LINE_PIECE)
            if not piece:
                break
        yield line


def _read_range(fh, path: Path, encoding: str, offset_i: int | None,
                limit_i: int | None, enc_note: str, redirect: str) -> str:
    """READ_FILE с offset/limit, без да държи файла в паметта.

    Номерата са по `\n`, както в SEARCH_CODE и EDIT_FILE: str.splitlines()
    броеше и \f, \x1c, U+2028 за край на ред, и READ_FILE ред 5 беше друг ред
    (одит 2026-10-11). UTF-16/32 не се делят по байт `\n` — тях ги декодираме
    цели (рядкост, и таванът е _STYLE_PROBE).
    """
    start = max(0, (offset_i or 1) - 1)
    end = start + limit_i if limit_i else None
    picked: list[str] = []
    size = 0
    total = 0
    wide = encoding.startswith("utf-") and encoding != "utf-8"
    if wide:
        fh.seek(0)
        text = decode_text(fh.read(_STYLE_PROBE), partial=True)[0]
        lines = [ln + "\n" for ln in text.split("\n")]
        if lines and lines[-1] == "\n":
            lines.pop()
        source = iter([ln.encode(encoding) for ln in lines])
    else:
        source = _iter_lines(fh)
    for raw in source:
        total += 1
        if total <= start:
            continue
        if end is not None and total > end:
            # Останалите редове само се броят — на парчета, без декодиране.
            total += sum(1 for _ in source) if wide else _count_rest(fh)
            break
        if size <= 8000:
            line = displayable(raw.decode(encoding, "surrogateescape"))
            line = line.removesuffix("\n").removesuffix("\r")
            picked.append(f"{total:>5}\t{line}")
            size += len(picked[-1]) + 1
    _SEEN_PATHS.add(path.resolve())
    if start >= total:
        return (f"[READ_FILE: {path}] Файлът има {total} реда — offset {start + 1} е след "
                f"края. Прочети без offset или с offset ≤ {max(total, 1)}.{redirect}")
    # Номерирани редове (както `cat -n`) — тук, за разлика от режима по
    # подразбиране, точната позиция е ЦЯЛАТА цел на извикването: моделът
    # е поискал точно този диапазон, защото гради EDIT_FILE anchor или
    # обяснява нещо на потребителя по конкретен ред.
    chunk = "\n".join(picked)
    if len(chunk) > 8000:
        chunk = chunk[:8000] + "\n… [отрязано, диапазонът е по-голям от 8000 символа]"
    _log_episode(f"READ_FILE {path}", "прочетен (диапазон)", ["tool", "read_file"])
    last = total if end is None else min(end, total)
    return (f"[READ_FILE: {path}]  (редове {start + 1}-{last} от {total}){enc_note}\n"
            + chunk + redirect)


# Колко от съществуващ файл се чете, за да се познае кодирането му преди
# WRITE_FILE — колкото и таванът на SEARCH_CODE (по-голям файл е генериран).
_STYLE_PROBE = 2_000_000


# Буквални `\n` вместо нови редове (bench_all 2026-10-12, код f8f08d7): gpt-oss
# и nemotron понякога пишат кода с текста „\n“ там, където е новият ред — 8 .py
# файла в 4 проекта (предния ден 0). Записът минаваше с „✓“: ruff го няма в PATH
# на лаптопа и проверката по-долу тихо се пропуска; pytest падаше при събирането
# и faktura-excel свърши 0/1, а моделът четеше теста, не счупения файл. Поправя
# се само когато без поправката файлът не е Python, а с нея е; иначе моделът
# поне научава веднага кой ред не е наред.
_LITERAL_NEWLINES_MIN = 3


def _literal_newlines_outside_strings(code: str) -> str:
    """`\\n` извън низовете → нов ред. Извън низ `\\` е само продължение на реда,
    затова там буквалното „\\n“ е изгубен нов ред; в низа (`r"[^\\n]+"`) е код —
    заместване навсякъде чупеше точно него (faktura-excel-1/extract.py)."""
    out: list[str] = []
    i, n = 0, len(code)
    quote = ""        # отвореният низ: ', ", ''' или """
    while i < n:
        ch = code[i]
        if quote:
            if ch == "\\":
                out.append(code[i:i + 2])
                i += 2
                continue
            if code.startswith(quote, i):
                out.append(quote)
                i += len(quote)
                quote = ""
                continue
            if ch == "\n" and len(quote) == 1:   # незатворен низ свършва с реда
                quote = ""
            out.append(ch)
            i += 1
            continue
        for lost in ("\\r\\n", "\\n"):
            if code.startswith(lost, i):
                out.append("\n")
                i += len(lost)
                break
        else:
            if ch == "#":                       # коментарът — до истинския или изгубения ред
                end = min((j for j in (code.find("\n", i), code.find("\\n", i)) if j >= 0), default=n)
                out.append(code[i:end])
                i = end
                continue
            if ch in "'\"":
                quote = ch * 3 if code.startswith(ch * 3, i) else ch
                out.append(quote)
                i += len(quote)
                continue
            out.append(ch)
            i += 1
    return "".join(out)


def _python_write_check(path: Path, content: str) -> tuple[str, str]:
    """(съдържанието за запис, бележка към резултата) за .py файл."""
    from genesis_agent.code_edit import syntax_error
    err = syntax_error(path, content)
    if not err:
        return content, ""
    if content.count("\\n") >= _LITERAL_NEWLINES_MIN:
        for fixed in (_literal_newlines_outside_strings(content),
                      content.replace("\\r\\n", "\n").replace("\\n", "\n")):
            if not syntax_error(path, fixed):
                return fixed, ("\n⚠ Новите редове бяха написани буквално като \\n — поправени "
                               f"преди записа (без поправката: {err}). Пиши истински нови редове.")
    return content, (f"\n⚠ Записаният файл не е валиден Python: {err}. Поправи го в "
                     "този файл, преди да пускаш тестовете.")


def _tool_write_file(arg: str, content: str, exact: bool = False) -> str:
    path, redirect = _resolve_noted(arg, redirect=False)
    if not arg.strip() or path.is_dir():
        # Иначе write_text върху папка → „Permission denied“, а моделът го
        # чете като защитена папка и спира да пита (виж load_tool_arguments).
        return (f"[WRITE_FILE] ❌ Няма име на файл: path е „{arg}“ ({path} е папка). "
                "Дай пълния път до файла, напр. faktura/extract.py.")
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
    # Управляващ файл на Genesis (hooks/MCP/доверие/commands/agents)? Тези
    # файлове пускат програми извън sandbox-а или решават кое е доверено;
    # проектните са ВЪТРЕ в workspace-а, затова проверката „извън workspace"
    # не ги пазеше, а shell гейтът вече питаше за `printf > .genesis/hooks.json`
    # — по-слабият път решаваше (одит 2026-10-15). Същата бариера като shell.
    control = sandbox.control_path_reason(path)
    if control:
        verdict = sandbox.RiskVerdict(sandbox.RiskLevel.CONFIRM, [control])
        allowed, reason = sandbox._decide(f"WRITE_FILE {path}", verdict, sandbox.get_policy())
        if not allowed:
            return f"[WRITE_FILE] {reason}"
    # Пишем ли извън workspace-а? Това е операция за потвърждение.
    try:
        inside = path.resolve().is_relative_to(_WORKSPACE.resolve())
    except (ValueError, OSError):
        inside = False
    if not inside:
        verdict = sandbox.RiskVerdict(sandbox.RiskLevel.CONFIRM,
                                      [f"запис извън workspace: {path}" + redirect.replace("\n", " ")])
        allowed, reason = sandbox._decide(f"WRITE_FILE {path}", verdict, sandbox.get_policy())
        if not allowed:
            return f"[WRITE_FILE] {reason}{redirect}"
    # Ruff pre-check преди диска, само за .py (design note, 2026-07-29): не
    # блокираме записа при unfixable проблеми (моделът изрично поиска точно
    # това съдържание) — но ако ruff го оправи автоматично, пишем ФИКСНАТАТА
    # версия, и ВИНАГИ показваме находките в резултата, за да се самокоригира
    # моделът в следващия рунд вместо да чака sandbox изпълнение да ги хване.
    lint_note = ""
    if path.suffix == ".py":
        content, syntax_note = _python_write_check(path, content)
        from genesis_agent.code_validate import validate_code_with_ruff
        ok, detail = validate_code_with_ruff(content)
        if ok and detail:
            content = detail
        elif not ok:
            lint_note = f"\n{detail}"
        lint_note = syntax_note + lint_note
    # Съществуващ файл се записва в СВОЕТО кодиране (одит 2026-10-11): cp1251
    # файл, минал през READ→WRITE, излизаше с U+FFFD на мястото на всяка буква.
    encoding = "utf-8"
    kept: list[str] = []
    if path.is_file():
        try:
            with open(path, "rb") as fh:
                original, encoding = decode_text(fh.read(_STYLE_PROBE), partial=True)
        except OSError:
            original = ""
        if has_raw_bytes(original) and "\ufffd" in content:
            return (f"[WRITE_FILE: {path}] ❌ Файлът има байтове, които не са текст в "
                    f"{encoding}, а в READ_FILE се виждат като „\ufffd“ — записът би ги "
                    "заменил завинаги. Ползвай EDIT_FILE за частична промяна: тя ги пази.")
        # Краищата на редовете и BOM-ът също са на файла (одит 2026-10-11):
        # READ_FILE показва LF, моделът пише LF, и CRLF .bat излизаше изцяло с
        # LF и без BOM — дифф на всеки ред и счупени GOTO етикети. Чист CRLF
        # файл остава CRLF; смесеният получава LF, както EDIT_FILE го прави.
        # Ревю 2026-10-11: така „обърни run.sh в LF“ / „махни BOM-а“ не ставаше
        # с никой файлов инструмент, а WRITE_FILE отчиташе ✓ с файла непроменен.
        # `exact` пише точно даденото, а запазеното се казва в резултата.
        if not exact:
            crlf = original.count("\r\n")
            if crlf and crlf == original.count("\n") and content.replace("\r\n", "").count("\n"):
                content = content.replace("\r\n", "\n").replace("\n", "\r\n")
                kept.append("CRLF краищата")
            if original.startswith("\ufeff") and not content.startswith("\ufeff"):
                content = "\ufeff" + content
                kept.append("BOM-ът")
    enc_note = (f" (запазени {' и '.join(kept)} на файла; exact=true пише точно "
                "даденото — напр. за LF или без BOM)") if kept else ""
    try:
        data = encode_text(content, encoding)
    except UnicodeEncodeError as e:
        # Ревю 2026-10-11: отказът караше модела да маха „é“ от „Café“ и
        # файл не можеше да се обърне в UTF-8. Съдържание, което не влиза в
        # старото кодиране, не е върнат обратно сбъркан текст от READ_FILE
        # (той е в старото кодиране), така че UTF-8 не губи нищо.
        try:
            data = encode_text(content, "utf-8")
        except UnicodeEncodeError as e2:
            # Самотен сурогат (половин емоджи от JSON, `\ud83d`) няма място и в
            # UTF-8: без това излизаше суровата английска грешка на кодека от
            # dispatch_tool_call (ревю 2026-10-11, трета вълна).
            return (f"[WRITE_FILE: {path}] ❌ Знакът {content[e2.start:e2.end]!r} не е "
                    "текст в никое кодиране (половин емоджи?) — махни го или го замени. "
                    "ФАЙЛЪТ НЕ Е ПРОМЕНЕН.")
        enc_note += (f" (файлът беше {encoding}, а {content[e.start:e.end]!r} няма място "
                    "в него — записан е като UTF-8)")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as e:
        return f"[WRITE_FILE] Грешка: {e}"
    _SEEN_PATHS.add(path.resolve())
    _log_episode(f"WRITE_FILE {path}", f"записани {len(content)} символа",
                 ["tool", "write_file"])
    from genesis_agent.web_check import web_note
    return (f"[WRITE_FILE: {path}] ✓ записани {len(content)} символа{enc_note}{lint_note}"
            f"{_scaffold_tests(path)}{web_note(path)}{redirect}")


_PYTEST_CONFIGS = (("pytest.ini", ""), ("pyproject.toml", "[tool.pytest"),
                   ("setup.cfg", "[tool:pytest]"), ("tox.ini", "[pytest]"))


def _scaffold_tests(path: Path) -> str:
    """Първият тест в подпапка на проект без pytest настройка → празен
    conftest.py в корена, за да вижда тестът модулите от корена.

    NEXT_STEPS Г.10: проектът за ЕГН загуби 4 рунда по импорти, преди
    подсказката в RUN_CMD (_import_path_hint) да се появи — тя идва чак СЛЕД
    падналия pytest. Тук се слага преди първото пускане. Само ако коренът няма
    conftest.py и никаква pytest настройка: в съществуващ проект пътят вече е
    решен по някакъв начин и не е наша работа да го сменяме.
    """
    if path.suffix != ".py" or not (path.name.startswith("test_") or path.name.endswith("_test.py")):
        return ""
    try:
        root = _WORKSPACE.resolve()
        rel = path.resolve().relative_to(root)
    except (ValueError, OSError):
        return ""
    if len(rel.parts) < 2 or rel.parts[0] in _SKIP_DIRS:
        return ""
    # Проектът е папката над `tests/` — `egn/tests/test_egn.py` → `egn/`, не
    # коренът на workspace-а (там conftest-ът не помага на `pytest` в `egn/`).
    idx = next((i for i, part in enumerate(rel.parts[:-1]) if part in ("tests", "test")), 0)
    project = root.joinpath(*rel.parts[:idx])
    test_dir = rel.parts[idx]
    conftest = project / "conftest.py"
    if conftest.exists():
        return ""
    for name, section in _PYTEST_CONFIGS:
        cfg = project / name
        try:
            if cfg.is_file() and (not section or section in cfg.read_text(encoding="utf-8", errors="replace")):
                return ""
        except OSError:
            return ""
    # Скелето е част от хода за /undo (e2e 2026-10-11): измерено — WRITE_FILE
    # egn/tests/test_egn.py, после /undo триеше двата файла на модела, а
    # egn/conftest.py, създаден тук, оставаше сирак в проекта.
    from genesis_agent import edit_history
    edit_history.record(conftest)
    try:
        conftest.write_text(
            "# Коренът на проекта е в sys.path на pytest, за да се внасят модулите\n"
            "# от корена в tests/ без sys.path хакове.\n", encoding="utf-8")
    except OSError:
        return ""
    finally:
        edit_history.forget_if_unchanged(conftest)
    _SEEN_PATHS.add(conftest)
    where = conftest.relative_to(root).as_posix()
    return (f"\n[скеле] Създаден е празен {where} — тестовете в {test_dir}/ внасят "
            "модулите до него директно (`from x import y`), без sys.path хакове.")


def _tool_edit_file(path_arg: str, old: str, new: str, replace_all: bool = False) -> str:
    """Закотвена замяна в СЪЩЕСТВУВАЩ файл (виж genesis_agent/code_edit.py).

    Минава през същата CONFIRM бариера като WRITE_FILE при запис извън
    workspace-а — редакцията е по-малка по обхват, но не е по-малко реална.
    """
    path, redirect = _resolve_noted(path_arg, redirect=False)
    # Същата бариера като при READ_FILE, по същата причина — и намерена по
    # същия начин: `EDIT_FILE` връща unified diff, а диффът носи КОНТЕКСТНИ
    # редове. Редакция на `.env` с произволна котва връща в отговора реда
    # `OPENROUTER_API_KEY=...`, който никой не е искал да вижда. Гейтът на
    # READ_FILE пазеше едната врата; тази водеше към същото съдържание.
    sensitive = sandbox.sensitive_path_reason(path)
    if sensitive:
        verdict = sandbox.RiskVerdict(sandbox.RiskLevel.CONFIRM, [sensitive])
        allowed, reason = sandbox._decide(f"EDIT_FILE {path}", verdict, sandbox.get_policy())
        if not allowed:
            return f"[EDIT_FILE] {reason}"
    # Управляващ файл на Genesis — същата бариера като при WRITE_FILE и shell
    # (одит 2026-10-15): редакция на доверен hooks.json пак би развалила
    # подписа, но записът не бива да минава без потвърждение.
    control = sandbox.control_path_reason(path)
    if control:
        verdict = sandbox.RiskVerdict(sandbox.RiskLevel.CONFIRM, [control])
        allowed, reason = sandbox._decide(f"EDIT_FILE {path}", verdict, sandbox.get_policy())
        if not allowed:
            return f"[EDIT_FILE] {reason}"
    try:
        inside = path.resolve().is_relative_to(_WORKSPACE.resolve())
    except (ValueError, OSError):
        inside = False
    if not inside:
        verdict = sandbox.RiskVerdict(sandbox.RiskLevel.CONFIRM,
                                      [f"редакция извън workspace: {path}" + redirect.replace("\n", " ")])
        allowed, reason = sandbox._decide(f"EDIT_FILE {path}", verdict, sandbox.get_policy())
        if not allowed:
            return f"[EDIT_FILE] {reason}{redirect}"

    from genesis_agent.code_edit import edit_file
    res = edit_file(path, old, new, replace_all=replace_all)
    if not res.ok:
        _log_episode(f"EDIT_FILE {path}", f"отказана: {res.detail[:200]}",
                     ["tool", "edit_file", "rejected"])
        hint = _did_you_mean(path) if not path.exists() else ""
        return f"[EDIT_FILE: {path}] ❌ {res.detail}{hint}{redirect}"
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
    from genesis_agent.web_check import web_note
    return f"[EDIT_FILE: {path}] {res.detail}\n{diff}{ruff_note}{web_note(path)}{redirect}"


_SEARCH_CAP = 60    # SEARCH_CODE показва толкова; repo_map._DEFAULT_MAX_RESULTS
_GLOB_CAP = 200


def _project_top(root: Path) -> Path | None:
    """Workspace-ът, ако `root` е под него — дълбочината за пропуснатите
    папки се мери от корена на проекта (`src/env/` е код, `env/` в корена — venv)."""
    try:
        return _WORKSPACE if root.absolute().is_relative_to(_WORKSPACE.absolute()) else None
    except (OSError, ValueError):
        return None


def _skipped_note(root: Path, top: Path | None) -> str:
    """„ (не търсих в: node_modules/, build/ — …)“ или празно. Одит 2026-10-22:
    празният отговор казваше „низът наистина го няма“ и когато търсенето
    изобщо не е влизало там, където е."""
    from genesis_agent.repo_map import skipped_here
    names = skipped_here(root, top) if root.is_dir() else []
    if not names:
        return ""
    return (f" (не търсих в: {', '.join(names[:8])} — зависимости/артефакти; "
            "ако трябва там, подай папката като path)")


def _tool_search_code(arg: str, path: str = "", glob: str = "") -> str:
    pattern = arg.strip()
    if not pattern:
        return "[SEARCH_CODE] Празен шаблон."
    from genesis_agent.repo_map import search_code
    # Абсолютен glob или с `..` (ревю 2026-10-22): rg отговаряше „наистина го
    # няма“, Python пътят — „Грешка: Non-relative patterns are unsupported“, а
    # `[SEARCH_CODE: x | /път/*.py]` намираше. Както него: буквалната част е
    # папката (и минава през пазача по-долу, като при GLOB), името — филтърът.
    if glob and (Path(glob).is_absolute() or ".." in Path(glob).parts):
        parts = (Path(path) / glob).parts if path else Path(glob).parts
        cut = next((i for i, p in enumerate(parts) if any(ch in p for ch in "*?[{")),
                   len(parts) - 1)
        path, glob = str(Path(*parts[:cut])) if cut else "", parts[-1]
    # Шаблон, подаден като път (`site/**/*.html`): 2026-09-28 моделът получи
    # два пъти „няма такъв път“ и загуби два рунда. Частта до първия `*`/`?`
    # е папката, останалото — филтърът по име.
    if path and not glob and any(ch in path for ch in "*?"):
        parts = Path(path.strip()).parts
        cut = next(i for i, p in enumerate(parts) if any(ch in p for ch in "*?"))
        path, glob = str(Path(*parts[:cut])) if cut else "", parts[-1]
    root = _resolve(path) if path else _WORKSPACE
    refusal = _sensitive_root_refusal("SEARCH_CODE", root)
    if refusal:
        return refusal
    cap = _SEARCH_CAP
    top = _project_top(root)
    try:
        # Едно повече от показаното: така личи, че има още (одит 2026-10-22 —
        # „60 съвпадения“ изглеждаше като всичко, а бяха 80).
        hits = search_code(pattern, root, glob or None, max_results=cap + 1, top=top)
    except (ValueError, FileNotFoundError) as e:
        return f"[SEARCH_CODE: {pattern}] {e}"
    more = len(hits) > cap
    hits = hits[:cap]
    # Отделен от корена въпрос: единично чувствително попадение в иначе
    # нормална папка (`credentials.json` до кода). Решението е ЕДНО за всички
    # такива попадения — не по едно на ред — и е същото, което пази READ_FILE.
    # В режим „allow" операторът вече е казал да; безусловното криене там
    # правеше инструмента негоден за работа, която той е одобрил.
    # Скритото попадение се БРОИ на глас: премълчаното значи „низът го няма",
    # а е точно обратното, и моделът строи план върху грешен извод.
    sensitive_hits = [h for h in hits if sandbox.sensitive_path_reason(h.path)]
    hidden: list = []
    if sensitive_hits:
        reasons = sorted({sandbox.sensitive_path_reason(h.path) or "" for h in sensitive_hits})
        verdict = sandbox.RiskVerdict(sandbox.RiskLevel.CONFIRM, reasons)
        allowed, _ = sandbox._decide(f"SEARCH_CODE съвпадения в {len(sensitive_hits)} "
                                     "чувствителни файла", verdict, sandbox.get_policy())
        if not allowed:
            hidden = sensitive_hits
            hits = [h for h in hits if not sandbox.sensitive_path_reason(h.path)]
    if not hits and hidden:
        return (f"[SEARCH_CODE: {pattern}] {len(hidden)} съвпадения, всички в "
                "чувствителни файлове (ключове/тайни) — съдържанието им не се показва.")
    if not hits:
        skipped = _skipped_note(root, top)
        if skipped:
            return (f"[SEARCH_CODE: {pattern}] Няма съвпадения в {root}{skipped}. "
                    "В претърсеното низът наистина го няма.")
        return (f"[SEARCH_CODE: {pattern}] Няма съвпадения в {root}. "
                "Това означава, че низът наистина го няма — не предполагай, че е скрит.")
    lines = [f"[SEARCH_CODE: {pattern}] {len(hits)} съвпадения в {root}"]
    lines += [f"  {h.path}:{h.line}: {h.text.strip()}" for h in hits]
    if hidden:
        lines.append(f"  (+{len(hidden)} в чувствителни файлове — съдържанието "
                     "им не се показва)")
    if more:
        lines.append(f"  (показани първите {cap} — има още; стесни шаблона или подай path/glob)")
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
    from genesis_agent.repo_map import _GLOB_SCAN_CAP, find_files_counted, split_glob
    given = _resolve(path) if path else _WORKSPACE
    try:
        # Абсолютен шаблон или с `..` носи собствен корен — пазачът гледа ТОЙ
        # (одит 2026-10-22: `GLOB ../.ssh/*` изреждаше имената на ключовете).
        root = split_glob(pattern, given)[0]
    except ValueError as e:
        return f"[GLOB: {pattern}] {e}"
    refusal = _sensitive_root_refusal("GLOB", root)
    if refusal:
        return refusal
    top = _project_top(root)
    try:
        hits, total = find_files_counted(pattern, given, _GLOB_CAP, top=top)
    except (ValueError, FileNotFoundError) as e:
        return f"[GLOB: {pattern}] {e}"
    # Имената в чувствителна папка — същото решение като `LIST_DIR ~/.ssh`,
    # какъвто и да е пътят дотам (`GLOB .ssh/* | ~`). Скритите се броят на глас.
    sensitive = [h for h in hits if sandbox.sensitive_path_reason((root / h).parent.as_posix() + "/")]
    hidden = 0
    if sensitive:
        reasons = sorted({sandbox.sensitive_path_reason((root / h).parent.as_posix() + "/") or ""
                          for h in sensitive})
        verdict = sandbox.RiskVerdict(sandbox.RiskLevel.CONFIRM, reasons)
        allowed, reason = sandbox._decide(f"GLOB {pattern} в {root}", verdict, sandbox.get_policy())
        if not allowed:
            hidden = len(sensitive)
            hits = [h for h in hits if h not in sensitive]
            if not hits:
                return f"[GLOB] {reason}"
    if not hits:
        empty = root.is_dir() and not any(root.iterdir())
        return (f"[GLOB: {pattern}] Няма файлове по този шаблон в {root}{_skipped_note(root, top)}."
                + (" Папката е празна — създай нужните файлове с WRITE_FILE." if empty else ""))
    _log_episode(f"GLOB {pattern}", f"{len(hits)} файла", ["tool", "glob"])
    lines = [f"[GLOB: {pattern}] {len(hits)} файла в {root}"] + [f"  {h}" for h in hits]
    if hidden:
        lines.append(f"  (+{hidden} в чувствителни папки — имената им не се показват)")
    if total > len(hits) + hidden:
        more = f"поне {total}" if total >= _GLOB_SCAN_CAP else str(total)
        lines.append(f"  (показани {len(hits)} от {more} — най-новите по mtime; стесни шаблона)")
    return "\n".join(lines)


def _tool_repo_map(arg: str = "") -> str:
    from genesis_agent.repo_map import repo_map
    root = _resolve(arg) if arg.strip() else _WORKSPACE
    refusal = _sensitive_root_refusal("REPO_MAP", root)
    if refusal:
        return refusal
    try:
        out = repo_map(root)
    except OSError as e:
        return f"[REPO_MAP] Грешка: {e}"
    _log_episode(f"REPO_MAP {root}", "картиран", ["tool", "repo_map"])
    return out


# `pip install` на вече инсталирани пакети (bench sales-report, 2026-10-11):
# моделът започна с `python -m pip install pytest`, sandbox-ът без оператор
# отказа, моделът попита „да инсталираме ли?“ и пускът свърши без нито един
# файл — а pytest си беше там. Само прости имена: версия, -U, -r, -e или път
# значат „искам друго“ и минават през sandbox-а както винаги.
_PIP_INSTALL = re.compile(r"^(?:(?P<py>\"[^\"]+\"|\S+)\s+-m\s+pip|pip3?)\s+install\s+(?P<rest>.+)$",
                          re.IGNORECASE)
_PIP_QUIET_FLAGS = frozenset({"-q", "--quiet", "--user", "--disable-pip-version-check",
                              "--no-input", "--no-warn-script-location"})
_VERSIONS_PROBE = ("import importlib.metadata as m, sys\n"
                   "print(', '.join(f'{n} {m.version(n)}' for n in sys.argv[1:]))")
_PIP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?$")


def _already_installed(command: str) -> str:
    """Резултат „вече е инсталиран“, ако командата е pip install само на пакети,
    които Python-ът на командата вече има; иначе празен низ (пускай нормално)."""
    m = _PIP_INSTALL.match(command.strip())
    if not m:
        return ""
    words = [w.strip('"') for w in re.findall(r'"[^"]*"|\S+', m.group("rest"))]
    names = [w for w in words if w not in _PIP_QUIET_FLAGS]
    if not names or not all(_PIP_NAME.match(n) for n in names):
        return ""
    import shutil
    import subprocess
    py = (m.group("py") or "").strip('"')
    try:
        if not py:
            from genesis_agent.paths import project_python
            exe = project_python(_WORKSPACE)
        else:
            exe = shutil.which(py, path=sandbox._build_env(sandbox.get_policy()).get("PATH")) or ""
        if not exe:
            return ""
        probe = subprocess.run(
            [exe, "-c", _VERSIONS_PROBE, *[re.sub(r"\[.*\]$", "", n) for n in names]],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    if probe.returncode != 0 or not probe.stdout.strip():
        return ""
    _log_episode(f"RUN_CMD {command}", "вече инсталирано", ["tool", "run_cmd"])
    return (f"[RUN_CMD: {command}]  (rc=0)\nВече е инсталирано ({probe.stdout.strip()}) "
            f"в {exe} — pip не е пускан. Продължи със задачата.")


def _tool_run_cmd(arg: str) -> str:
    command = arg.strip()
    installed = _already_installed(command)
    if installed:
        return installed
    res = sandbox.run_shell(command, cwd=_WORKSPACE)
    if res.blocked:
        _log_episode(f"RUN_CMD {command}", f"отказан: {res.stderr}", ["tool", "run_cmd", "blocked"])
        # Security alert през notifier — блокирана опасна операция.
        try:
            from genesis_agent.notifier import notify
            notify(f"🛡️ **Genesis Sandbox** блокира опасна команда:\n`{command[:300]}`\n{res.stderr[:200]}")
        except Exception:
            log.debug("известието за блокирана команда не тръгна", exc_info=True)
        return f"[RUN_CMD: {command}]\n{res.stderr}"
    out = res.stdout.strip()
    err = res.stderr.strip()
    parts = [f"[RUN_CMD: {command}]  (rc={res.returncode})"]
    if out:
        parts.append(out[:6000])
    if err:
        parts.append("stderr:\n" + err[:2000])
    hint = _import_path_hint(out + "\n" + err, Path(_WORKSPACE)) or _missing_package_hint(out + "\n" + err)
    if hint:
        parts.append(hint)
    _log_episode(f"RUN_CMD {command}", "ok" if res.ok else f"rc={res.returncode}",
                 ["tool", "run_cmd"])
    return "\n".join(parts)


def _import_path_hint(output: str, root: Path) -> str:
    """Подсказка, когато pytest не вижда модул, който си стои в корена.

    Измерено 2026-09-25 (проект за ЕГН): тестовете в tests/ не намираха egn.py
    от корена, моделът опита четири пъти по различен начин (~4 рунда) и накрая
    сложи sys.path хак в самия тест. Стандартното решение е един празен
    conftest.py — механизъм тук, а не ред в промпта, защото кой модел ще се
    падне във веригата не се знае.
    """
    m = re.search(r"No module named '([\w.]+)'", output)
    if not m or (root / "conftest.py").exists():
        return ""
    name = m.group(1).split(".")[0]
    if not ((root / f"{name}.py").is_file() or (root / name / "__init__.py").is_file()):
        return ""
    return (f"[подсказка] `{name}` е в корена на проекта, но pytest не го вижда от tests/. "
            "Решение: празен conftest.py в корена (или `pythonpath = .` в pytest.ini) — "
            "не sys.path хакове в тестовете.")


def _missing_package_hint(output: str) -> str:
    """`No module named 'docx'` → точното име в PyPI (`python-docx`) и с кой
    Python да се инсталира. Без нея моделът гадае `pip install docx` (друг,
    стар пакет) или инсталира в Python-а на Genesis, не на проекта."""
    if "No module named" not in output:
        return ""
    try:
        from genesis_agent.paths import project_python
        from genesis_agent.py_deps import missing_module_hint
        py = project_python(_WORKSPACE)
        return missing_module_hint(output, Path(_WORKSPACE), f'"{py}"' if " " in py else py)
    except Exception:
        log.debug("подсказката за липсващ модул не се построи", exc_info=True)
        return ""


# Маркер, по който агентният цикъл разпознава "агентът чака отговор" и спира,
# вместо да продължи да гадае. Проверява се в genesis_agent/agent_core.py.
ASK_USER_MARKER = "__GENESIS_ASK_USER__"


def _tool_ask_user(question: str, options=None) -> str:
    """Питане при неяснота (design note, 2026-07-27).

    Нищо не се изпълнява — този инструмент СПИРА цикъла. Реалното "изчакване"
    е връщането на контрола към човека: фронтендът показва въпроса, а
    следващото съобщение на потребителя е отговорът. Затова тук няма input() —
    той би увиснал в GUI/systemd, където няма кой да пише в stdin.
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
        log.debug("WEB_SEARCH се провали", exc_info=True)
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
        log.debug("research не се зареди", exc_info=True)
        return f"[RESEARCH] research модулът не е наличен: {e}"
    return grounded_research(arg.strip())


def _tool_list_dir(arg: str) -> str:
    path, redirect = _resolve_noted(arg)
    if not path.is_dir():
        if sandbox.sensitive_path_reason(path) or sandbox.sensitive_path_reason(path.as_posix() + "/"):
            return f"[LIST_DIR] Не е директория: {path}"
        if path.is_file():
            return f"[LIST_DIR] {path} е файл, не папка — ползвай READ_FILE."
        return f"[LIST_DIR] Не е директория: {path}.{_did_you_mean(path)}"
    # Изброяването на `~/.ssh` не дава съдържание, но дава ИМЕНАТА на ключовете
    # — първата стъпка на всяко "кое има смисъл да прочета". Същият пазач,
    # който вече стои на READ_FILE и EDIT_FILE (design note, 2026-09-20);
    # измерено преди това: `LIST_DIR ~/.ssh` изброяваше `id_rsa` дори в
    # режим "deny".
    #
    # Разделителят накрая НЕ е козметика: образецът пази `\.ssh/` и `\.aws/`
    # със наклонена черта, защото е писан за shell команди, в които пътят до
    # файл винаги я носи. Директория, подадена без нея, не съвпада с нищо.
    sensitive = sandbox.sensitive_path_reason(path.as_posix() + "/")
    if sensitive:
        verdict = sandbox.RiskVerdict(sandbox.RiskLevel.CONFIRM, [sensitive])
        allowed, reason = sandbox._decide(f"LIST_DIR {path}", verdict, sandbox.get_policy())
        if not allowed:
            return f"[LIST_DIR] {reason}"
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as e:
        return f"[LIST_DIR] Грешка: {e}"
    lines = [f"[LIST_DIR: {path}]{redirect}"]
    if not entries:
        # Само заглавието, без нито един ред, моделът чете като „инструментът
        # не отговори“ и пита отново. Измерено 2026-09-28 (bench_projects):
        # 3 от първите 6 пуска — groq gpt-oss-120b вика LIST_DIR три пъти върху
        # новата празна папка, пазачът за повторения спира хода и не е написан
        # нито ред код (0/1 скрити).
        lines.append("  (папката е празна — няма файлове за четене; създай нужните с WRITE_FILE)")
        return "\n".join(lines)
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
        log.debug("skill_loader не се зареди", exc_info=True)
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
        log.debug("workspace_memory не се зареди", exc_info=True)
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
        log.debug("workspace_memory не се зареди", exc_info=True)
        return f"[TASK_ADD] workspace_memory не е наличен: {e}"
    parts = [p.strip() for p in arg.split("|")]
    return wm.add_thread(parts[0], parts[1] if len(parts) > 1 else "")


def _tool_task_update(arg: str) -> str:
    """[TASK_UPDATE: id | статус | следваща стъпка]  (статус: open/blocked/done)"""
    try:
        from genesis_agent import workspace_memory as wm
    except Exception as e:
        log.debug("workspace_memory не се зареди", exc_info=True)
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
        log.debug("workspace_memory не се зареди", exc_info=True)
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
        log.debug("delegate не се зареди", exc_info=True)
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
# ...но driver кодът е опционален, така че `[USE_SKILL: име]` САМ по себе си е
# валидната минимална форма (виж skill_loader.use_skill: празен driver = зареди
# умението и пусни self-test-а). Отделен pattern за нея — той се прилага само
# върху тагове, които затвореният вариант по-горе НЕ е consume-нал, и хваща
# точно самия таг, за да не изяде останалата част от отговора.
_BARE_USE_SKILL_RE = re.compile(r"\[USE_SKILL:\s*(?P<name>[^\]\n]+)\]")
# Текстове, с които skill_loader.use_skill сигнализира, че НИЩО не е тръгнало
# (няма достатъчно близко умение, или файлът не се зарежда). Държат се тук,
# защото само викащият знае дали да добави съвет за синтаксис отгоре.
_USE_SKILL_FAILURES = ("Няма достатъчно близко умение", "Грешка при зареждане",
                       "не е наличен")
# EDIT_FILE е тричастен — файл + anchor + замяна. Разделителят е дълъг и
# нетипичен нарочно: и двете половини са СУРОВ код, така че всичко по-късо
# (--- или ===) рано или късно се среща вътре в самия код и реже редакцията
# на грешното място.
_EDIT_SEPARATOR = "---GENESIS-REPLACE-WITH---"
_EDIT_RE = re.compile(r"\[EDIT_FILE:\s*(?P<path>[^\]]+)\](?P<body>.*?)\[END_EDIT\]",
                      re.DOTALL)
# Аргументът може да носи скоби до две нива: `app/[id]/page.tsx` (Next.js),
# `sys.argv[1:]`, `[ -f x ]`. С `[^\]]+` (до 2026-10-07) всичко се режеше на
# първата `]` — файлът „не съществуваше“, а командата падаше с незатворени кавички.
_BRACKETS = r"\[(?:[^\[\]]|\[[^\[\]]*\])*\]"
_SIMPLE_RE = re.compile(
    r"\[(?P<tool>READ_FILE|RUN_CMD|WEB_SEARCH|LIST_DIR|DELEGATE|RESEARCH|BROWSE|ASK_USER|"
    r"SEARCH_CODE|REPO_MAP|GLOB|"
    r"BROWSER_CLICK|BROWSER_TYPE|REMEMBER|TASK_ADD|TASK_UPDATE|TASK_LIST|"
    r"RUN_BG|BG_OUTPUT|BG_KILL|MCP|EXPLORE|WEB_FETCH|AGENT|TODO_WRITE|VIEW_IMAGE):"
    r"\s*(?P<arg>(?:[^\[\]]|" + _BRACKETS + r")+)\]"
)
# REPO_MAP без аргумент = текущият workspace (както BROWSER_READ/TASK_LIST).
_REPO_MAP_RE = re.compile(r"\[REPO_MAP\]")
# BROWSER_READ е без аргумент — отделен pattern.
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
    "RUN_BG": lambda arg: _tool_run_bg(arg),
    "BG_OUTPUT": lambda arg="": _background().output(arg),
    "BG_KILL": lambda arg: _background().kill(arg),
    "MCP": lambda arg: _mcp().call_text_tag(arg),
    "EXPLORE": lambda arg: _tool_explore(arg),
    "WEB_FETCH": lambda arg: _tool_web_fetch(arg),
    "AGENT": lambda arg: _tool_agent(*(part.strip() for part in arg.partition("|")[::2])),
    "TODO_WRITE": lambda arg: _todos().write(arg),
    "VIEW_IMAGE": lambda arg: _tool_view_image(arg),
}


def _tool_view_image(path: str) -> str:
    """Картинка от работната папка към модела в следващата заявка (2026-10-15).
    Пътищата — като READ_FILE (genesis_agent.vision.load_image)."""
    from genesis_agent import vision
    resolved, redirect = _resolve_noted(str(path).strip())
    return vision.view_image(str(resolved), Path(_WORKSPACE)) + redirect


def _tool_agent(name: str, task: str) -> str:
    """Собствен под-агент от .genesis/agents/ — задачата в отделен разговор."""
    from genesis_agent import agents
    return agents.run(name, task, Path(_WORKSPACE))


def _todos():
    from genesis_agent import todos
    return todos


def _tool_web_fetch(url: str) -> str:
    from genesis_agent.web_fetch import web_fetch
    return web_fetch(url)


def _tool_explore(question: str) -> str:
    """Под-агент, който само чете — отговорът без файловете в историята."""
    from genesis_agent.explore import explore
    return explore(question, str(_WORKSPACE))


def _mcp():
    from genesis_agent import mcp_client
    return mcp_client


def _background():
    from genesis_agent import background
    return background


def _tool_run_bg(arg: str) -> str:
    """Фонова команда (dev сървър, watcher) — същата бариера като RUN_CMD."""
    return _background().start(arg, cwd=Path(_WORKSPACE))


_READONLY_RE = re.compile(
    r"\[(?P<tool>READ_FILE|WEB_SEARCH|LIST_DIR|RESEARCH):\s*(?P<arg>[^\]]+)\]"
)
_READONLY_DISPATCH = {
    "READ_FILE": _tool_read_file,
    "WEB_SEARCH": _tool_web_search,
    "RESEARCH": _tool_research,
    "LIST_DIR": _tool_list_dir,
}


def _safe_tool(name: str, fn: Callable[..., str], *args) -> str:
    """Изпълнява един тул и превръща изключението в текст вместо да го пуска нагоре.

    Огледало на гаранцията, която `dispatch_tool_call` (native пътят) вече дава
    изрично. tool_schemas.py описва двата пътя като различаващи се САМО по
    формата на извикването — но при грешка се разминаваха дълбоко (bug fix,
    2026-08-12): native връщаше "[TOOL] Грешка при изпълнение: ...", а
    текстовият пускаше изключението през parse_and_execute_tools и то отнасяше
    и вече УСПЕЛИТЕ резултати от същата реплика (един провалил се RESEARCH
    изяждаше READ_FILE и LIST_DIR резултатите до него).

    Има значение кой път пострадва: текстовите тагове са пътят на моделите БЕЗ
    native function-calling, тоест на слабия/локалния резервен слой — точно там,
    където устойчивостта трябва да е по-голяма, а не по-малка.
    """
    payload = _text_args(name, args)
    blocked = _before_tool(name, payload)
    if blocked:
        return blocked
    try:
        out = fn(*args)
    except Exception as e:
        log.debug("инструментът %s гръмна", name, exc_info=True)
        out = f"[{name}] Грешка при изпълнение: {e}"
    return _after_tool(name, payload, out)


def _text_args(name: str, args: tuple) -> dict:
    """The text-tag arguments in the shape the native path has (for hooks)."""
    if name == "EDIT_FILE" and len(args) >= 3:
        return {"path": str(args[0]).strip(), "old": args[1], "new": args[2]}
    if name in ("WRITE_FILE", "EDIT_FILE", "READ_FILE", "LIST_DIR", "VIEW_IMAGE") and args:
        return {"path": str(args[0]).strip(), **({"content": args[1]} if name == "WRITE_FILE"
                                                  and len(args) > 1 else {})}
    if name in ("RUN_CMD", "RUN_BG") and args:
        return {"command": str(args[0])}
    if name == "WEB_FETCH" and args:  # hooks виждат {url} и по двата пътя
        return {"url": str(args[0]).strip()}
    return {"arg": " | ".join(str(a) for a in args)}


def _hook_view(name: str, args: dict) -> tuple[str, dict]:
    """Как hooks виждат извикването: `[RUN_BG: …]` е RUN_CMD във фона —
    hook с matcher RUN_CMD не бива да се заобикаля с другото име (одит 2026-10-08)."""
    if name == "RUN_BG":
        return "RUN_CMD", {"command": args.get("arg", args.get("command", "")), "background": True}
    if name == "MCP":
        # `[MCP: github.create_issue | {...}]` — за hooks същото като native
        # `mcp__github__create_issue`: matcher mcp__github__.* се заобикаляше
        # с текстовия таг (одит 2026-10-08).
        head, _, raw = str(args.get("arg", "")).partition("|")
        from genesis_agent import mcp_client
        found = mcp_client._find(head.strip())
        server, _, tool = head.strip().partition(".")
        qualified = found[1].qualified if found else \
            f"mcp__{mcp_client._safe(server)}__{mcp_client._safe(tool)}"
        try:
            parsed = load_tool_arguments(raw.strip()) if raw.strip() else {}
        except (ValueError, TypeError):
            parsed = {"arg": raw.strip()}
        return qualified, parsed if isinstance(parsed, dict) else {"arg": raw.strip()}
    if name == "EXPLORE" and "arg" in args:
        return name, {"question": args["arg"]}
    if name == "AGENT" and "arg" in args:
        agent, _, task = str(args["arg"]).partition("|")
        return name, {"agent": agent.strip(), "task": task.strip()}
    return name, args


def _before_tool(name: str, args: dict) -> str | None:
    """One gate for both paths (text tags and native calls): plan mode, the
    operator's PreToolUse hooks, and the /undo snapshot of a file about to
    change. Never raises — and fails CLOSED: an error in the gate refuses the
    tool (it used to be swallowed and the tool ran past a hook that had
    just blocked it — audit 2026-10-08)."""
    if _cancelled():
        return f"[{name}] ⏹ Спряно от оператора — не е изпълнено."
    try:
        hook_name, hook_args = _hook_view(name, args)
        scope = _AGENT_SCOPE
        if scope is not None and hook_name not in scope[1] and name not in scope[1]:
            return (f"[{name}] ⛔ Под-агентът „{scope[0]}“ няма този инструмент. "
                    f"Има: {', '.join(sorted(scope[1])) or 'никакви'}.")
        from genesis_agent import plan_mode
        refusal = plan_mode.refusal(name, args)
        if refusal:
            return refusal
        from genesis_agent import hooks
        blocked = hooks.pre_tool(hook_name, hook_args)
        if blocked:
            return blocked
    except Exception as e:
        log.debug("проверката преди %s се провали — не е изпълнен", name, exc_info=True)
        return f"[{name}] ⛔ Проверката преди инструмента се провали ({e}) — не е изпълнен."
    elided = _copies_elided_mark(name, args)
    if elided:
        return elided
    if name in ("WRITE_FILE", "EDIT_FILE") and args.get("path"):
        try:
            from genesis_agent import edit_history
            edit_history.record(_resolve_noted(str(args["path"]), redirect=False)[0])
        except Exception:
            log.debug("снимката за /undo преди запис не стана", exc_info=True)
    elif _may_change_files(name, args):
        # Команда, умение, MCP: пипат файлове, които никой не е назовал —
        # снимка на проекта преди и след тях, за /undo (2026-10-09).
        try:
            from genesis_agent import edit_history
            if name == "RUN_BG" or (name == "RUN_CMD" and str(args.get("background", "")).strip()
                                    .lower() in ("true", "1", "yes")):
                # Фонова: пише и след края на хода — снимка не помага. Ходът се
                # маркира в background.start, когато процесът наистина тръгне.
                pass
            else:
                # Командите и уменията минават през sandbox: снимката — когато
                # процесът тръгне, след одобрението (одит 2026-10-09).
                edit_history.before_command(Path(_WORKSPACE),
                                            at_exec=name in ("RUN_CMD", "USE_SKILL"))
        except Exception:
            log.debug("снимката за /undo преди команда не стана", exc_info=True)
    return None


def _copies_elided_mark(name: str, args: dict) -> str | None:
    """Отказва запис, който носи маркера за изпуснато от историята (B.12).

    budget._elide_old_writes заменя съдържанието на стари записи в изпратеното
    копие с маркер; в текстов режим слаб модел вижда
    WRITE_FILE({path, content: <маркер>…}) и може да го препише като „код“ —
    файлът би станал един ред бележка. Пазачът е в кода, не в промпта, и е
    винаги включен, независимо от GENESIS_ELIDE_OLD_WRITES (2026-10-11): двата
    пътя (native и текстови тагове) стигат дотук с нормализирани аргументи.
    RUN_CMD/RUN_BG също — heredoc в команда е същият запис по друг път.
    Хваща се формата на маркера, не голото му име (budget.carries_elided_mark)
    — иначе търсене или commit съобщение, които го назовават, се отказваха."""
    if name in ("WRITE_FILE", "EDIT_FILE"):
        text = str(args.get("content", "")) + str(args.get("new", ""))
    elif name in ("RUN_CMD", "RUN_BG"):
        text = str(args.get("command", "")) + str(args.get("arg", ""))
    else:
        return None
    from genesis_agent.budget import carries_elided_mark
    if not carries_elided_mark(text):
        return None
    target = args.get("path") or args.get("command") or args.get("arg") or ""
    return (f"[{name}: {str(target)[:120]}] ⛔ Това е маркер за изпуснато от историята, "
            f"не съдържание — прочети файла с READ_FILE и напиши истинския код.")


def _may_change_files(name: str, args: dict) -> bool:
    from genesis_agent import plan_mode
    if name in plan_mode.READ_ONLY_TOOLS or name in ("BG_KILL", "REMEMBER", "TASK_ADD",
                                                     "TASK_UPDATE", "TODO_WRITE"):
        return False
    if name.startswith(("BROWSE", "BROWSER_")):
        return False
    if name.startswith("mcp__"):
        try:
            from genesis_agent import mcp_client
            return not mcp_client.is_read_only(name)
        except Exception:
            log.debug("MCP не каза дали %s само чете — смятаме, че пише", name, exc_info=True)
            return True
    return True


def _after_tool(name: str, args: dict, result: str) -> str:
    try:
        from genesis_agent import hooks
        hook_name, hook_args = _hook_view(name, args)
        out = hooks.post_tool(hook_name, hook_args, result)
    except Exception:
        log.debug("куките след %s се провалиха", name, exc_info=True)
        out = result
    # СЛЕД PostToolUse hooks: `ruff format` след EDIT_FILE иначе изглеждаше
    # като чужда промяна и /undo оставяше файла (одит 2026-10-09).
    if name in ("WRITE_FILE", "EDIT_FILE") and args.get("path"):
        try:  # отказан/неуспешен запис не е промяна за /undo
            from genesis_agent import edit_history
            edit_history.forget_if_unchanged(_resolve_noted(str(args["path"]), redirect=False)[0])
        except Exception:
            log.debug("/undo не забрави непроменения файл", exc_info=True)
    elif _may_change_files(name, args) and not (
            name == "RUN_BG" or (name == "RUN_CMD" and str(args.get("background", "")).strip()
                                 .lower() in ("true", "1", "yes"))):
        try:  # какво промени командата — само между двете снимки
            from genesis_agent import edit_history
            edit_history.after_command()
        except Exception:
            log.debug("промените от командата не се записаха за /undo", exc_info=True)
    return out


def parse_and_execute_readonly_tools(response_text: str) -> list[str]:
    """
    Изпълнява САМО безопасните read-only инструменти (READ_FILE/WEB_SEARCH/
    LIST_DIR). За автономния цикъл — Brain-ът може да събере
    информация по време на мисия, без риск от RUN_CMD/WRITE_FILE/DELEGATE.
    Връща списък от резултати.
    """
    if not response_text:
        return []
    results: list[tuple[int, str]] = []
    for m in _READONLY_RE.finditer(response_text):
        fn = _READONLY_DISPATCH[m.group("tool")]
        results.append((m.start(), _safe_tool(m.group("tool"), fn, m.group("arg"))))
    results.sort(key=lambda t: t[0])
    return [r for _, r in results]


# Claude (през `claude -p`, 2026-10-12) понякога пише ПЪРВИЯ инструмент в
# своя собствен XML формат, а останалите — с нашите тагове. Измерено на 30
# сурови отговора от bench (vat/eik/iban ×2): 6 започваха с
# `<invoke name="WRITE_FILE">път</parameter>` / `<invoke name="RUN_CMD">pwd</invoke>`;
# всеки струваше рунд („първият ми WRITE_FILE беше с грешен синтаксис“), а
# отговор САМО с XML не съдържаше `[TAG:` — нямаше подкана и ходът свършваше
# без нито един инструмент (vat-check 0/1). Превръщат се само имената на
# нашите инструменти; всичко друго остава текст.
_XML_TOOLS = ("WRITE_FILE|EDIT_FILE|READ_FILE|RUN_CMD|RUN_BG|LIST_DIR|SEARCH_CODE|GLOB|"
              "REPO_MAP|WEB_SEARCH|WEB_FETCH|ASK_USER|EXPLORE|TODO_WRITE|BG_OUTPUT|BG_KILL")
_XML_BLOCK_OPEN_RE = re.compile(
    r'<invoke name="(?P<tool>WRITE_FILE|EDIT_FILE)">\s*'
    r'(?:<parameter name="(?:path|file_path)">)?\s*(?P<path>[^\n<\]]+?)\s*'
    r'(?:</parameter>|</invoke>)?[ \t]*\n'
    r'(?:\s*<parameter name="(?:content|body)">)?')
_XML_BLOCK_CLOSE_RE = re.compile(r"\n?[ \t]*</(?:parameter|invoke)>\s*(?=\[END_(?:WRITE|EDIT)\])")
# Стойността не минава през друг `<invoke` (одит 2026-10-22): незатворен
# `<invoke name="RUN_CMD">…pytest -q` стигаше до `</invoke>` на СЛЕДВАЩИЯ
# READ_FILE — той не тръгваше, а XML-ът му отиваше в shell командата
# („Syntax error: newline unexpected“). Незатвореният остава текст.
# Само друго извикване (`<invoke name="`) спира стойността, не всяко
# `<invoke` (ревю 2026-10-22: `grep -n "<invoke" x.py` иначе не тръгваше).
_XML_VALUE = r'(?:(?!<invoke name=").)*?'
# Съдържанието на запис/редакция е данни и МОЖЕ да съдържа <invoke> (ревю
# 2026-10-22, измерено): тест с `'<invoke name="RUN_CMD">echo … >> log.txt</invoke>'`
# в content спираше външния WRITE_FILE — файлът не се записваше, а вътрешният
# RUN_CMD се ИЗПЪЛНЯВАШЕ (преди поправката по-горе: запис, нищо пуснато).
_XML_CONTENT_KEYS = "content|body|text|old_string|old_str|old|new_string|new_str|new"
_XML_SIMPLE_RE = re.compile(
    r'<invoke name="(?P<tool>' + _XML_TOOLS + r')">\s*'
    r'(?:<parameter name="\w+">)?(?P<arg>' + _XML_VALUE + r')\s*(?:</parameter>\s*)?</invoke>',
    re.DOTALL)
_XML_TAIL_RE = re.compile(r"\s*</parameter>\s*</invoke>\s*\]")
_XML_WRAPPER_RE = re.compile(r"</?function_calls>\s*")


# Пълното XML извикване с именувани параметри (2026-10-12, bench_fix с
# claude_cli: 0/13 — в `genesis fix` Claude пише ВСЯКА редакция така:
# `<invoke name="EDIT_FILE"><parameter name="path">…</parameter>
# <parameter name="old_string">…</parameter><parameter name="new_string">…`;
# формата с един аргумент горе не го хваща и редакцията не се изпълняваше).
_XML_FULL_INVOKE_RE = re.compile(
    r'<invoke name="(?P<tool>' + _XML_TOOLS + r')">'
    r'(?P<params>(?:\s*(?:<parameter name="(?:' + _XML_CONTENT_KEYS + r')">.*?'
    r'|<parameter name="[\w-]+">' + _XML_VALUE + r')</parameter>)+)\s*</invoke>',
    re.DOTALL)
_XML_PARAM_RE = re.compile(r'<parameter name="(?P<key>[\w-]+)">(?P<value>.*?)</parameter>', re.DOTALL)
_XML_ARG_ALIASES = {"file_path": "path", "filepath": "path", "filename": "path",
                    "old_string": "old", "old_str": "old", "new_string": "new", "new_str": "new",
                    "cmd": "command", "text": "content", "body": "content", "query": "pattern"}
# Главният аргумент на едноредовия таг на всеки инструмент.
_XML_MAIN_ARG = {"READ_FILE": "path", "LIST_DIR": "path", "REPO_MAP": "path", "GLOB": "pattern",
                 "SEARCH_CODE": "pattern", "RUN_CMD": "command", "RUN_BG": "command",
                 "WEB_SEARCH": "query", "WEB_FETCH": "url", "ASK_USER": "question",
                 "EXPLORE": "question", "TODO_WRITE": "todos", "BG_OUTPUT": "id", "BG_KILL": "id"}


# Стойност, която скобовият регекс не би върнал цяла (одит 2026-10-22):
# XML-ът се превръщаше в `[RUN_CMD: стойност]` и се парсваше пак, а той спира
# на първата несдвоена `]` — `echo "a]b"` тръгваше като `echo "a`, а
# `grep -c "[" a.txt` изобщо не тръгваше. Такава стойност отива в `slots`, а
# тагът носи само номера ѝ; parse_and_execute_tools я взима оттам дословно.
_XML_SLOT_RE = re.compile(r"\x00(\d+)\x00")


def _xml_tag(tool: str, value: str, slots: list[str] | None) -> str:
    tag = f"[{tool}: {value}]"
    # Слотът се чете само от едноредовите тагове (ревю 2026-10-22): отварящият
    # `[WRITE_FILE: път]` на `<invoke name="WRITE_FILE">a.py</invoke>…[END_WRITE]`
    # получаваше път „\x000\x00“ и записът падаше като „извън workspace“.
    if slots is None or tool not in _SIMPLE_DISPATCH:
        return tag
    m = _SIMPLE_RE.fullmatch(tag)
    if m and m.group("arg") == value.lstrip():
        return tag
    slots.append(value)
    return f"[{tool}: \x00{len(slots) - 1}\x00]"


def _xml_full_invoke(m: re.Match, slots: list[str] | None = None) -> str:
    tool = m.group("tool")
    args: dict[str, str] = {}
    for p in _XML_PARAM_RE.finditer(m.group("params")):
        key = p.group("key")
        args[_XML_ARG_ALIASES.get(key, key) if not (tool == "WEB_SEARCH" and key == "query")
             else key] = p.group("value")
    path = args.get("path", "").strip()
    if tool == "EDIT_FILE" and path and "old" in args and "new" in args:
        return f"[EDIT_FILE: {path}]\n{args['old']}\n{_EDIT_SEPARATOR}\n{args['new']}\n[END_EDIT]"
    if tool == "WRITE_FILE" and path and "content" in args:
        return f"[WRITE_FILE: {path}]\n{args['content']}[END_WRITE]"
    main = args.get(_XML_MAIN_ARG.get(tool, ""), "") or (next(iter(args.values()), "") if len(args) == 1 else "")
    if not main:
        return m.group(0)        # непознат вид — остава текст и получава подканата за синтаксис
    if tool == "RUN_CMD" and str(args.get("background", "")).lower() in ("true", "1", "yes"):
        tool = "RUN_BG"
    return _xml_tag(tool, main.strip(), slots)


# Трета форма от същия bench_fix (3 от 50 отговора): `<tool_call>READ_FILE
# </tool_call><arg name="path">…</arg>` — свежда се до <invoke>, един път.
_XML_TOOL_CALL_RE = re.compile(
    r'<tool_call>\s*(?P<tool>' + _XML_TOOLS + r')\s*</tool_call>'
    r'(?P<args>(?:\s*<arg name="[\w-]+">.*?</arg>)+)', re.DOTALL)
_XML_ARG_RE = re.compile(r'<arg name="(?P<key>[\w-]+)">(?P<value>.*?)</arg>', re.DOTALL)


def _tool_call_as_invoke(m: re.Match) -> str:
    params = "".join(f'<parameter name="{a.group("key")}">{a.group("value")}</parameter>'
                     for a in _XML_ARG_RE.finditer(m.group("args")))
    return f'<invoke name="{m.group("tool")}">{params}</invoke>'


# Измислен затварящ таг (2026-10-12, 2 на 371 отговора на Claude): дълга
# RUN_CMD с heredoc, затворена с `EOF[END_CMD]` вместо `]` — тагът не се
# разпознаваше, идваше подкана и ходът губеше рунд („heredoc-ът не се затвори“).
# `[END_CMD]` не е наш синтаксис никъде, затова при отворен RUN_CMD/RUN_BG е `]`.
_END_CMD = "[END_CMD]"


def _normalize_xml_tool_calls(text: str, slots: list[str] | None = None) -> str:
    """XML формата на Claude за нашите инструменти → нашите тагове.

    С `slots` (само от parse_and_execute_tools) стойност, която скобите не
    биха върнали цяла, се пази там — виж _xml_tag."""
    if _END_CMD in text and ("[RUN_CMD:" in text or "[RUN_BG:" in text):
        text = text.replace(_END_CMD, "]")
    if not any(mark in text for mark in ("<invoke", "</parameter>", "</invoke>", "<tool_call>")):
        return text
    text = _XML_WRAPPER_RE.sub("", text)
    text = _XML_TOOL_CALL_RE.sub(_tool_call_as_invoke, text)
    text = _XML_FULL_INVOKE_RE.sub(partial(_xml_full_invoke, slots=slots), text)
    text = _XML_BLOCK_OPEN_RE.sub(lambda m: f"[{m.group('tool')}: {m.group('path')}]\n", text)
    text = _XML_BLOCK_CLOSE_RE.sub("\n", text)
    text = _XML_TAIL_RE.sub("]", text)
    original = text

    def simple(m: re.Match) -> str:
        tag = f"[{m.group('tool')}: {m.group('arg')}]"
        # Същата команда и като XML, и като таг („<invoke name="RUN_CMD">pwd</invoke>
        # [RUN_CMD: pwd]“) — веднъж, не два пъти (команда със страничен ефект).
        return "" if tag in original else _xml_tag(m.group("tool"), m.group("arg"), slots)

    return _XML_SIMPLE_RE.sub(simple, text)


# Таг в `код` или в ``` ограда е пример или цитат, не извикване (одит
# 2026-10-22). Измерено преди: „Genesis би изпълнил `[RUN_CMD: git stash]` …
# Не пускам нищо“ скриваше незакомитнатата работа на оператора; финалното
# резюме „командата `[RUN_CMD: echo ред >> log.txt]` добави ред“ я пускаше
# втори път (два реда в log.txt); WRITE_FILE пример в ``` създаваше файла.
# Промптът и без това казва „never inside ```code``` fences“. Ако моделът
# все пак е обвил истинско извикване, нищо не тръгва, а подканата за
# синтаксис му казва защо (looks_like_attempted_tool_tag, malformed_tag_note).
_BACKTICKS_RE = re.compile(r"`+")
_FENCE_CLOSE_RE = re.compile(r"^[ \t]*(`{3,})[ \t]*\r?$", re.MULTILINE)   # и CRLF отговор


def _quoted_spans(text: str, tags: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Участъците (start, end) на затворените ``` огради и на `кода` в ред.

    `tags` са (start, end) на кандидат-таговете: тялото или аргументът на таг,
    започнал ИЗВЪН такъв участък, е съдържание — ``` в README от WRITE_FILE или
    `date` в RUN_CMD не отварят нищо, иначе нечетен брой би скрил истинските
    тагове след тях. Незатворена ограда не се брои (както незатворен `)."""
    spans: list[tuple[int, int]] = []
    starts = sorted(tags, key=lambda t: (t[0], -t[1]))
    ti, pos, n = 0, 0, len(text)
    while pos < n:
        while ti < len(starts) and starts[ti][0] < pos:
            ti += 1
        b = text.find("`", pos)
        if ti < len(starts) and (b < 0 or starts[ti][0] < b):
            pos = max(pos + 1, starts[ti][1])     # таг извън код: прескачаме го цял
            continue
        if b < 0:
            break
        run = _BACKTICKS_RE.match(text, b)
        width = len(run.group(0)) if run else 1
        line_start = text.rfind("\n", 0, b) + 1
        line_end = text.find("\n", b)
        line_end = n if line_end < 0 else line_end
        # Ограда: ``` в началото на ред и без ` след тях (```js``` е код в ред).
        if (width >= 3 and not text[line_start:b].strip(" \t")
                and "`" not in text[b + width:line_end]):
            fence = next((c for c in _FENCE_CLOSE_RE.finditer(text, line_end)
                          if len(c.group(1)) >= width), None)
            if fence:
                spans.append((line_start, fence.end()))
                pos = fence.end()
                continue
        else:
            close = next((c for c in _BACKTICKS_RE.finditer(text, b + width, line_end)
                          if len(c.group(0)) == width), None)
            if close:
                spans.append((b, close.end()))
                pos = close.end()
                continue
        pos = b + width
    return spans


def parse_and_execute_tools(response_text: str) -> list[str]:
    """
    Извлича и изпълнява всички тул-тагове в реда, в който се появяват.
    Връща списък от резултати (по един низ на тул). Празен списък = няма тулове.

    Блоковите тагове (WRITE_FILE / EDIT_FILE / USE_SKILL) се събират ЗАЕДНО и
    се решава влагането ПРЕДИ който и да е от тях да се изпълни (bug fix,
    2026-09-20). Преди това всеки тип се обхождаше в отделен цикъл и пазачът
    `_inside_write` се прилагаше само върху едноредовите тагове — затова
    `[USE_SKILL: x]...[END_USE_SKILL]` или `[EDIT_FILE: ...]`, написани ВЪТРЕ в
    тялото на `[WRITE_FILE: doc.md]`, реално се изпълняваха. Тоест молба от рода
    на "запиши ми в README как се вика умение" изпълняваше умението вместо (и
    освен) да запише файла. Сега най-външният блок печели, а всичко вътре в него
    е текст, какъвто и да е типът му.
    """
    if not response_text:
        return []
    slots: list[str] = []
    response_text = _normalize_xml_tool_calls(response_text, slots)

    # (позиция_в_текста, име, действие) — НИЩО не се изпълнява, докато не са
    # събрани всички тагове и не е решено влагането. После се изпълняват в
    # реда на позицията (bug fix, 2026-10-11). Дотук всяка група (блокове →
    # USE_SKILL → едноредови → BROWSER_READ …) се ИЗПЪЛНЯВАШЕ в свой проход и
    # само резултатите се сортираха: „[RUN_CMD: cp app.py app.bak]“ преди
    # „[WRITE_FILE: app.py]“ правеше копието СЛЕД презаписа (измерено:
    # app.bak = „PRICE = 0“), а върнатите резултати лъжеха модела, че копието
    # е първо. Същото за READ_FILE преди EDIT_FILE — четенето виждаше новото.
    actions: list[tuple[int, str, Callable[[], str]]] = []

    # ── 1. Блокови тагове: събираме кандидатите, после решаваме влагането ────
    # (start, end, kind, match) за трите блокови типа, подредени по позиция.
    block_candidates: list[tuple[int, int, str, re.Match]] = []
    for kind, rx in (("WRITE_FILE", _WRITE_RE), ("EDIT_FILE", _EDIT_RE),
                     ("USE_SKILL", _USE_SKILL_RE)):
        # С припокриване, не finditer (ревю 2026-10-22, измерено): „Ще ползвам
        # `[WRITE_FILE: път]`, за да…“ + истински запис — примерът стигаше до
        # общия [END_WRITE] и поглъщаше истинския таг; той не беше кандидат,
        # а примерът се прескача като цитат → нищо не се записваше. Вложен
        # кандидат пак се прескача, ако външният е приет (_inside_block).
        pos = 0
        while (m := rx.search(response_text, pos)) is not None:
            block_candidates.append((m.start(), m.end(), kind, m))
            pos = m.start() + 1
    # По-ранният старт печели; при равен старт — по-дългият блок е външният.
    block_candidates.sort(key=lambda c: (c[0], -c[1]))

    consumed_spans: list[tuple[int, int]] = []

    def _inside_block(pos: int) -> bool:
        return any(s <= pos < e for s, e in consumed_spans)

    # Примерите в `код` и ``` огради — виж _quoted_spans.
    quoted = _quoted_spans(response_text, [(c[0], c[1]) for c in block_candidates] + [
        (m.start(), m.end()) for rx in (_BARE_USE_SKILL_RE, _SIMPLE_RE, _BROWSER_READ_RE,
                                        _REPO_MAP_RE, _TASK_LIST_RE)
        for m in rx.finditer(response_text)])

    def _quoted(pos: int) -> bool:
        return any(s <= pos < e for s, e in quoted)

    def _edit_action(m: re.Match) -> str:
        body = m.group("body")
        if _EDIT_SEPARATOR not in body:
            return (f"[EDIT_FILE: {m.group('path').strip()}] ❌ Липсва разделителят "
                    f"{_EDIT_SEPARATOR} между стария и новия текст.")
        old_part, new_part = body.split(_EDIT_SEPARATOR, 1)
        return _safe_tool("EDIT_FILE", _tool_edit_file, m.group("path"),
                          _strip_one_newline(old_part), _strip_one_newline(new_part))

    for start, end, kind, m in block_candidates:
        if _inside_block(start) or _quoted(start):
            continue  # вложен в вече приет блок или пример в код → текст, не тул
        consumed_spans.append((start, end))
        if kind == "WRITE_FILE":
            # Новият ред веднага след тага е синтаксис, не съдържание
            # (2026-10-12): моделите почти винаги го пишат, и всеки файл
            # започваше с празен ред — а `#!/bin/sh` на втори ред не е shebang.
            # Само един и само отпред; краят на файла остава какъвто е написан.
            actions.append((start, kind, partial(
                _safe_tool, "WRITE_FILE", _tool_write_file, m.group("path"),
                m.group("body").removeprefix("\r\n").removeprefix("\n"))))
        elif kind == "EDIT_FILE":
            actions.append((start, kind, partial(_edit_action, m)))
        else:  # USE_SKILL
            actions.append((start, kind, partial(
                _safe_tool, "USE_SKILL", _tool_use_skill, m.group("name"), m.group("body"))))

    # ── 1b. USE_SKILL БЕЗ затварящ [END_USE_SKILL] ──────────────────────────
    # Най-скъпият пропуск в целия парсер (bug fix, 2026-09-20). driver кодът е
    # ИЗРИЧНО опционален — `use_skill()` документира, че при празен driver само
    # зарежда умението и пуска self-test-а му — така че `[USE_SKILL: име]` сам
    # по себе си е напълно валидната минимална форма и точно тази форма моделът
    # пише най-често. Дотук тя не съвпадаше с НИЩО: `_USE_SKILL_RE` изисква
    # затварящия таг, а `_SIMPLE_RE` не познава USE_SKILL. Резултат: нула
    # изпълнени тула за реплика, която ВИДИМО вика умение. Викащият цикъл
    # (agent_core.run_tool_loop / терминалът) тогава хваща само
    # `looks_like_attempted_tool_tag`, праща "сгрешил си синтаксиса", моделът
    # пише същото пак — и рундовете изгарят, без нито един скил да е тръгнал.
    # Тук consume-ваме САМО самия таг (не остатъка от отговора), за да могат
    # другите тагове след него пак да се изпълнят.
    def _bare_skill_action(m: re.Match) -> str:
        out = _safe_tool("USE_SKILL", _tool_use_skill, m.group("name"), "")
        # Изпълнено е — но ако моделът е искал да ВИКА нещо от умението, нека
        # научи точния синтаксис, вместо да гадае пак следващия рунд. Само при
        # УСПЕШНО заредено умение: ако такова изобщо няма, синтаксисът на
        # driver кода е без значение и бележката е чист шум в контекста.
        if not any(marker in out for marker in _USE_SKILL_FAILURES):
            out += ("\n(Без driver код — блокът не беше затворен. За да извикаш функция "
                    "от умението: [USE_SKILL: име]<твоят код>[END_USE_SKILL].)")
        return out

    for m in _BARE_USE_SKILL_RE.finditer(response_text):
        if _inside_block(m.start()) or _quoted(m.start()):
            continue
        consumed_spans.append((m.start(), m.end()))
        actions.append((m.start(), "USE_SKILL", partial(_bare_skill_action, m)))

    # ── 2. Едноредови тулове — прескачаме тези вътре в блоков таг ───────────
    for m in _SIMPLE_RE.finditer(response_text):
        if _inside_block(m.start()) or _quoted(m.start()):
            continue
        tool = m.group("tool")
        arg = m.group("arg")
        slot = _XML_SLOT_RE.fullmatch(arg.strip())
        if slot and int(slot.group(1)) < len(slots):
            arg = slots[int(slot.group(1))]     # дословната XML стойност (виж _xml_tag)
        actions.append((m.start(), tool, partial(
            _safe_tool, tool, _SIMPLE_DISPATCH[tool], arg)))

    # 3. BROWSER_READ — без аргумент. 3b. REPO_MAP без аргумент — картира
    # текущия workspace. 4. TASK_LIST без аргумент — показва отворените нишки.
    no_arg: tuple[tuple[str, re.Pattern, Callable[[], str]], ...] = (
        ("BROWSER_READ", _BROWSER_READ_RE, partial(_safe_tool, "BROWSER_READ", _tool_browser_read)),
        ("REPO_MAP", _REPO_MAP_RE, partial(_safe_tool, "REPO_MAP", _tool_repo_map, "")),
        ("TASK_LIST", _TASK_LIST_RE, partial(_safe_tool, "TASK_LIST", _tool_task_list, "open")))
    for name, rx, thunk in no_arg:
        for m in rx.finditer(response_text):
            if _inside_block(m.start()) or _quoted(m.start()):
                continue
            actions.append((m.start(), name, thunk))

    # Изпълняваме по позиция в текста (sort е стабилен; позициите не се дублират).
    actions.sort(key=lambda a: a[0])
    results: list[str] = []
    asked = False
    for _pos, name, thunk in actions:
        if asked:
            # Таговете СЛЕД ASK_USER чакат отговора (bug fix, 2026-10-11).
            # Измерено: „[ASK_USER: Да презапиша ли notes.txt?]“ + WRITE_FILE
            # в същата реплика записваше файла, преди операторът да отговори.
            # Резултат-заместител вместо мълчание — моделът трябва да знае, че
            # не е направено.
            results.append(f"[{name}] не е изпълнено — чака отговора на оператора.")
            continue
        out = thunk()
        results.append(out)
        if name == "ASK_USER" and ASK_USER_MARKER in out:
            asked = True
    return results


# Всяко скоби-подобно "[ГЛАВНИ_БУКВИ:" или "[ГЛАВНИ_БУКВИ]" в текста — независимо
# дали е познат таг. Ползва се САМО след като parse_and_execute_tools вече е
# върнал [], за да различим "моделът приключи" от "моделът се опита да викне
# tool, но обърка синтаксиса/името" (грешно име, липсващо двоеточие, липсващ
# [END_WRITE] и т.н.) — вторият случай иначе тихо се третираше като финален
# отговор (виж git история: "Fix local model narrating tool use without ever
# executing" / "Fix Genesis handing work back instead of doing it").
_ATTEMPTED_TAG_RE = re.compile(r"\[(?P<name>[A-Z][A-Z_]{2,30})(?P<end>:|\])")
# Не всяка дума в скоби е опит (одит 2026-10-22): верен финален отговор с
# `[ERROR]` / `[INFO]` (нива в лог), `[DONE]` или `os.environ[PATH]` получаваше
# „не намерих валиден tool таг“ — още едно платено обръщение и втори, различен
# отговор пред оператора (измерено: 2 обаждания вместо 1). Броят се: нашите
# имена (и в `код` — там не се изпълняват), близки до тях (READFILE, RUN_CMDS)
# и измислени с аргумент ([INSTALL: requests]) извън `код`.
_KNOWN_TAG_NAMES = frozenset(_SIMPLE_DISPATCH) | {
    "WRITE_FILE", "EDIT_FILE", "USE_SKILL", "BROWSER_READ", "REPO_MAP", "TASK_LIST",
    "END_WRITE", "END_EDIT", "END_USE_SKILL"}


def _near_known_tag(name: str) -> bool:
    # До 1 поправка за кратките имена: с 2 `[API]` беше „близко“ до MCP.
    def limit(k: str) -> int:
        return 1 if min(len(name), len(k)) < 7 else 2
    return any(_edit_distance(name, k, limit(k)) <= limit(k) for k in _KNOWN_TAG_NAMES)


def looks_like_attempted_tool_tag(response_text: str) -> bool:
    """True ако текстът съдържа нещо, което прилича на tool tag, но нито един
    истински таг не е разпознат от parse_and_execute_tools за него."""
    if not response_text:
        return False
    # `<invoke name="…">`, останал след _normalize_xml_tool_calls, също е опит
    # за инструмент — иначе ходът свършваше тихо (2026-10-12, виж горе).
    if "<invoke name=" in response_text or "<tool_call>" in response_text:
        return True
    quoted = _quoted_spans(response_text, [])
    for m in _ATTEMPTED_TAG_RE.finditer(response_text):
        name = m.group("name")
        if name in _KNOWN_TAG_NAMES:
            return True
        if any(s <= m.start() < e for s, e in quoted):
            continue
        if m.group("end") == ":" or _near_known_tag(name):
            return True
    return False


def malformed_tag_note(response_text: str) -> str:
    """Системната бележка към модела, когато looks_like_attempted_tool_tag е True.

    Незатворен WRITE_FILE/EDIT_FILE (одит 2026-10-22) почти винаги е отрязан на
    изходния таван: „грешен синтаксис“ караше модела да пише същия голям файл
    пак и ходът свършваше без файл. Подсказката е като при native пътя
    (dispatch_tool_call, „Аргументите са отрязани“)."""
    text = response_text or ""
    # Таг, само споменат в `код` (ревю 2026-10-22): „създадох го с
    # `[WRITE_FILE: app.py]`“ не е отрязан запис — „раздели записа“ пращаше
    # модела да пише файла пак. Цитатите се махат, преди да се търси блокът.
    cuts = [0, *(p for span in _quoted_spans(text, []) for p in span), len(text)]
    block = open_block_tag("".join(text[a:b] for a, b in zip(cuts[::2], cuts[1::2])))
    if block:
        end = "[END_WRITE]" if block == "WRITE_FILE" else "[END_EDIT]"
        return (f"[Система]: Блокът [{block}: …] няма затварящ {end} — нищо не е "
                "изпълнено. Най-често отговорът е отрязан на изходния таван на модела "
                "(твърде голям файл). Раздели записа: по-малки файлове или WRITE_FILE с "
                "началото и EDIT_FILE за останалото — не пиши същия голям блок пак.")
    return ("[Система]: В последния отговор не намерих валиден tool таг, но той изглежда "
            "като опит за такъв. Ако си искал да викнеш инструмент — използвай точния "
            "синтаксис [TAG: аргумент] (или [WRITE_FILE: път]...[END_WRITE] за файлове) "
            "без ` или ``` около него: таг в `код` или в ``` ограда е само текст и не се "
            "изпълнява. Ако вече си приключил — дай кратък финален отговор БЕЗ скоби във "
            "формат на таг.")


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
        try:
            arguments = load_tool_arguments(arguments)
        except (ValueError, TypeError) as e:
            msg = f"[{name}] Невалидни аргументи (не са валиден JSON): {arguments[:200]}"
            # JSON, свършил посред низ/обект, е отрязан на изходния таван
            # (ревю 2026-10-11): Brain го връща, щом го отреже всеки модел, и
            # без тази подсказка моделът пращаше същия голям запис пак.
            if isinstance(e, json.JSONDecodeError) and (
                    e.msg.startswith("Unterminated string") or e.pos >= len(arguments.rstrip())):
                msg += ("\nАргументите са отрязани — по-дълги са от изходния таван на "
                        "модела. Раздели записа: по-малки файлове или WRITE_FILE с "
                        "началото и EDIT_FILE за останалото.")
            return msg
    if not isinstance(arguments, dict):
        arguments = {}
    blocked = _before_tool(name, arguments)
    if blocked:
        return blocked
    return _after_tool(name, arguments, _dispatch(name, arguments))


def _dispatch(name: str, arguments: dict) -> str:
    try:
        if name == "READ_FILE":
            return _tool_read_file(arguments.get("path", ""),
                                   arguments.get("offset"), arguments.get("limit"))
        if name == "WRITE_FILE":
            return _tool_write_file(arguments.get("path", ""), arguments.get("content", ""),
                                    str(arguments.get("exact", "")).strip().lower()
                                    in ("true", "1", "yes"))
        if name == "EDIT_FILE":
            return _tool_edit_file(arguments.get("path", ""),
                                   arguments.get("old", ""),
                                   arguments.get("new", ""),
                                   # „false“ като низ е истина в Python: заменяше
                                   # всички съвпадения вместо отказ (одит 2026-10-09)
                                   str(arguments.get("replace_all", "")).strip().lower()
                                   in ("true", "1", "yes"))
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
            # „false“ като низ е истина в Python — фонът само при изрично да (одит 2026-10-08)
            if str(arguments.get("background", "")).strip().lower() in ("true", "1", "yes"):
                return _tool_run_bg(arguments.get("command", ""))
            return _tool_run_cmd(arguments.get("command", ""))
        if name == "EXPLORE":
            return _tool_explore(str(arguments.get("question", "") or ""))
        if name == "AGENT":
            return _tool_agent(str(arguments.get("agent", "") or ""),
                               str(arguments.get("task", "") or ""))
        if name == "TODO_WRITE":
            # Без "todos" — отказ, не празен списък (проба 2026-10-12: липсващ
            # ключ мълчаливо изтриваше задачите). Друго име — само ако е списък.
            todo_list = arguments.get("todos")
            for alias in ("tasks", "items"):
                if todo_list is None and isinstance(arguments.get(alias), list):
                    todo_list = arguments[alias]
            return _todos().write(todo_list, native=True)
        if name == "WEB_FETCH":
            return _tool_web_fetch(str(arguments.get("url", "") or ""))
        if name.startswith("mcp__"):
            return _mcp().call(name, arguments)
        if name == "BG_OUTPUT":
            return _background().output(str(arguments.get("id", "") or ""))
        if name == "BG_KILL":
            return _background().kill(str(arguments.get("id", "") or ""))
        if name == "ASK_USER":
            return _tool_ask_user(arguments.get("question", ""),
                                  arguments.get("options"))
        if name == "WEB_SEARCH":
            return _tool_web_search(arguments.get("query", ""))
        if name == "RESEARCH":
            return _tool_research(arguments.get("question", ""))
        if name == "LIST_DIR":
            return _tool_list_dir(arguments.get("path", ""))
        if name == "VIEW_IMAGE":
            return _tool_view_image(str(arguments.get("path", "") or ""))
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
                    value = str(arguments.get("value", "") or "")
                    # Схемата иска само kind и value, а празна тема се
                    # отказваше: моделът, спазил схемата, губеше извикването
                    # (одит 2026-10-22). Темата — първите думи на стойността.
                    topic = str(arguments.get("topic", "") or "").strip() \
                        or " ".join(value.split()[:8])[:60]
                    return wm.set_preference(topic, value)
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
        log.debug("инструментът %s гръмна", name, exc_info=True)
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
