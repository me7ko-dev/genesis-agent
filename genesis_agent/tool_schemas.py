"""
genesis_agent/tool_schemas.py — OpenAI-формат tool schemas за native function-calling.

Едно и също множество инструменти вече има ДВЕ лица:
  1. Текстови тагове ([RUN_CMD: ...]) — регекс парсене в genesis_skills.py,
     работи навсякъде (дори локални модели без function-calling), но е
     крехко (моделът може да сгреши синтаксиса/името на функцията).
  2. Native OpenAI `tools=[...]` — по-надеждно (структуриран JSON, моделът
     не може да обърка синтаксиса на самия таг), но не всеки доставчик/модел
     го поддържа (проверено наживо 2026-07-25, виж config.yaml supports_tools).

И двата пътя изпълняват СЪЩИЯ код (genesis_skills._tool_*) — само форматът
на извикването/резултата е различен. Тук дефинираме само схемите; диспечът
на native извиквания е в genesis_skills.dispatch_tool_call().
"""
from __future__ import annotations

import json
import re

FULL_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "READ_FILE",
            "description": "Read a file: the first 8000 chars, or a numbered range with offset "
                           "(1-indexed line) + limit (lines) — needed past ~150 lines and before editing there.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "GLOB",
            "description": "Find files by name, e.g. '**/*.py' (contents: SEARCH_CODE).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "default: workspace"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "WRITE_FILE",
            "description": "Create or overwrite a whole file. An existing file must be read "
                           "first in this session, or it is refused. Partial change: EDIT_FILE.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    # ── Работа по ЧУЖД код (design note, 2026-07-30) ──────────────────────────────
    # WRITE_FILE презаписва целия файл. Върху файл, който агентът сам е написал,
    # това е нормално; върху чужд модул от 2000 реда значи "препиши го по памет"
    # и всичко незапомнено изчезва тихо. EDIT_FILE е закотвена замяна: anchor-ът
    # трябва вече да съществува, да е уникален, и резултатът трябва още да се
    # парсва — иначе файлът НЕ се пипа. Виж genesis_agent/code_edit.py.
    {
        "type": "function",
        "function": {
            "name": "EDIT_FILE",
            "description": "Replace an exact snippet of an existing file; returns the diff. "
                           "'old' must occur once, whitespace included (add context lines). An "
                           "edit breaking Python syntax is refused. Prefer over WRITE_FILE for "
                           "files you didn't just write.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "SEARCH_CODE",
            "description": "Regex search in file contents (grep -rn) — find code before reading.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "default: workspace"},
                    "glob": {"type": "string", "description": "e.g. *.py"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "REPO_MAP",
            "description": "Project summary: language, layout, entry points, test command, "
                           "git. Call first on an unfamiliar codebase.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "default: workspace"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "RUN_CMD",
            "description": "Run a shell command through the sandbox (dangerous ones ask the "
                           "operator or are blocked).",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "background": {"type": "boolean",
                                   "description": "true for a dev server/watcher: returns an id "
                                                  "at once; BG_OUTPUT reads it, BG_KILL stops it"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "WEB_FETCH",
            "description": ("Read one web page as text (docs, changelogs, API references). "
                            "WEB_SEARCH finds pages, this reads one. Internal addresses are refused."),
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string"}},
                "required": ["url"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "EXPLORE",
            "description": ("A read-only sub-agent answers a question about the code (where is X, "
                            "who calls Y) from its OWN context, with path:line references. Use it "
                            "instead of many SEARCH_CODE/READ_FILE calls on code you haven't seen."),
            "parameters": {"type": "object", "properties": {
                "question": {"type": "string"}},
                "required": ["question"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "TODO_WRITE",
            "description": ("Task list shown to the operator, for work with 3+ steps: write it "
                            "all first, then rewrite as you go — one item in_progress, each "
                            "completed as soon as done."),
            "parameters": {"type": "object", "properties": {
                "todos": {"type": "array", "items": {"type": "object", "properties": {
                    "content": {"type": "string"},
                    "status": {"type": "string",
                               "enum": ["pending", "in_progress", "completed"]}},
                    "required": ["content", "status"]}}},
                "required": ["todos"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "BG_OUTPUT",
            "description": "New output of a background command and whether it still runs; "
                           "without id, the list of them.",
            "parameters": {"type": "object",
                           "properties": {"id": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "BG_KILL",
            "description": "Stop a background command (and its children).",
            "parameters": {"type": "object",
                           "properties": {"id": {"type": "string"}}, "required": ["id"]},
        },
    },
    # ── Питане при неяснота (design note, 2026-07-27) ──────────────────────────────
    # Дотук "питай, ако не си сигурен" беше само инструкция в промпта, която се
    # бореше с далеч по-силната "ПРАВИ, не връщай списък със задачи" — и губеше.
    # Реален докладван случай: помолен да премести снимки, агентът гадаеше
    # кои/откъде/накъде вместо да попита, и на третия опит тръгна по грешните
    # файлове. Питането трябва да е ИНСТРУМЕНТ (евтин, легитимен, спира цикъла),
    # не поведение, което моделът трябва да си пробие сам срещу промпта.
    {
        "type": "function",
        "function": {
            "name": "ASK_USER",
            "description": "Ask the user and wait. Use BEFORE acting when it is unclear which "
                           "files, from/to where, or overwrite vs keep — never guess on a bulk "
                           "or destructive operation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "in the user's language"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "WEB_SEARCH",
            "description": "Search the web and return raw result snippets.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "RESEARCH",
            "description": "Web research cross-checked across several sources; prefer over "
                           "WEB_SEARCH when accuracy matters.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "LIST_DIR",
            "description": "List the contents of a directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "USE_SKILL",
            "description": "Run a verified library skill (name or description). Without "
                           "driver_code: lists its functions; driver_code is Python calling them "
                           "by name (no import). Try before writing a common utility.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name_or_query": {"type": "string"},
                    "driver_code": {"type": "string"},
                },
                "required": ["name_or_query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "DELEGATE",
            "description": "Hand a self-contained coding task to a sub-agent mission that "
                           "writes and verifies new code (heavier than USE_SKILL).",
            "parameters": {
                "type": "object",
                "properties": {"goal": {"type": "string"}},
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "BROWSE",
            "description": "Open a URL in an isolated headless browser (no saved logins). "
                           "Returns title, text and numbered clickable/fillable elements.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "BROWSER_READ",
            "description": "Re-read the current browser page (e.g. after a click).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "BROWSER_CLICK",
            "description": "Click an element from the numbered list returned by BROWSE/BROWSER_READ. "
                           "Payment/order-confirmation buttons are always blocked.",
            "parameters": {
                "type": "object",
                "properties": {"index_or_text": {"type": "string"}},
                "required": ["index_or_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "BROWSER_TYPE",
            "description": "Type text into a field from the numbered element list. "
                           "Password/card/CVV fields are always blocked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index_or_text": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["index_or_text", "text"],
            },
        },
    },
    # ── Памет за РАБОТАТА (design note, 2026-07-25) ────────────────────────────────
    # Дотук Genesis нямаше НИТО ЕДИН инструмент за запис в собствената си
    # памет — само четеше инжектираното при старт, значи всяка сесия
    # започваше с амнезия за същината на работата.
    {
        "type": "function",
        "function": {
            "name": "REMEMBER",
            "description": "Save a decision (value + why) or a user preference (topic + "
                           "value) for every future session, as soon as it is clear.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["decision", "preference"]},
                    "topic": {"type": "string"},
                    "value": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["kind", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "TASK_ADD",
            "description": "Open a work thread that survives sessions, for anything left "
                           "unfinished.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "next_step": {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "TASK_UPDATE",
            "description": "Update a thread's status or next step; done as soon as finished.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "from TASK_LIST"},
                    "status": {"type": "string", "enum": ["open", "blocked", "done"]},
                    "next_step": {"type": "string"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "TASK_LIST",
            "description": "List work threads (default: open ones).",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["open", "blocked", "done", "all"]},
                },
            },
        },
    },
]

_BROWSER_TOOLS = frozenset({"BROWSE", "BROWSER_READ", "BROWSER_CLICK", "BROWSER_TYPE"})


def browser_available() -> bool:
    """Инсталиран ли е extra-то `[browser]` (playwright)."""
    import importlib.util
    return importlib.util.find_spec("playwright") is not None


# Без playwright четирите браузърни схеми са ~1100 знака във ВСЯКО обръщение
# за инструменти, които могат само да върнат грешка — и канят модела да ги
# пробва. Премахват се от списъка, не от кода: с `[browser]` се връщат сами.
if not browser_available():
    FULL_TOOLS = [t for t in FULL_TOOLS if t["function"]["name"] not in _BROWSER_TOOLS]


def fit_system_prompt(prompt: str) -> str:
    """Маха абзаца „BROWSER SAFETY" от config.yaml, когато браузърът го няма —
    правила за инструменти, които моделът не вижда, са чист разход."""
    if browser_available():
        return prompt
    import re
    return re.sub(r"BROWSER SAFETY — NON-NEGOTIABLE:.*?\n\s*\n", "", prompt, count=1, flags=re.DOTALL)

# Read-only подмножество — за автономни мисии (autonomous_loop.py), където
# писане/команди минават през отделния code-generation път, не през tool tags.
READONLY_TOOLS: list[dict] = [
    t for t in FULL_TOOLS
    if t["function"]["name"] in {"READ_FILE", "WEB_SEARCH", "RESEARCH", "LIST_DIR", "GLOB"}
]

# Мисии (design note, 2026-07-25, "мисиите с реални умения"): READONLY_TOOLS +
# USE_SKILL — Brain-ът вече може РЕАЛНО да извика съществуващо умение по
# време на генериране (не само да получи кода му инжектиран в промпта през
# build_context композицията). Умишлено БЕЗ RUN_CMD/WRITE_FILE/DELEGATE/
# browser — мисията произвежда Python код като краен резултат (после минава
# през run_python_subprocess + verifier), не sandbox странични ефекти по
# време на самото съставяне.
MISSION_TOOLS: list[dict] = READONLY_TOOLS + [
    t for t in FULL_TOOLS if t["function"]["name"] == "USE_SKILL"
]

# Поправка на ЧУЖД проект (design note, 2026-07-30, genesis_agent/repo_agent.py).
# Умишлено по-тесен набор от FULL_TOOLS:
#   • БЕЗ browser/WEB_SEARCH/RESEARCH — цикълът работи по код на диска, не в мрежата;
#     един URL в traceback-а не бива да отвежда агента да сърфира.
#   • БЕЗ DELEGATE/USE_SKILL — подагент без checkpoint-а и без нишката на този
#     конкретен ремонт произвежда промени, за които никой не отговаря.
#   • БЕЗ ASK_USER — repo_agent се пуска и без надзор (`genesis fix`), а питане
#     там просто увисва; неяснотата се решава чрез спиране с доклад.
#   • С EDIT_FILE, SEARCH_CODE, REPO_MAP и RUN_CMD — намери, промени, пусни тестовете.
REPAIR_TOOLS: list[dict] = [
    t for t in FULL_TOOLS
    if t["function"]["name"] in {
        "READ_FILE", "LIST_DIR", "SEARCH_CODE", "REPO_MAP", "GLOB",
        "EDIT_FILE", "WRITE_FILE", "RUN_CMD",
    }
]


_JSON_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|.)", re.DOTALL)
# В поправката `\b` и `\f` също са буквална черта (2026-10-07): щом моделът е
# писал `\d` сурово, `\b` до него е границата на дума в регекса, не backspace —
# иначе `r"\bcat\d+\b"` стигаше до диска с два невидими \x08 знака.
_KEEP_ESCAPES = {'"', "\\", "/", "n", "r", "t"}


def load_tool_arguments(raw) -> dict:
    """Аргументите на native tool_call като dict.

    Модел, който пише код с регекси, праща `"\\d{4}"` в JSON низ — невалиден
    escape. json.loads пада и callers-ите тихо даваха `{}`: WRITE_FILE без път
    пишеше в самата папка → „[Errno 13] Permission denied“ и моделът питаше
    дали папката е защитена (bench faktura-excel 2026-09-26 и 2026-09-29).
    Невалидният escape става буквална наклонена черта — това е, което моделът
    иска във файла. Иначе поправим → хвърля оригиналната грешка."""
    if isinstance(raw, dict):
        return raw
    text = raw or "{}"
    try:
        value = json.loads(text)
    except json.JSONDecodeError as first:
        fixed = _JSON_ESCAPE.sub(
            lambda m: m.group(0) if m.group(1) in _KEEP_ESCAPES or len(m.group(1)) == 5
            else "\\\\" + m.group(1), text)
        try:
            value = json.loads(fixed)
        except json.JSONDecodeError:
            raise first from None
    if not isinstance(value, dict):
        # Списък/число не е „празни аргументи“: с {} TODO_WRITE триеше списъка
        # (одит 2026-10-09). JSONDecodeError, за да го хващат всички callers.
        raise json.JSONDecodeError("аргументите не са JSON обект", text, 0)
    return value
