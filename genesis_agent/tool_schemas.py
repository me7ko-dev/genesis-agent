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

FULL_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "READ_FILE",
            "description": "Read a file (absolute or workspace-relative). Returns the first "
                           "8000 chars; pass offset (1-indexed line) + limit for a numbered range — "
                           "needed past ~150 lines and before editing there. Several files → "
                           "several READ_FILE calls in the same turn.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to read"},
                    "offset": {"type": "integer", "description": "1-indexed line to start from"},
                    "limit": {"type": "integer", "description": "Max lines to return"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "GLOB",
            "description": "Find files by name pattern, e.g. '**/*.py'. "
                           "For file contents use SEARCH_CODE.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
                    "path": {"type": "string", "description": "Directory to search (default: workspace)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "WRITE_FILE",
            "description": "Create or overwrite a file. An existing file must have been read "
                           "in this session first, or the call is refused. For a partial change "
                           "use EDIT_FILE.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to write"},
                    "content": {"type": "string", "description": "Full file content"},
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
            "description": "Replace an exact snippet in an existing file; returns the diff. "
                           "'old' must occur exactly once (whitespace counts) — add context lines "
                           "to make it unique. An edit that would break Python syntax is refused "
                           "and the file is untouched. Prefer over WRITE_FILE for files you "
                           "didn't just write.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to edit"},
                    "old": {"type": "string", "description": "Exact existing text to replace"},
                    "new": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean",
                                    "description": "Replace every occurrence (default false)"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "SEARCH_CODE",
            "description": "Regex search in file contents (grep -rn). Locate code with this "
                           "before reading files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression"},
                    "path": {"type": "string", "description": "Directory to search (default: workspace)"},
                    "glob": {"type": "string", "description": "Optional filename filter, e.g. *.py"},
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
                    "path": {"type": "string", "description": "Project root (default: workspace)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "RUN_CMD",
            "description": "Run a real shell command through the sandbox (SAFE auto-runs, "
                           "dangerous ones ask the operator or are blocked).",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Shell command"}},
                "required": ["command"],
            },
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
            "description": "Ask the user and wait for the answer. Use BEFORE acting when it is "
                           "unclear which files, from/to where, or overwrite vs keep — never "
                           "guess on a bulk or destructive operation. Asking is not handing "
                           "work back.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The specific question, in the user's language."},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional choices, so the user can answer in one word.",
                    },
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
            "description": "Run a verified skill from the library (name or description, "
                           "fuzzy-matched). Empty driver_code lists its functions; driver_code "
                           "calls them by name (no import) and returns the real result. Try "
                           "before writing a common utility yourself.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name_or_query": {"type": "string", "description": "Exact skill name or a free-text description"},
                    "driver_code": {"type": "string", "description": "Optional Python that calls the skill's functions/classes directly by name"},
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
            "description": "Save a decision (with why) or a user preference. Shown at the "
                           "start of every future session — record it as soon as it is clear.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["decision", "preference"]},
                    "topic": {"type": "string",
                              "description": "For a preference: the topic (e.g. 'commit style')."},
                    "value": {"type": "string",
                              "description": "The decision itself, or the preference value."},
                    "why": {"type": "string", "description": "For a decision: the reason."},
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
                    "next_step": {"type": "string", "description": "Concrete next action."},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "TASK_UPDATE",
            "description": "Update a thread: status or next step. Mark done as soon as "
                           "it is finished.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Thread id from TASK_LIST."},
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
