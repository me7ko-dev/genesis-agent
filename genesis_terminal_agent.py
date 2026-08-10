#!/usr/bin/env python3
"""
Genesis Agent — the terminal frontend.

A full agent in a terminal: the model chain from config.yaml with automatic
fallback on quota or rate limits, real tool execution through the sandbox,
the skill library, workspace memory that survives sessions, and a status bar
showing which provider is actually answering.

The provider chain itself lives in genesis_agent/brain.py — this file owns
only what is genuinely terminal-specific: the /model picker, the status bar,
rich rendering, and the interactive sandbox confirmation prompt.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import yaml
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

try:
    import genesis_skills
except ImportError:
    pass

try:
    from genesis_agent.tool_schemas import FULL_TOOLS as TERMINAL_TOOL_SCHEMAS
except ImportError:
    TERMINAL_TOOL_SCHEMAS: Any = None  # type: ignore[no-redef]

# ── Споделено ядро (genesis_agent): памет + sandbox политика ────────────────────────
# Всичко тук е меко-опционално: ако липсва зависимост, чатът пак работи.
try:
    from genesis_agent import conversation_memory as _conv_mem
    from genesis_agent.memory import memory_context as _memory_context
except Exception:
    _conv_mem: Any = None  # type: ignore[no-redef]
    _memory_context: Any = None  # type: ignore[no-redef]


def _remember(role: str, content: str) -> None:
    """Записва реплика в споделената conversation_memory (никога не хвърля)."""
    if _conv_mem is None or not content:
        return
    try:
        _conv_mem.add_message(role, content)
    except Exception:
        pass


console = Console()

# Терминалът е интерактивен → sandbox-ът пита оператора преди опасни операции,
# вместо да ги отказва тихо. Потвърждението се рендира през rich console.
try:
    from genesis_agent import sandbox as _sandbox

    def _terminal_confirm(operation: str, verdict) -> bool:
        console.print("\n[bold yellow]⚠️  GENESIS SANDBOX — изисква потвърждение[/]")
        for r in verdict.reasons:
            console.print(f"    [yellow]• {r}[/]")
        console.print(f"    [dim]Операция:[/] {operation[:300]}")
        try:
            ans = console.input("    [bold]Да се изпълни ли? [y/N] [/]").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return ans in ("y", "yes", "да", "d")

    _sandbox.set_policy(_sandbox.SandboxPolicy(mode="interactive", confirm_fn=_terminal_confirm))
except Exception:
    pass

# --- Load Config ---
# All paths come from genesis_agent.paths, which derives them from the
# installed package and the user's own home — nothing machine-specific here.
from genesis_agent.paths import (
    CONFIG_PATH,
    ENV_FILES,
    _strip_inline_comment,
    history_dir,
    workspace_dir,
)

try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
except Exception as e:
    console.print(f"[red]Грешка при зареждане на config.yaml: {e}[/]")
    sys.exit(1)

# config.yaml may override the workspace; empty (the default) means "use the
# project directory".
_ws = (config.get("workspace") or {}).get("path") or ""
WORKSPACE = Path(_ws).expanduser() if _ws else workspace_dir()
try:
    genesis_skills.set_workspace(WORKSPACE)
except NameError:
    pass

_hd = (config.get("storage") or {}).get("history_dir") or ""
HISTORY_DIR = Path(_hd).expanduser() if _hd else history_dir()
HISTORY_DIR.mkdir(parents=True, exist_ok=True)


# Estimated context window (adjust per model)
DEFAULT_CONTEXT_WINDOW = config.get("models", {}).get("context_window", 128000)


# ── Session tracking ──────────────────────────────────────────────────────────
session_start_time = time.time()
total_input_tokens = 0
total_output_tokens = 0

def estimate_tokens(text: str) -> int:
    """Rough estimate: ~4 chars per token for mixed Bulgarian/English."""
    return len(text) // 4

def get_elapsed_time() -> str:
    """Returns elapsed time as MM:SS or HH:MM:SS."""
    elapsed = int(time.time() - session_start_time)
    if elapsed >= 3600:
        return f"{elapsed // 3600}h {(elapsed % 3600) // 60}m"
    elif elapsed >= 60:
        return f"{elapsed // 60}m {elapsed % 60}s"
    return f"{elapsed}s"

def get_context_stats() -> tuple:
    """Returns (used_tokens, remaining, percentage)."""
    used = total_input_tokens + total_output_tokens
    remaining = max(0, DEFAULT_CONTEXT_WINDOW - used)
    pct = min(100, int((used / DEFAULT_CONTEXT_WINDOW) * 100))
    return used, remaining, pct

# ── Status bar ────────────────────────────────────────────────────────────────
def build_status_bar() -> Text:
    """Build one-line status bar compact, one line."""
    model_short = current_model_id.split("/")[-1] if "/" in current_model_id else current_model_id
    if len(model_short) > 20:
        model_short = model_short[:17] + "..."

    elapsed = get_elapsed_time()
    ctx_used, ctx_remain, ctx_pct = get_context_stats()

    # Format context: K for thousands
    ctx_used_str = f"{ctx_used // 1000}K" if ctx_used > 1000 else str(ctx_used)
    ctx_total_str = f"{DEFAULT_CONTEXT_WINDOW // 1000}K"

    status = Text()
    status.append(" ⚕ ", style="cyan")
    status.append(model_short, style="bold cyan")
    status.append(" │ ", style="dim")
    status.append(f"⏱ {elapsed}", style="yellow")
    status.append(" │ ", style="dim")
    status.append(f"ctx {ctx_used_str}/{ctx_total_str}", style="magenta")
    status.append(f" ({ctx_pct}%)", style="dim magenta")
    status.append(" │ ", style="dim")
    status.append(f"~{ctx_remain // 1000}K left", style="green")

    return status

# ── API Keys ──────────────────────────────────────────────────────────────────
KEYS = {
    "GROQ_API_KEY": "", "GEMINI_API_KEY": "", "OPENROUTER_API_KEY": "",
    "NVIDIA_API_KEY": "", "GITHUB_TOKEN": "", "OPENAI_API_KEY": "",
    "HF_TOKEN": "", "COHERE_API_KEY": "", "OLLAMA_API_KEY": "",
    "GENESIS_DISCORD_WEBHOOK": config.get("discord", {}).get("webhook", ""),
    "GENESIS_DISCORD_BOT_TOKEN": config.get("discord", {}).get("bot_token", ""),
    "OLLAMA_MODEL": config.get("models", {}).get("ollama_model", "llama3.2")
}

def load_env(path):
    if not path.exists(): return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        line = line.removeprefix("export ").strip()
        if "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), _strip_inline_comment(v.strip()).strip('"').strip("'")
            if k in KEYS and v: KEYS[k] = v

# Real environment variables win over the .env files, matching
# paths.get_secret. The loop below used to run the other way round, so an
# exported key meant to override a stale one in ~/.genesis/.env did nothing.
for _p in ENV_FILES:
    load_env(Path(_p))
for k in KEYS:
    if os.environ.get(k):
        KEYS[k] = os.environ[k]

# Opt-in ollama_cloud multi-key rotation lives in genesis_agent.brain (the
# actual chat completions go through Brain — see the two `from
# genesis_agent.brain import Brain` calls below), so it already works even
# though KEYS/load_env above only ever recognize the bare OLLAMA_API_KEY.
# This flag exists ONLY so the status panel's "0/9 активни" line and the
# "no key configured" warning do not lie to the operator when the real key
# lives at OLLAMA_API_KEY_2.._10 instead of the bare name — it changes no
# request-routing behavior, only what gets printed.
_OLLAMA_CLOUD_EXTRA_KEYS = [f"OLLAMA_API_KEY_{i}" for i in range(2, 11)]
_ollama_cloud_multi: dict[str, bool] = {}
for _p in ENV_FILES:
    if not Path(_p).exists():
        continue
    for _line in Path(_p).read_text(encoding="utf-8", errors="replace").splitlines():
        _line = _line.strip().removeprefix("export ").strip()
        if "=" not in _line or _line.startswith("#"):
            continue
        _k, _v = _line.split("=", 1)
        _k = _k.strip()
        if _k in _OLLAMA_CLOUD_EXTRA_KEYS and _strip_inline_comment(_v.strip()).strip('"').strip("'"):
            _ollama_cloud_multi[_k] = True
for _k in _OLLAMA_CLOUD_EXTRA_KEYS:
    if os.environ.get(_k):
        _ollama_cloud_multi[_k] = True
HAS_OLLAMA_CLOUD_KEY = bool(KEYS.get("OLLAMA_API_KEY") or _ollama_cloud_multi)

# ── Providers & Models ────────────────────────────────────────────────────────
PROVIDERS = {
    "groq":         {"name": "⚡ Groq",              "key_env": "GROQ_API_KEY",       "base_url": "https://api.groq.com/openai/v1",                        "type": "openai"},
    "gemini":       {"name": "✨ Gemini",             "key_env": "GEMINI_API_KEY",    "base_url": "https://generativelanguage.googleapis.com/v1beta/models",  "type": "gemini"},
    "openrouter":   {"name": "🌌 OpenRouter",         "key_env": "OPENROUTER_API_KEY","base_url": "https://openrouter.ai/api/v1",                          "type": "openai"},
    "nvidia":       {"name": "🟢 NVIDIA NIM",         "key_env": "NVIDIA_API_KEY",    "base_url": "https://integrate.api.nvidia.com/v1",                   "type": "openai"},
    "github":       {"name": "🐙 GitHub Models",      "key_env": "GITHUB_TOKEN",      "base_url": "https://models.inference.ai.azure.com",                "type": "openai"},
    "openai":       {"name": "🧠 OpenAI",             "key_env": "OPENAI_API_KEY",    "base_url": "https://api.openai.com/v1",                            "type": "openai"},
    # base_url беше api-inference.huggingface.co — МЪРТЪВ хост (ConnectionError,
    # проверено 2026-07-25). Всяко съобщение хабеше 4 неуспешни опита (4-те HF
    # модела) преди да падне надолу. brain.py открай време ползва работещия
    # router.huggingface.co — още едно последствие от двете паралелни вериги.
    "huggingface":  {"name": "🤗 Hugging Face",       "key_env": "HF_TOKEN",          "base_url": "https://router.huggingface.co/v1",                     "type": "openai"},
    "cohere":       {"name": "🟠 Cohere",             "key_env": "COHERE_API_KEY",    "base_url": "https://api.cohere.ai/compatibility/v1",               "type": "openai"},
    # Ollama LOCAL — localhost, без ключ, native /api/chat endpoint
    "ollama":       {"name": "🏠 Ollama (Local)",     "key_env": None,                "base_url": "http://localhost:11434",                                "type": "ollama"},
    # Ollama CLOUD — ollama.com/v1, OpenAI-compatible, изисква OLLAMA_API_KEY
    "ollama_cloud": {"name": "☁️  Ollama (Cloud)",    "key_env": "OLLAMA_API_KEY",    "base_url": "https://ollama.com/v1",                                 "type": "openai"},
    "llmstudio":    {"name": "🖥️  LLM Studio",        "key_env": None,                "base_url": "http://127.0.0.1:1234/v1",                             "type": "openai"},
}
FALLBACKS = {
    "groq":         ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
    "nvidia":       ["meta/llama-3.1-405b-instruct"],
    "github":       ["gpt-4o", "gpt-4o-mini"],
    "openai":       ["gpt-4o", "gpt-4o-mini"],
    "openrouter":   ["meta-llama/llama-3.3-70b-instruct:free", "openai/gpt-4o"],
    "huggingface":  ["Qwen/Qwen2.5-72B-Instruct", "mistralai/Mixtral-8x7B-Instruct-v0.1"],
    "cohere":       ["command-a-plus-05-2026", "command-a-03-2025", "command-r-plus-08-2024", "command-r-08-2024", "command-r7b-12-2024"],
    "ollama":       [KEYS["OLLAMA_MODEL"]],
    "ollama_cloud": ["llama3.2", "llama3.1:8b", "mistral", "phi3"],
    "llmstudio":    ["local-model"],
}

MODELS_CACHE: dict[str, list[str]] = {}
current_provider = config.get("models", {}).get("default_provider", "groq")
current_model_id = config.get("models", {}).get("default_model_id", "llama-3.3-70b-versatile")

# Fallback chain from config (in order, tried on quota/rate-limit errors).
# min 32B (design note, 2026-07-25): терминалният чат е основният coding assistant —
# по-малките модели мислят забележимо по-слабо. config.yaml's size_b анотира
# всеки запис; default_model_id (Qwen2.5-Coder-32B, 32B) вече го покрива.
_TERMINAL_MIN_SIZE_B = 32

# `/maxcoding` — за сесията, не за постоянно. Обичайната верига е компромис
# между качество, скорост и квота, което е правилният компромис за разговор;
# работата по код иска другия. Изборът е изричен и се вижда в /status.
_CODING_MODE = False

# `/local_model_max` (14B, най-мощният локален) и `/local_model_normal` (7B,
# по-лек и по-бърз) — изричен офлайн режим за сесията, вкл./изкл. като
# /maxcoding. None = обичайният облак-пръв ред; иначе държи tag-а на
# инсталирания Ollama модел, който в момента е форсиран. Прилагането
# (env + brain.LOCAL_MODEL) е споделено с GUI-то — виж genesis_agent.brain.set_local_only.
_LOCAL_ONLY_MODEL: str | None = None

FALLBACK_CHAIN = [
    {"provider": fb.get("provider", "groq"), "model": fb.get("model", "")}
    for fb in config.get("models", {}).get("fallback_models", [])
    if fb.get("model") and fb.get("size_b", 0) >= _TERMINAL_MIN_SIZE_B
]

# Native tool-calling support per (provider, model) — тествано наживо
# 2026-07-25 (виж config.yaml supports_tools за пълния коментар/метод).
_SUPPORTS_TOOLS = {
    (fb.get("provider", ""), fb.get("model", "")): bool(fb.get("supports_tools", False))
    for fb in config.get("models", {}).get("fallback_models", [])
}

# FREE model detection
FREE_PROVIDERS = {"groq", "ollama"}  # local Ollama is free; Ollama Cloud has usage limits/subscription gates
FREE_MODEL_PATTERNS = [":free", "command-r", "command-a"]  # cohere free tier

def is_free_model(provider_key: str, model_id: str) -> bool:
    if provider_key in FREE_PROVIDERS:
        return True
    ml = model_id.lower()
    return any(p in ml for p in FREE_MODEL_PATTERNS)

def model_badge(provider_key: str, model_id: str) -> str:
    return "[green bold]FREE[/]" if is_free_model(provider_key, model_id) else "[yellow dim]PAID[/]"

# ── API Calls ────────────────────────────────────────────────────────────────
def fetch_models(provider_key):
    if provider_key in MODELS_CACHE:
        return MODELS_CACHE[provider_key]
    p = PROVIDERS[provider_key]
    key = KEYS.get(p["key_env"] or "", "")
    if p["type"] == "openai":
        try:
            r = requests.get(f"{p['base_url']}/models",
                             headers={"Authorization": f"Bearer {key}"}, timeout=10)
            if r.status_code == 200:
                models = sorted([m.get("id","") for m in r.json().get("data",[])])
                if models:
                    MODELS_CACHE[provider_key] = models
                    return models
        except Exception: pass
    elif p["type"] == "gemini":
        try:
            r = requests.get(f"{p['base_url']}?key={key}", timeout=10)
            if r.status_code == 200:
                models = [m["name"].replace("models/","") for m in r.json().get("models",[])
                          if "generateContent" in m.get("supportedGenerationMethods",[])]
                MODELS_CACHE[provider_key] = models
                return models
        except Exception: pass
    elif p["type"] == "ollama":
        # Local Ollama — use /api/tags
        try:
            r = requests.get(f"{p['base_url']}/api/tags", timeout=5)
            if r.status_code == 200:
                ollama_models = [m["name"] for m in r.json().get("models", [])]
                if ollama_models:
                    MODELS_CACHE[provider_key] = ollama_models
                    return MODELS_CACHE[provider_key]
                else:
                    MODELS_CACHE[provider_key] = ["__no_models__"]
                    return MODELS_CACHE[provider_key]
        except Exception:
            pass
    MODELS_CACHE[provider_key] = FALLBACKS.get(provider_key, [])
    return MODELS_CACHE[provider_key]

# Реален usage от ПОСЛЕДНОТО успешно извикване (не estimate_tokens() оценка) —
# попълва се от call_openai_compatible/call_gemini/call_ollama, чете се в
# ask_genesis(). None = доставчикът не върна usage, ask_genesis пада на estimate.
_last_usage = None


def call_openai_compatible(messages, provider_key, model_id, tools=None):
    global _last_usage
    p = PROVIDERS[provider_key]
    key = KEYS.get(p["key_env"] or "", "")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": "Mozilla/5.0"}
    if provider_key == "openrouter":
        headers.update({"HTTP-Referer": "http://localhost:3000", "X-Title": "Genesis Agent"})
    payload = {"model": model_id, "messages": messages, "temperature": 0.5, "max_tokens": 2048}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    try:
        r = requests.post(f"{p['base_url']}/chat/completions",
                          json=payload, headers=headers, timeout=60)
        if r.status_code == 200:
            data = r.json()
            choice = data.get("choices", [{}])[0]
            message = choice.get("message") or {}
            usage = data.get("usage")
            if usage:
                _last_usage = {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                }
            return message.get("content", "").strip(), (message.get("tool_calls") or None)
        # Parse error message
        try:
            err_body = r.json()
            err_msg = err_body.get("error", {}).get("message", r.text[:120])
        except Exception:
            err_msg = r.text[:120]
        raise RuntimeError(f"HTTP_{r.status_code}: {err_msg}")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"CONN: {e}")

def call_gemini(messages, model_id):
    global _last_usage
    key = KEYS["GEMINI_API_KEY"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={key}"
    g_msgs, sys_p = [], ""
    for m in messages:
        if m["role"] == "system": sys_p += m["content"] + "\n"
        elif m["role"] == "user": g_msgs.append({"role":"user","parts":[{"text":m["content"]}]})
        elif m["role"] == "assistant": g_msgs.append({"role":"model","parts":[{"text":m["content"]}]})
    payload = {"contents": g_msgs}
    if sys_p: payload["systemInstruction"] = {"parts":[{"text":sys_p}]}
    r = requests.post(url, json=payload, headers={"Content-Type":"application/json"}, timeout=60)
    if r.status_code == 200:
        data = r.json()
        usage = data.get("usageMetadata")
        if usage:
            _last_usage = {
                "prompt_tokens": usage.get("promptTokenCount", 0),
                "completion_tokens": usage.get("candidatesTokenCount", 0),
            }
        try: return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception: return "[Грешка: Празен отговор]"
    return f"[Грешка {r.status_code}]"

def call_ollama(messages, model_id):
    """Local Ollama via native /api/chat endpoint."""
    global _last_usage
    try:
        r = requests.post(
            f"{PROVIDERS['ollama']['base_url']}/api/chat",
            json={"model": model_id, "messages": messages, "stream": False},
            timeout=180
        )
        if r.status_code == 200:
            data = r.json()
            if "prompt_eval_count" in data or "eval_count" in data:
                _last_usage = {
                    "prompt_tokens": data.get("prompt_eval_count", 0),
                    "completion_tokens": data.get("eval_count", 0),
                }
            return data.get("message", {}).get("content", "").strip()
        return f"[Ollama грешка {r.status_code}: {r.text[:200]}]"
    except Exception as e:
        return f"[Ollama не отговаря: {e}]"

# (FALLBACK_TRIGGER_CODES живееше тук — кои HTTP кодове пускат fallback към
# следващия модел. Стана мъртъв код при сливането с brain.py: веригата вече е
# на Brain, който има собствена, по-богата логика — exhaustion cooldown,
# multi-key ротация, деприоритизация по success rate.)

def _call_provider(provider_key, model_id, messages, tools=None):
    """Route to correct API function. Raises RuntimeError on failure.
    Връща (content, tool_calls) — tool_calls е None за gemini/ollama (никога
    не получават native tools, виж _SUPPORTS_TOOLS)."""
    p = PROVIDERS.get(provider_key, {})
    ptype = p.get("type", "openai")
    if ptype == "gemini":
        return call_gemini(messages, model_id), None
    elif ptype == "ollama" and provider_key == "ollama":
        return call_ollama(messages, model_id), None
    else:
        return call_openai_compatible(messages, provider_key, model_id, tools=tools)

def _sanitize_for_textmode(messages):
    """Превръща native tool_calls/tool-role съобщения в обикновен текст —
    нужно когато веригата пада от tools-способен модел към такъв БЕЗ tools
    параметър в СЪЩИЯ разговор (виж genesis_agent.brain._sanitize_for_textmode,
    същата логика, отделно копие тук защото терминалният чат си има собствена
    provider-верига, не минава през Brain)."""
    out = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            out.append({"role": "user", "content": f"[Резултат от tool]: {m.get('content', '')}"})
        elif role == "assistant" and m.get("tool_calls"):
            calls_desc = "; ".join(
                f"{tc.get('function', {}).get('name')}({tc.get('function', {}).get('arguments', '')})"
                for tc in m["tool_calls"]
            )
            content = (m.get("content") or "").strip()
            out.append({"role": "assistant", "content": (content + f"\n[извикани tool-ове: {calls_desc}]").strip()})
        else:
            out.append(m)
    return out


# Доставчици, които genesis_agent.brain НЕ познава (Gemini има собствен API формат,
# останалите са рядко ползвани/неактивни). Достъпни са САМО чрез ръчен избор от
# `/model` менюто — никога не са били в автоматичната верига (config.yaml).
# За тях пазим стария директен път като escape hatch.
_BRAIN_UNKNOWN_PROVIDERS = {"gemini", "github", "openai", "llmstudio", "ollama"}


def _ask_via_legacy(messages, tools, prov, model):
    """Стария директен път — само за ръчно избран доставчик извън brain.py."""
    global total_input_tokens, total_output_tokens, _last_usage
    _last_usage = None
    use_tools = tools if (tools and _SUPPORTS_TOOLS.get((prov, model))) else None
    msgs = list(messages) if use_tools else _sanitize_for_textmode(list(messages))
    try:
        response, tool_calls = _call_provider(prov, model, msgs, tools=use_tools)
    except Exception as e:
        return f"[Грешка: {e}]", None
    if _last_usage:
        total_input_tokens += _last_usage.get("prompt_tokens", 0)
        total_output_tokens += _last_usage.get("completion_tokens", 0)
        try:
            from genesis_agent.budget import record_usage
            record_usage(provider=prov, model=model,
                         prompt_tokens=_last_usage.get("prompt_tokens", 0),
                         completion_tokens=_last_usage.get("completion_tokens", 0))
        except Exception:
            pass
    else:
        for m in messages:
            if m.get("role") in ("user", "system"):
                total_input_tokens += estimate_tokens(m.get("content", "") or "")
        total_output_tokens += estimate_tokens(response)
    return response, tool_calls


def ask_genesis(messages, tools=None):
    """Връща (content, tool_calls).

    ОБЕДИНЕНО с genesis_agent.brain.Brain (design note, 2026-07-25). Дотогава терминалът
    имаше СОБСТВЕНА, по-стара provider-верига, паралелна на brain.py —
    дублирана fallback/tools логика, която трябваше да се поправя на две
    места. Сега реалната работа я върши Brain, а терминалът запазва своето:
    ръчния избор на модел (`/model` меню → `pin_model`), status bar брояча,
    Rich известията и escape hatch-а за доставчици, които Brain не познава.

    Какво идва наготово от Brain (терминалът НЕ ги имаше досега):
      • exhaustion tracking с cooldown при 429/402/503;
      • data-driven деприоритизация на "болни" доставчици (provider_stats);
      • RETRY_ROUNDS — втори пълен обход след кратка пауза;
      • локалният мозък като последна резерва;
      • РАБОТЕЩИЯ HuggingFace endpoint (виж по-долу).

    Този списък описваше и multi-key ротация (PROVIDER_KEY, _2, _3...) и
    ротиращ офсет на началото на веригата. И двете са премахнати: ротирането
    на няколко акаунта на един доставчик нарушава ToS-а на повечето от тях
    (виж SECURITY.md — един ключ на доставчик), а офсетът връщаше отговори от
    различен модел на всяко съобщение и не се качваше обратно нагоре.
    """
    global total_input_tokens, total_output_tokens

    # Ръчно избран доставчик, който Brain не познава → стария директен път.
    if current_provider in _BRAIN_UNKNOWN_PROVIDERS:
        return _ask_via_legacy(messages, tools, current_provider, current_model_id)

    from genesis_agent.brain import Brain
    # Кодинг режимът (/maxcoding) нарочно бие ръчния пин: ако избраният в
    # `/model` модел остане пръв, режимът не прави нищо, а изглежда включен.
    brain = Brain(min_size_b=_TERMINAL_MIN_SIZE_B,
                  quality="coding" if _CODING_MODE else None,
                  pin_model=None if _CODING_MODE else (current_provider, current_model_id))
    pinned = (current_provider, current_model_id)

    reply = brain.complete(list(messages), tools=tools)
    text = reply.raw_text or ""

    # Brain вече логва usage в genesis_agent.budget вътрешно — тук САМО обновяваме
    # брояча на status bar-а, за да не се дублира записът в !budget.
    usage = getattr(reply, "usage", None)
    if usage:
        total_input_tokens += usage.get("prompt_tokens", 0)
        total_output_tokens += usage.get("completion_tokens", 0)
    else:
        for m in messages:
            if m.get("role") in ("user", "system"):
                total_input_tokens += estimate_tokens(m.get("content", "") or "")
        total_output_tokens += estimate_tokens(text)

    # Ако Brain е паднал на друг доставчик ЗА ТОЗИ отговор (cooldown/грешка,
    # включително сгромолясване до локалния модел като последна резерва),
    # само показваме бадж — НЕ пипаме current_provider/current_model_id.
    # (Бъг до 2026-07-31: тук се презаписваше глобалният пин с каквото Brain
    # реално отговори. Една временна облачна грешка ставаше ПОСТОЯННА смяна —
    # сесията оставаше заключена на резервния/локалния модел до края,
    # вместо да се самолекува на следващото съобщение. Ако fallback-ът беше
    # към "ollama_local" (Brain-ов вътрешен provider ключ, липсващ от
    # терминалния PROVIDERS речник), следващото /status дори гърмеше с
    # KeyError.) Пинът се пипа само от изричен избор на оператора — /model,
    # /maxcoding, /local_model_max, /local_model_normal.
    if brain.current:
        used_provider = brain.current.get("provider", current_provider)
        used_model_id = brain.current.get("model", current_model_id)
        if (used_provider, used_model_id) != pinned:
            badge = model_badge(used_provider, used_model_id)
            console.print(f"\n[yellow]↪ Отговорено от → {used_provider} / {used_model_id} {badge} "
                          f"[dim](временно — пинът си остава {pinned[0]}/{pinned[1]})[/][/]")

    if text.startswith("Error:"):
        return f"[Грешка: {text[6:].strip()}]", None
    return text, getattr(reply, "tool_calls", None)

# ── Компресия на живата история ─────────────────────────────────────────────
# Досега messages беше deque(maxlen=30) — просто РЕЖЕШЕ най-старото при
# препълване, без обобщение. Дълъг разговор растеше линейно в token разход
# (всяко следващо съобщение плаща за цялата натрупана история) и губеше стар
# контекст рязко на 30-тия ред. Работи сега като context management-а тук:
# периодично, ПРЕВАНТИВНО (не при твърд cutoff), най-старите съобщения се
# заменят с едно кратко резюме — по-евтино на токени И реално помни повече
# (резюмето носи информация от целия разговор, не само последните N реда).
_COMPACT_THRESHOLD = 16  # съобщения (без system) преди компресия
_COMPACT_KEEP_RECENT = 10  # колко последни съобщения остават сурови


def _compact_messages(messages: "deque") -> "deque":
    """Ако историята е пораснала над прага, обобщава по-старата част с евтин
    модел и я заменя с едно system резюме. Безопасно — при грешка просто
    пази последното (fallback), никога не чупи разговора.

    Делегира на genesis_agent.brain.Brain.compact_chat_history (изнесено оттук
    2026-07-27, за да го ползва и genesis_agent/gui/genesis_gui.py — виж коментара там за
    защо GUI-то се нуждаеше от точно същото)."""
    from genesis_agent.brain import Brain
    return Brain.compact_chat_history(
        messages, threshold=_COMPACT_THRESHOLD, keep_recent=_COMPACT_KEEP_RECENT
    )


# ── Tools ─────────────────────────────────────────────────────────────────────
def parse_and_execute_tools(response_text):
    try:
        return genesis_skills.parse_and_execute_tools(response_text)
    except NameError:
        return ["[Грешка: genesis_skills не е зареден]"]

# ── Discord ────────────────────────────────────────────────────────────────────
def discord_send(text: str) -> bool:
    """Праща в Discord. Връща True при успех; логва грешките вместо да ги гълта тихо."""
    webhook = KEYS.get("GENESIS_DISCORD_WEBHOOK","")
    if not webhook:
        return False
    if len(text) > 1990: text = text[:1990] + "…"
    try:
        payload = json.dumps({"content": text}).encode("utf-8")
        req = urllib.request.Request(webhook, data=payload,
            headers={"Content-Type":"application/json","User-Agent":"Genesis/5.0"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status in (200, 204)
    except Exception as e:
        console.print(f"[dim red]⚠ Discord грешка: {str(e)[:80]}[/]")
        return False

# ── Epic GENESIS Banner ───────────────────────────────────────────────────────
def get_system_info() -> dict:
    """Collect system status info for the banner."""
    info: dict[str, Any] = {}
    # CPU
    try:
        with open("/proc/loadavg") as f:
            load = f.read().split()[:3]
        info["cpu_load"] = f"{load[0]} {load[1]} {load[2]}"
    except Exception:
        info["cpu_load"] = "N/A"
    # RAM
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        mem = {l.split()[0].rstrip(':'): int(l.split()[1]) for l in lines[:5] if len(l.split()) >= 2}
        total_mb = mem.get("MemTotal", 0) // 1024
        free_mb  = (mem.get("MemAvailable", 0)) // 1024
        used_mb  = total_mb - free_mb
        info["ram"] = f"{used_mb}MB / {total_mb}MB"
        info["ram_pct"] = int(used_mb / total_mb * 100) if total_mb else 0
    except Exception:
        info["ram"] = "N/A"
        info["ram_pct"] = 0
    # Ollama
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=2)
        if r.status_code == 200:
            models = r.json().get("models", [])
            count = len(models)
            info["ollama"] = f"✅ Работи ({count} модела)" if count > 0 else "⚠️  Работи (без модели)"
            info["ollama_ok"] = True
        else:
            info["ollama"] = "❌ Не работи"
            info["ollama_ok"] = False
    except Exception:
        info["ollama"] = "❌ Не работи"
        info["ollama_ok"] = False
    # GPU
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            out = subprocess.check_output([nvidia_smi, "--query-gpu=name,memory.used,memory.total,temperature.gpu",
                                           "--format=csv,noheader,nounits"], timeout=3, text=True).strip()
            parts = [p.strip() for p in out.split(",")]
            if len(parts) >= 4:
                info["gpu"] = f"{parts[0]}  {parts[1]}MB/{parts[2]}MB  {parts[3]}°C"
            else:
                info["gpu"] = out
        except Exception:
            info["gpu"] = "N/A"
    else:
        info["gpu"] = "Няма NVIDIA GPU"
    # Disk
    try:
        st = os.statvfs(str(Path.home()))
        total_gb = (st.f_blocks * st.f_frsize) // (1024**3)
        free_gb  = (st.f_bfree  * st.f_frsize) // (1024**3)
        info["disk"] = f"{free_gb}GB свободни / {total_gb}GB"
    except Exception:
        info["disk"] = "N/A"
    return info


GENESIS_ART = """[bold cyan]
 ██████╗ ███████╗███╗   ██╗███████╗███████╗██╗███████╗
██╔════╝ ██╔════╝████╗  ██║██╔════╝██╔════╝██║██╔════╝
██║  ███╗█████╗  ██╔██╗ ██║█████╗  ███████╗██║███████╗
██║   ██║██╔══╝  ██║╚██╗██║██╔══╝  ╚════██║██║╚════██║
╚██████╔╝███████╗██║ ╚████║███████╗███████║██║███████║
 ╚═════╝ ╚══════╝╚═╝  ╚═══╝╚══════╝╚══════╝╚═╝╚══════╝[/]"""

VERSION_LINE = "[bold magenta]    ⚡ TERMINAL AGENT  •  Autonomous AI System ⚡[/]"
DIVIDER      = "[dim cyan]    ══════════════════════════════════════════════════════[/]"


def print_minimal_banner():
    """Epic GENESIS startup banner with system status."""
    os.system("clear")
    console.print()
    console.print(GENESIS_ART)
    console.print(VERSION_LINE, justify="center")
    console.print(DIVIDER)
    console.print()

    # Collect system info
    with console.status("[dim cyan]Зареждам система...[/]", spinner="dots"):
        sysinfo = get_system_info()

    # Status table
    stable = Table(box=box.ROUNDED, border_style="cyan", show_header=False, padding=(0, 2))
    stable.add_column("Key",   style="bold cyan",  width=18)
    stable.add_column("Value", style="bold white")

    model_short = current_model_id.split("/")[-1] if "/" in current_model_id else current_model_id
    prov_name   = PROVIDERS[current_provider]["name"]
    api_keys_ok = sum(1 for pk, pd in PROVIDERS.items()
                      if pd["key_env"] and (KEYS.get(pd["key_env"])
                                            or (pk == "ollama_cloud" and _ollama_cloud_multi)))
    api_keys_tot = sum(1 for pd in PROVIDERS.values() if pd["key_env"])

    ram_pct = sysinfo.get("ram_pct", 0)
    ram_color = "green" if ram_pct < 70 else ("yellow" if ram_pct < 90 else "red")

    stable.add_row("🤖  Модел",         f"{model_short}")
    stable.add_row("🌐  Доставчик",     prov_name)
    stable.add_row("🔑  API Ключове",
                   f"[{'green' if api_keys_ok else 'red'}]{api_keys_ok}[/] / {api_keys_tot} активни")
    stable.add_row("🏠  Ollama",        sysinfo.get("ollama", "N/A"))
    stable.add_row("🖥️   GPU",           sysinfo.get("gpu", "N/A"))
    stable.add_row(f"💾  RAM ({ram_pct}%)", f"[{ram_color}]{sysinfo.get('ram', 'N/A')}[/]")
    stable.add_row("⚡  CPU Load",      sysinfo.get("cpu_load", "N/A"))
    stable.add_row("💿  Диск",          sysinfo.get("disk", "N/A"))
    stable.add_row("📅  Дата",          datetime.now().strftime("%d.%m.%Y  %H:%M:%S"))

    console.print(Panel(stable,
        title="[bold cyan]◈ СИСТЕМНА ИНФОРМАЦИЯ ◈[/]",
        border_style="cyan",
        padding=(0, 1)
    ))
    console.print()
    console.print("[dim]  Команди: [cyan]/model[/] [cyan]/models[/] [cyan]/clear[/] [cyan]/status[/] [cyan]/discord[/] [cyan]/backup[/] [cyan]/tasks[/] [cyan]/help[/]  │  Изход: [cyan]exit[/][/]")
    console.print(f"[dim]  Fallback: [green]{len(FALLBACK_CHAIN)} модела[/] верига | Активен: [cyan]{current_model_id.split('/')[-1][:30]}[/][/]")
    # Без нито един ключ нищо облачно няма да проработи, а "0 / 5 активни" в
    # таблицата отгоре е твърде тихо за фатално условие — първото съобщение
    # просто се проваляше с грешка от края на веригата.
    if not api_keys_ok:
        local_ok = "✅" in str(sysinfo.get("ollama", ""))
        console.print("[bold yellow]  ⚠ Няма конфигуриран нито един API ключ.[/] "
                      "Пусни [cyan]genesis setup[/] (или попълни ~/.genesis/.env).")
        if local_ok:
            console.print("[dim]    Локалният Ollama е наличен и ще поеме всичко — "
                          "той е резерва, не заместител на облака.[/]")
    console.print()

# ── Status Bar Display ────────────────────────────────────────────────────────
def show_status_bar():
    """Show live status bar at bottom."""
    console.print()
    console.print(Panel(
        build_status_bar(),
        border_style="cyan",
        padding=(0, 1),
        title="[dim]Status[/]",
        title_align="left"
    ))

def update_status_in_place():
    """Quick status update (for after responses)."""
    # Status shown at start, updates on next prompt

# ── Agent Selection Menu ──────────────────────────────────────────────────────
def show_agent_menu():
    global current_provider, current_model_id
    console.print()
    table = Table(title="[bold cyan]AI Доставчици[/]", box=box.ROUNDED, border_style="cyan", show_lines=True)
    table.add_column("#", style="bold white", width=4)
    table.add_column("Доставчик", style="bold")
    table.add_column("Статус")

    opts = list(PROVIDERS.keys())
    for i, pk in enumerate(opts, 1):
        p = PROVIDERS[pk]
        key_val = KEYS.get(p["key_env"] or "", "") or (p["key_env"] is None)
        status = "[green]✅[/]" if key_val else "[red]❌[/]"
        active = " [yellow]◀[/]" if pk == current_provider else ""
        table.add_row(str(i), p["name"] + active, status)
    table.add_row("0", "Назад", "")
    console.print(table)

    try:
        sel = int(console.input("\n[bold cyan]> [/]").strip())
    except ValueError:
        return
    if sel == 0: return
    if not 1 <= sel <= len(opts): return

    pk = opts[sel-1]
    p = PROVIDERS[pk]
    if p["key_env"] and not KEYS.get(p["key_env"]):
        console.print(f"[red]⚠ Няма ключ за {p['name']}![/]")
        return

    with console.status("[dim]Извличам модели...[/]", spinner="dots"):
        models = fetch_models(pk)

    # Special case: Ollama works but no models downloaded
    if pk == "ollama" and models == ["__no_models__"]:
        console.print()
        console.print(Panel(
            "[yellow]⚠  Ollama работи, но няма изтеглени модели!\n\n"
            "[cyan]Изтегли модел с команда:[/]\n"
            "  [bold green]ollama pull llama3.2[/]       ← препоръчан (2GB)\n"
            "  [bold green]ollama pull llama3.1:8b[/]    ← по-малък (4.7GB)\n"
            "  [bold green]ollama pull mistral[/]        ← Mistral 7B (4.1GB)\n"
            "  [bold green]ollama pull phi3[/]           ← Microsoft Phi3 (2.3GB)\n\n"
            "[dim]След изтеглянето влез отново в Ollama меню.[/]",
            title="[bold yellow]🏠 Ollama — Без модели[/]",
            border_style="yellow",
            padding=(1, 2)
        ))
        # Offer to pull now
        pull_choice = console.input("\n[bold yellow]Изтегли модел сега? (llama3.2/друг/не) > [/]").strip().lower()
        if pull_choice and pull_choice not in ["не", "no", "n", ""]:
            model_to_pull = "llama3.2" if pull_choice in ["да", "yes", "y"] else pull_choice
            console.print(f"[cyan]⬇  Изтеглям {model_to_pull}...[/]")
            subprocess.Popen(["bash", "-c", f"ollama pull {model_to_pull}"])
            console.print(f"[green]✓ Изтеглянето на {model_to_pull} стартира в фонов режим. Изчакай малко и пробвай отново.[/]")
        return

    if not models:
        console.print("[red]⚠ Неуспешно. Провери дали услугата е стартирана.[/]")
        return

    mtable = Table(title=f"Модели — {p['name']}", box=box.SIMPLE, border_style="green")
    mtable.add_column("#", width=5)
    mtable.add_column("Модел")
    for j, m in enumerate(models, 1):
        active = " [yellow]◀[/]" if pk == current_provider and m == current_model_id else ""
        badge  = model_badge(pk, m)
        mtable.add_row(str(j), f"{m}{active}", badge)
    mtable.add_row("0", "Назад", "")
    console.print(mtable)

    try:
        msel = int(console.input("[green]> [/]").strip())
    except ValueError:
        return
    if msel == 0: return
    if 1 <= msel <= len(models):
        current_provider = pk
        current_model_id = models[msel-1]
        console.print(f"[green]✓ {current_model_id}[/]")

# ── Main Loop ─────────────────────────────────────────────────────────────────
def main():
    global total_input_tokens, total_output_tokens
    total_input_tokens = 0
    total_output_tokens = 0

    print_minimal_banner()

    SYSTEM_PROMPT = config.get("system_prompt", "CRITICAL: You are Genesis, autonomous AI coding agent.")

    # Реалните пътища на машината — иначе моделът ги отгатва (жив тест: писа в
    # несъществуваща измислена sandbox директория). Виж agent_core.env_facts.
    try:
        from genesis_agent.agent_core import env_facts
        SYSTEM_PROMPT += "\n\n" + env_facts(str(WORKSPACE))
    except Exception:
        pass

    # ── Брифинг за състоянието на РАБОТАТА (design note, 2026-07-25) ──────────────
    # Досега тук се инжектираха последните 8 епизода — на практика лог от
    # `RUN_CMD echo ...` извиквания, който не носеше никаква информация за
    # какво реално се работи. Сега първо върви workspace паметта (отворени
    # нишки със следващи стъпки, решения, предпочитания) — това, което
    # позволява да продължим оттам, докъдето сме стигнали, без преразказ.
    briefing_text = ""
    try:
        from genesis_agent import workspace_memory as _wm
        briefing_text = _wm.briefing()
        if briefing_text:
            SYSTEM_PROMPT += "\n\n## СЪСТОЯНИЕ НА РАБОТАТА (от предишни сесии)\n" + briefing_text
    except Exception:
        pass

    # Епизодичната памет остава като допълнение — полезна е за "какво се обърка
    # последно", но вече НЕ е основният контекст.
    if _memory_context is not None:
        try:
            ctx = _memory_context(n=6)
            if ctx and ctx.strip() and "Няма записани" not in ctx:
                SYSTEM_PROMPT += "\n\n## Скорошна активност (второстепенно)\n" + ctx
        except Exception:
            pass

    # Проактивно отваряне: потребителят вижда веднага какво е отворено и кое е
    # следващото, вместо да се сеща сам или да пита.
    if briefing_text:
        console.print(Panel(Text(briefing_text[:1800]),
                            title="[bold cyan]📋 Оттук продължаваме[/]",
                            border_style="cyan", padding=(1, 2)))

    messages = deque([{"role": "system", "content": SYSTEM_PROMPT}], maxlen=30)

    while True:
        try:
            show_status_bar()

            user_input = console.input("[bold green]❯[/] ").strip()
            if not user_input: continue

            # ── Commands ──
            if user_input.lower() in ["exit", "quit", "изход"]:
                discord_send("🔴 Genesis изключен.")
                break

            if user_input.lower() == "/agent":
                show_agent_menu()
                continue

            if user_input.lower() == "/clear":
                messages = deque([{"role": "system", "content": SYSTEM_PROMPT}], maxlen=30)
                total_input_tokens = 0
                total_output_tokens = 0
                print_minimal_banner()
                continue

            if user_input.lower() == "/autoupgrade":
                # Пуска ковачницата на заден план: сама тегли цели от
                # goal_engine и произвежда verified умения.
                #
                # Дотук сочеше `real_evolution_marathon.py` — файл, който
                # живее само в личния предшественик на проекта и никога не е
                # бил доставян тук. Командата беше тих no-op за всеки: с
                # ptyxis (терминал, който повечето хора нямат) прозорецът
                # мигваше и изчезваше, а без него логът отиваше в `logs/`,
                # която дори не се създава.
                from genesis_agent.config import LOGS_DIR
                LOGS_DIR.mkdir(parents=True, exist_ok=True)
                logf = LOGS_DIR / "forge.log"
                console.print("[cyan]🔨 Стартирам ковачницата на заден план…[/]")
                with open(logf, "ab") as lf:
                    subprocess.Popen(
                        [sys.executable, "-m", "genesis_agent.parallel_forge"],
                        stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                        start_new_session=True)
                console.print(f"[green]✓ Работи. Лог: {logf}[/]")
                discord_send("🔨 **Genesis стартира ковачницата.**")
                continue

            if user_input.lower() == "/backup":
                # Целта се задава от потребителя — не гадаем устройство и не
                # монтираме дискове вместо него. Без GENESIS_BACKUP_DIR просто
                # обясняваме какво липсва, вместо да пишем някъде наслуки.
                dest = os.environ.get("GENESIS_BACKUP_DIR", "").strip()
                if not dest:
                    console.print(
                        "[yellow]Задай GENESIS_BACKUP_DIR (къде да пази архива), напр.:[/]\n"
                        "  export GENESIS_BACKUP_DIR=/mnt/backup/genesis")
                    continue
                Path(dest).mkdir(parents=True, exist_ok=True)
                console.print(f"[cyan]💾 Архивирам {WORKSPACE} → {dest} …[/]")
                r = subprocess.run(
                    ["rsync", "-a", "--delete",
                     "--exclude", "venv", "--exclude", "__pycache__",
                     "--exclude", ".git", "--exclude", ".env",
                     f"{WORKSPACE}/", f"{dest}/"],
                    capture_output=True, text=True, check=False)
                if r.returncode == 0:
                    console.print("[green]✅ Архивирането завърши.[/]")
                    discord_send(f"💾 **Архив готов** → `{dest}`")
                else:
                    console.print(f"[red]❌ rsync се провали:[/] {r.stderr.strip()[:200]}")
                continue


            if user_input.lower() == "/model":
                show_agent_menu()
                continue

            # ── /maxcoding — най-силните БЕЗПЛАТНИ модели за работа по код ──
            # Нужен е като отделен режим, защото обикновеният ред на веригата е
            # компромис между качество, скорост и квота: за чат това е правилно,
            # за редакция на чужд код — не. Тук изборът е изричен и се плаща в
            # скорост и квота, не в пари.
            if user_input.lower() in ("/maxcoding", "/макскод"):
                globals()["_CODING_MODE"] = not _CODING_MODE
                if _CODING_MODE:
                    from genesis_agent.brain import _load_coding_chain
                    chain = _load_coding_chain()
                    console.print("[bold green]🛠️  Кодинг режим ВКЛЮЧЕН[/] — "
                                  "най-силните безплатни модели за код:")
                    for i, c in enumerate(chain, 1):
                        console.print(f"   [cyan]{i}.[/] {c['provider']}/{c['model']}")
                    console.print("[dim]   По-бавно и харчи повече квота — затова не е по подразбиране.[/]")
                    if current_provider or current_model_id:
                        # Ръчният пин би държал избрания модел пръв и би обезсмислил
                        # режима — казваме го, вместо да го оставим да мълчи.
                        console.print("[dim]   Ръчно избраният модел (/model) се игнорира, докато режимът е включен.[/]")
                else:
                    console.print("[yellow]🛠️  Кодинг режим ИЗКЛЮЧЕН[/] — обичайната верига.")
                continue

            # ── /local_model_max, /local_model_normal — изричен офлайн режим ──
            # Форсира Brain да ползва САМО локален Ollama модел (GENESIS_LOCAL_ONLY),
            # без изобщо да пипа облака. Две фиксирани нива, каквото е реално
            # инсталирано на машината: 14B (най-мощен, бавен) и 7B (по-лек, бърз).
            # Повторно извикване на същата команда изключва режима обратно към
            # облак-пръв ред; извикване на другата команда, докато режимът вече
            # е включен, само сменя нивото.
            if user_input.lower() in ("/local_model_max", "/локален_макс"):
                from genesis_agent.brain import LOCAL_TIER_MAX, set_local_only
                globals()["_LOCAL_ONLY_MODEL"] = (
                    None if _LOCAL_ONLY_MODEL == LOCAL_TIER_MAX else LOCAL_TIER_MAX
                )
                set_local_only(_LOCAL_ONLY_MODEL)
                if _LOCAL_ONLY_MODEL:
                    console.print(f"[bold green]🏠 Локален режим ВКЛЮЧЕН (MAX)[/] — {LOCAL_TIER_MAX} "
                                  "(14B, най-мощният локален модел, бавен)")
                    console.print("[dim]   Само локално — облакът не се пипа, докато режимът е включен.[/]")
                else:
                    console.print("[yellow]🏠 Локален режим ИЗКЛЮЧЕН[/] — обратно към облачната верига.")
                continue

            if user_input.lower() in ("/local_model_normal", "/локален_нормал"):
                from genesis_agent.brain import LOCAL_TIER_NORMAL, set_local_only
                globals()["_LOCAL_ONLY_MODEL"] = (
                    None if _LOCAL_ONLY_MODEL == LOCAL_TIER_NORMAL else LOCAL_TIER_NORMAL
                )
                set_local_only(_LOCAL_ONLY_MODEL)
                if _LOCAL_ONLY_MODEL:
                    console.print(f"[bold green]🏠 Локален режим ВКЛЮЧЕН (NORMAL)[/] — {LOCAL_TIER_NORMAL} "
                                  "(7B, по-лек и по-бърз)")
                    console.print("[dim]   Само локално — облакът не се пипа, докато режимът е включен.[/]")
                else:
                    console.print("[yellow]🏠 Локален режим ИЗКЛЮЧЕН[/] — обратно към облачната верига.")
                continue

            if user_input.lower() == "/status":
                _ctx_used, ctx_remain, ctx_pct = get_context_stats()
                console.print(f"[cyan]Модел:[/] {current_model_id}"
                              + ("  [green](кодинг режим — веригата е друга)[/]" if _CODING_MODE else ""))
                if _LOCAL_ONLY_MODEL:
                    console.print(f"[cyan]Доставчик:[/] 🏠 Локален режим — {_LOCAL_ONLY_MODEL} (облакът е спрян)")
                else:
                    console.print(f"[cyan]Доставчик:[/] {PROVIDERS[current_provider]['name']}")
                console.print(f"[cyan]Време:[/] {get_elapsed_time()}")
                console.print(f"[cyan]Токени:[/] ~{total_input_tokens + total_output_tokens} ({ctx_pct}%)")
                console.print(f"[cyan]Остава:[/] ~{ctx_remain}")
                continue

            if user_input.lower() == "/help":
                help_table = Table(box=box.ROUNDED, border_style="cyan", show_header=False, padding=(0, 2))
                help_table.add_column("Команда", style="bold cyan", width=20)
                help_table.add_column("Описание", style="white")
                help_table.add_row("/model или /agent", "Смяна на AI модел/доставчик")
                help_table.add_row("/models", f"Покажи целия fallback chain ({len(FALLBACK_CHAIN)} модела)")
                help_table.add_row("/maxcoding", "Вкл./изкл. най-силните БЕЗПЛАТНИ модели за код")
                help_table.add_row("/local_model_max", "Вкл./изкл. офлайн режим — само qwen3:14b (мощен, бавен)")
                help_table.add_row("/local_model_normal", "Вкл./изкл. офлайн режим — само qwen2.5-coder:7b (лек, бърз)")
                help_table.add_row("/clear", "Нов разговор (изчиства историята)")
                help_table.add_row("/status", "Системна информация и статистика")
                help_table.add_row("/history", "Преглед и зареждане на стари сесии")
                help_table.add_row("/discord <текст>", "Изпрати съобщение в Discord")
                help_table.add_row("/autoupgrade", "Пуска ковачницата (нови умения) на заден план")
                help_table.add_row("/backup", "Архивиране към GENESIS_BACKUP_DIR")
                help_table.add_row("/tasks", "Състояние на работата — отворени нишки, решения")
                help_table.add_row("/done <id>", "Затвори нишка като готова (/drop <id> = изхвърли)")
                help_table.add_row("exit / quit", "Изход")
                console.print(Panel(help_table, title="[bold cyan]◈ GENESIS КОМАНДИ ◈[/]", border_style="cyan"))
                continue

            # ── Затваряне/изхвърляне на нишка (хигиена) ──
            if user_input.lower().startswith(("/done", "/drop", "/готово")):
                parts_cmd = user_input.split()
                if len(parts_cmd) < 2:
                    console.print("[yellow]Дай номер: `/done 3` (готово) или `/drop 3` (изхвърли)[/]")
                    continue
                try:
                    from genesis_agent import workspace_memory as _wm
                    drop = user_input.lower().startswith("/drop")
                    for ident in parts_cmd[1:]:
                        console.print(f"[dim]{_wm.close_thread(ident, drop=drop)}[/]")
                except Exception as e:
                    console.print(f"[red]⚠ {e}[/]")
                continue

            # ── Състояние на работата (workspace памет) ──
            if user_input.lower() in ("/tasks", "/задачи", "/state"):
                try:
                    from genesis_agent import workspace_memory as _wm
                    b = _wm.briefing(max_threads=20, max_decisions=10)
                    st = _wm.stats()
                    console.print(Panel(
                        Text(b if b else "Още нищо не е записано."),
                        title=f"[bold cyan]📋 Работа — {st['open']} отворени, "
                              f"{st['blocked']} блокирани, {st['done']} готови[/]",
                        border_style="cyan", padding=(1, 2)))
                except Exception as e:
                    console.print(f"[red]⚠ {e}[/]")
                continue

            # ── Discord command ──
            if user_input.lower().startswith("/discord"):
                parts = user_input.split(" ", 1)
                if len(parts) > 1 and parts[1].strip():
                    msg = parts[1].strip()
                    if discord_send(f"💬 **Genesis (ръчно):** {msg}"):
                        console.print("[green]✓ Изпратено в Discord![/]")
                    else:
                        console.print("[red]✗ Неуспешно изпращане (провери webhook-а).[/]")
                else:
                    webhook = KEYS.get("GENESIS_DISCORD_WEBHOOK", "")
                    status = "[green]✅ Настроен[/]" if webhook else "[red]❌ Не е настроен (добави в .env)[/]"
                    console.print(f"[cyan]Discord webhook: {status}[/]")
                    console.print("[dim]Използване: /discord <твоето съобщение>[/]")
                continue

            # ── Show full fallback chain ──
            if user_input.lower() == "/models":
                console.print()
                fb_table = Table(
                    title=f"[bold cyan]◈ FALLBACK CHAIN — {len(FALLBACK_CHAIN)} модела ◈[/]",
                    box=box.ROUNDED, border_style="cyan", show_lines=True
                )
                fb_table.add_column("#",       style="bold white",  width=4)
                fb_table.add_column("Доставчик", style="bold",       width=18)
                fb_table.add_column("Модел",    style="cyan")
                fb_table.add_column("Тип",      width=10)
                for idx, fb in enumerate(FALLBACK_CHAIN, 1):
                    prov_key = fb['provider']
                    mod = fb['model']
                    pname = PROVIDERS.get(prov_key, {}).get('name', prov_key)
                    badge = model_badge(prov_key, mod)
                    active = " [yellow bold]◀ ACTIVE[/]" if prov_key == current_provider and mod == current_model_id else ""
                    fb_table.add_row(str(idx), pname, f"{mod}{active}", badge)
                console.print(fb_table)
                console.print(f"[dim]Текущ: [cyan]{current_provider}[/] / [bold]{current_model_id}[/][/]")
                continue

            if user_input.lower() == "/history":
                history_files = sorted(glob.glob(str(HISTORY_DIR / "session_*.json")), reverse=True)
                if not history_files:
                    console.print("[yellow]Няма намерена история.[/]")
                    continue
                
                table = Table(title="[bold cyan]История на сесиите[/]", box=box.ROUNDED, border_style="cyan")
                table.add_column("#", style="bold white")
                table.add_column("Файл")
                table.add_column("Дата")
                for i, hf in enumerate(history_files[:10], 1):
                    dt = datetime.fromtimestamp(os.path.getmtime(hf)).strftime("%Y-%m-%d %H:%M:%S")
                    table.add_row(str(i), Path(hf).name, dt)
                table.add_row("0", "Назад", "")
                console.print(table)
                
                try:
                    hsel = int(console.input("\n[bold cyan]Избери сесия за зареждане > [/]").strip())
                    if hsel > 0 and hsel <= len(history_files):
                        with open(history_files[hsel-1], "r", encoding="utf-8") as f:
                            messages = json.load(f)
                        console.print(f"[green]✓ Сесията е заредена! ({len(messages)} съобщения)[/]")
                except (ValueError, FileNotFoundError):
                    console.print("[red]Невалиден избор.[/]")
                continue


            messages.append({"role": "user", "content": user_input})
            _remember("user", user_input)

            # Итеративен tool цикъл (design note, 2026-07-25): преди спираше след 1
            # рунд инструменти + 1 "финален" отговор, чиито евентуални НОВИ
            # тул-тагове никога не се изпълняваха — за реална многостъпкова
            # задача (свали → разархивирай tar.gz → инсталирай → symlink)
            # Genesis можеше да свърши само ПЪРВАТА стъпка, после само ОПИСВАШЕ
            # останалите вместо да ги изпълни (точно репортнатият проблем с
            # многостъпкова инсталация). Сега цикълът продължава рунд по рунд,
            # докато Genesis сам спре да вика тулове или се удари в тавана.
            _TOOL_ROUND_CAP = 8
            round_i = 0
            _malformed_tag_retries = 0
            with console.status("[dim]Genesis мисли...[/]", spinner="dots2"):
                response, tool_calls = ask_genesis(messages, tools=TERMINAL_TOOL_SCHEMAS)

            while True:
                console.print()
                if response.strip():
                    try:
                        content = Markdown(response)
                    except Exception:
                        content = Text(response)
                    console.print(Panel(content, border_style="cyan", padding=(1, 2)))
                assistant_msg = {"role": "assistant", "content": response}
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                messages.append(assistant_msg)
                _remember("assistant", response if response.strip() else
                          f"[повикани {len(tool_calls)} tool(-а)]")

                if tool_calls:
                    # Native tool-calling (design note, 2026-07-25): моделът поддържа
                    # структуриран function-calling — извикваме СЪЩИТЕ backend-и
                    # като regex-tag режима (genesis_skills.dispatch_tool_call),
                    # но без риск от грешно написан таг/синтаксис.
                    asked = ""
                    for tc in tool_calls:
                        fn = tc.get("function", {}) or {}
                        name = fn.get("name", "")
                        try:
                            args = json.loads(fn.get("arguments") or "{}")
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                        result = genesis_skills.dispatch_tool_call(name, args)
                        console.print(Panel(Text(result[:2000]), title=f"🔧 {name}", border_style="green"))
                        messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                          "name": name, "content": result})
                        if genesis_skills.ASK_USER_MARKER in result:
                            asked = result
                    if asked:
                        # ASK_USER (design note, 2026-07-27): агентът е попитал → цикълът
                        # СПИРА и чака реален отговор. Без това инструментът е
                        # безсмислен — моделът би задал въпроса и веднага сам би
                        # продължил да гадае, точно поведението, което спираме.
                        q = asked.replace(genesis_skills.ASK_USER_MARKER, "").strip()
                        console.print(Panel(Text(q), title="❓ Genesis пита",
                                            border_style="yellow", padding=(1, 2)))
                        break
                    round_i += 1
                    if round_i >= _TOOL_ROUND_CAP:
                        console.print(f"[yellow]⚠ Достигнат таван от {_TOOL_ROUND_CAP} инструмент-рунда "
                                       "за това съобщение — спирам тук, продължи с ново съобщение.[/]")
                        break
                    with console.status("[dim]Анализирам...[/]", spinner="aesthetic"):
                        response, tool_calls = ask_genesis(messages, tools=TERMINAL_TOOL_SCHEMAS)
                    continue

                # Стар text-tag режим — моделът не поддържа native tool-calling
                # (или просто избра да не вика нищо тази реплика).
                tool_results = parse_and_execute_tools(response)
                if not tool_results:
                    # Празно ≠ непременно "приключи" — може да е объркан tool tag
                    # (виж agent_core.run_tool_loop, същият фикс, design note
                    # 2026-08-11: "Fix local model narrating tool use without
                    # ever executing" беше закърпен само тук частично, не в корена).
                    if (_malformed_tag_retries < 2
                            and genesis_skills.looks_like_attempted_tool_tag(response)):
                        _malformed_tag_retries += 1
                        messages.append({
                            "role": "system",
                            "content": "[Система]: В последния отговор не намерих валиден tool "
                                       "таг, но той изглежда като опит за такъв. Ако си искал да "
                                       "викнеш инструмент — използвай точния синтаксис "
                                       "`[TAG: аргумент]` (или `[WRITE_FILE: път]...[END_WRITE]` "
                                       "за файлове). Ако вече си приключил — дай кратък финален "
                                       "отговор БЕЗ скоби във формàт на таг.",
                        })
                        with console.status("[dim]Анализирам...[/]", spinner="aesthetic"):
                            response, tool_calls = ask_genesis(messages, tools=TERMINAL_TOOL_SCHEMAS)
                        continue
                    break
                asked = next((r for r in tool_results
                              if genesis_skills.ASK_USER_MARKER in r), "")
                if asked:
                    q = asked.replace(genesis_skills.ASK_USER_MARKER, "").strip()
                    console.print(Panel(Text(q), title="❓ Genesis пита",
                                        border_style="yellow", padding=(1, 2)))
                    break
                round_i += 1
                if round_i >= _TOOL_ROUND_CAP:
                    console.print(f"[yellow]⚠ Достигнат таван от {_TOOL_ROUND_CAP} инструмент-рунда "
                                   "за това съобщение — спирам тук, продължи с ново съобщение.[/]")
                    break
                messages.append({"role": "system",
                                  "content": "[Резултат]:\n" + "\n\n".join(tool_results) +
                                  "\n\nАко тези резултати вече изпълняват заявката на потребителя "
                                  "напълно — дай КРАТКО финално обобщение БЕЗ никакви нови tool тагове. "
                                  "Викай нов tool САМО ако наистина има следваща реална стъпка. "
                                  "ВАЖНО: ако някоя команда е отказана от оператора (SANDBOX DECLINED), "
                                  "но ДРУГ резултат по-горе вече доказва, че целта е постигната (напр. "
                                  "командата вече работи правилно) — не настоявай за отказаната команда, "
                                  "просто отчети успех с наличните доказателства."})
                with console.status("[dim]Анализирам...[/]", spinner="aesthetic"):
                    response, tool_calls = ask_genesis(messages, tools=TERMINAL_TOOL_SCHEMAS)

            # Превантивна компресия на историята — преди cutoff-а на deque(maxlen=30),
            # не при него. Пести токени в дълги разговори, "помни" повече чрез резюме.
            before_len = len(messages)
            pre_compact = [m for m in messages if m.get("role") in ("user", "assistant")]
            messages = _compact_messages(messages)
            if len(messages) < before_len:
                console.print(f"[dim]🗜 История компресирана ({before_len} → {len(messages)} съобщения)[/]")
                # Компресията е моментът, в който старото съдържание се изхвърля —
                # записваме трайното от НЕкомпресираната история, докато я имаме.
                # Без това дълга сесия губи ранните решения (auto_capture на изход
                # вижда само последните ~10 съобщения), а при рязко прекъсване
                # (kill, затворен прозорец, спрян ток) се губи всичко от сесията.
                try:
                    from genesis_agent import workspace_memory as _wm
                    saved = _wm.auto_capture(pre_compact)
                    if any(saved.values()):
                        console.print(f"[dim]🧠 Запомнено преди компресията: "
                                       f"{saved['threads']}т/{saved['decisions']}р/{saved['preferences']}п[/]")
                except Exception:
                    pass

            # Save session history — convert deque to list for JSON serialization!
            session_file = HISTORY_DIR / f"session_{datetime.fromtimestamp(session_start_time).strftime('%Y%m%d_%H%M%S')}.json"
            with open(session_file, "w", encoding="utf-8") as f:
                json.dump(list(messages), f, ensure_ascii=False, indent=None, separators=(',', ':'))

        except (KeyboardInterrupt, EOFError):
            # EOF (Ctrl-D, or stdin closed when piped) used to fall through to
            # the generic handler below, which printed the error and looped —
            # forever, since the next read hit EOF again immediately.
            break
        except Exception as e:
            console.print(f"[red]⚠ {e}[/]")

    try:
        from genesis_agent import browser as _browser_mod
        _browser_mod.close()  # затваря headless Chromium, ако е бил отворен
    except Exception:
        pass

    # ── Автоматично запомняне на трайното от сесията ────────────────────────
    # Не разчитаме моделът да е викал REMEMBER/TASK_ADD по време на чата —
    # тестове на живо показаха, че често НЕ го прави (тръгва да решава
    # задачата и забравя да запише). Тук един евтин извличащ пас гарантира,
    # че решенията, предпочитанията и недовършеното оцеляват за следващия път.
    try:
        from genesis_agent import workspace_memory as _wm
        convo = [m for m in messages if m.get("role") in ("user", "assistant")]
        if len(convo) >= 2:
            with console.status("[dim]Запомням какво свършихме...[/]", spinner="dots"):
                saved = _wm.auto_capture(list(convo))
            if any(saved.values()):
                console.print(
                    f"[dim]🧠 Запомнено: {saved['threads']} нишки, "
                    f"{saved['decisions']} решения, {saved['preferences']} предпочитания[/]"
                )
    except Exception:
        pass

    console.print("\n[dim]Довиждане![/]")

if __name__ == "__main__":
    main()