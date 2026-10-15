"""
genesis_agent.brain — the single provider chain behind every frontend.

One `Brain` call walks the model chain from `config.yaml` top to bottom:
strongest cloud model first, falling through to the next on any failure, and
finally to a local Ollama model if one is installed. Providers you have no key
for are skipped silently, so the agent works with one key or with six.

Design notes worth knowing before you change anything here:
  • ONE key per provider BY DEFAULT. Rotating several free accounts to
    multiply a quota violates most providers' terms; breadth across providers
    is the supported way to get headroom. The documented exception is opt-in
    numbered keys: if the OPERATOR (not this codebase) chooses to add
    `<PROVIDER_KEY>_2`..`_10` to their own gitignored `~/.genesis/.env`, Brain
    rotates across whichever of those they set — opt-in, per-machine, never
    suggested or created by this project. Whether that is within a given
    provider's terms is the operator's call to make, not this code's; nothing
    here contacts a provider to check. Originally `ollama_cloud`-only,
    generalised to every provider on 2026-08-11 at the operator's request.
    Note that keys from the SAME account gain nothing — providers rate-limit
    per account/organisation, not per key.
  • Models that support native OpenAI tool-calling are preferred when `tools=`
    is passed; the rest fall back to the text-tag protocol in `config.yaml`.
  • A provider that returns 429/402/503/401/403 is put on a cooldown rather
    than retried immediately — see `_mark_exhausted`.

Public interface: `Brain()`, `system_prompt_base()`, `complete(messages)` →
object with `.raw_text` and `.code`.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import requests
import yaml

from genesis_agent.paths import (
    CONFIG_PATH,
    ENV_FILES,
    env_value,
)
from genesis_agent.tool_schemas import load_tool_arguments

# Extra ollama_cloud keys an operator may add THEMSELVES to their own
# gitignored ~/.genesis/.env — OLLAMA_API_KEY_2 through _10. Not suggested,
# not enabled by default, not touched unless these exact names are present.
# See the module docstring above.
_OLLAMA_CLOUD_KEY_ENVS = ["OLLAMA_API_KEY"] + [f"OLLAMA_API_KEY_{i}" for i in range(2, 11)]

log = logging.getLogger("genesis.brain")

CLOUD_TIMEOUT = 90
LOCAL_TIMEOUT = 240  # локалният 3B модел на слаб GPU е бавен — даваме му време
RETRY_ROUNDS = 2  # колко пъти да обходим цялата верига при пълен провал
_ROUND_PAUSE_S = 8  # пауза между два кръга по веригата
# Общ краен срок на едно обръщение = timeout × това. `timeout` на requests е
# между два пакета, не общ: OpenRouter държи връзката жива с празни редове,
# докато безплатният модел чака, и обръщението висеше без край (2026-09-25:
# проба спря 40 мин след 429-ци, 0 CPU, отворена HTTPS връзка; сутринта —
# 1 299 s). ×4 оставя място за дълъг легитимен отговор (16k токена).
_TOTAL_DEADLINE_FACTOR = 4


def _post_with_deadline(url: str, *, headers: dict, json: dict, timeout: int) -> Any:
    """requests.post с общ краен срок: след него → requests Timeout (→ следващ
    модел във веригата). Нишката се изоставя (daemon) — сокетът се затваря,
    когато сървърът приключи; процесът не я чака при изход."""
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["r"] = requests.post(url, headers=headers, json=json, timeout=timeout)
        except BaseException as e:  # предава се на викащия
            box["e"] = e

    worker = threading.Thread(target=_run, daemon=True, name="genesis-http")
    worker.start()
    deadline = timeout * _TOTAL_DEADLINE_FACTOR
    worker.join(deadline)
    if worker.is_alive():
        raise requests.exceptions.Timeout(f"няма отговор за {deadline} s (общ краен срок)")
    if "e" in box:
        raise box["e"]
    return box["r"]

# Таван на изходните токени за OpenAI-съвместимите доставчици. 4096 е стойността,
# с която проектът работи от началото — оставена е като подразбиране нарочно
# (всеки безплатен доставчик във веригата я приема; по-висока стойност някои
# отхвърлят с 400). Env override-ът съществува, защото 4096 е РЕАЛНО ограничение
# за по-дълъг скрипт: виж _is_truncated и коментара в _http за какво се случваше
# преди, когато таванът се удари.
MAX_OUTPUT_TOKENS = int(os.environ.get("GENESIS_MAX_TOKENS", "4096"))

# Таван по модел (design note, 2026-09-23): `max_tokens:` на запис в config.yaml.
# Поводът: gpt-oss мисли, и размисълът се брои в тавана — на живо отговорът
# му свърши точно на 4096. Общият таван остава 4096, защото по-високият го
# отхвърлят някои доставчици; вдига се само за модели, проверени наживо, че
# го приемат. GENESIS_MAX_TOKENS, ако е зададен, важи за всички (операторът
# го е поискал изрично).
_OUTPUT_CAPS: dict[str, int] | None = None


def _output_cap(model: str) -> int:
    global _OUTPUT_CAPS
    if os.environ.get("GENESIS_MAX_TOKENS"):
        return MAX_OUTPUT_TOKENS
    if _OUTPUT_CAPS is None:
        caps: dict[str, int] = {}
        try:
            models = (yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}).get("models", {}) or {}
            for group in ("fallback_models", "coding_models"):
                for entry in models.get(group, []) or []:
                    if entry.get("model") and entry.get("max_tokens"):
                        caps[entry["model"]] = int(entry["max_tokens"])
        except Exception as e:
            log.debug("_output_cap: config.yaml не се чете (%s), таванът остава общ", e)
        _OUTPUT_CAPS = caps
    return _OUTPUT_CAPS.get(model, MAX_OUTPUT_TOKENS)

# Anthropic native (само при quality="max"). max_tokens покрива И размисъла, И
# отговора. `xhigh` е препоръчаното ниво за кодинг и агентна работа; отделен
# запис в config.yaml може да го смени за конкретен модел.
ANTHROPIC_EFFORT = "xhigh"
# Заявката към Anthropic е поточна (2026-10-11): при `max` размисълът расте и
# една заявка минава 90-те секунди на CLOUD_TIMEOUT (най-дългият отговор при
# xhigh в bench egn-check — 6 919 токена) — тогава веригата тихо падаше на
# по-слабия модел. Потокът държи връзката жива и позволява по-голям таван;
# плаща се само генерираното.
ANTHROPIC_STREAM_MAX_TOKENS = 64000
# Операторът избира усилието без да пипа config.yaml: GENESIS_EFFORT=max.
_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Не всеки модел на Anthropic приема тези два параметъра. `output_config.effort`
# и adaptive thinking вървят от поколение 4.6 нагоре; на Haiku 4.5 и по-старите
# (Sonnet 4.5 и назад) `effort` връща 400, а thinking иска `budget_tokens`.
# Изпращахме и двата безусловно, тоест всеки, който смени модела в config.yaml
# на по-евтин — точно което прави човек, който пести — получаваше HTTP 400 без
# следа защо. Списъкът е allowlist по префикс: непознат модел получава заявка
# без тези параметри, което всеки модел приема.
_ADAPTIVE_EFFORT_MODELS = (
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5", "claude-sonnet-4-6",
    "claude-fable-5", "claude-mythos-5",
)


def _supports_adaptive_effort(model: str) -> bool:
    """Приема ли този модел adaptive thinking + output_config.effort."""
    name = (model or "").strip().lower()
    return any(name.startswith(prefix) for prefix in _ADAPTIVE_EFFORT_MODELS)

# OpenAI-съвместими доставчици (base_url + env ключ; None ключ = без auth, локален).
_PROVIDERS = {
    "ollama_local": ("http://localhost:11434/v1", None),   # СОБСТВЕН локален мозък
    "huggingface": ("https://router.huggingface.co/v1", "HF_TOKEN"),  # голями бързи, отделна квота
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "cohere":     ("https://api.cohere.ai/compatibility/v1", "COHERE_API_KEY"),
    "groq":       ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "nvidia":     ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
    # Cerebras (added 2026-08-11): genuinely free, no card — 1M tokens/day +
    # 14,400 req/day PER MODEL, no signup friction. 8K context on the free
    # tier though (much smaller than the other free providers here).
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
    # SambaNova Cloud (added 2026-08-11): free tier, no card required either.
    "sambanova": ("https://api.sambanova.ai/v1", "SAMBANOVA_API_KEY"),
    # Together AI (added 2026-08-11, design note): NOT truly free like the
    # entries above — a payment method is required to even create a working
    # key (a signup credit offsets early usage, but it is card-gated). Kept
    # out of config.yaml's default `fallback_models` free chain on purpose;
    # only useful if the operator has explicitly added a card-backed key and
    # wants to opt in via a manual fallback_models entry.
    "together": ("https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    # Ollama Cloud (различно от ollama_local!) — ~5M токена/седмица безплатно,
    # СЪЩИЯТ OpenAI-съвместим формат, но много по-големи модели (:cloud суфикс)
    # отколкото GTX 1650 4GB може да върти локално. Добавено 2026-07-25.
    "ollama_cloud": ("https://ollama.com/v1", "OLLAMA_API_KEY"),
    # ── Платени доставчици (design note, 2026-07-30) ──────────────────────────────
    # Никога не се пробват сами: влизат във веригата САМО при quality="max"
    # (`GENESIS_QUALITY=max`) И само ако ключът реално е конфигуриран. Без ключ
    # всичко работи точно както преди — безплатната верига, същият ред.
    #
    # Причината да съществуват изобщо: за поправка на чужд код таванът на
    # безплатния слой е реален. Тези модели струват пари на заявка, затова
    # изборът кога да се плати остава на оператора, не на кода.
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    # Anthropic НЕ минава през OpenAI-съвместимия път по-долу — има собствен
    # native клон (_call_anthropic), защото Messages API-то е различен формат
    # (system отделно от messages, tool_use/tool_result блокове, adaptive
    # thinking). base_url-ът тук е само маркер за _PROVIDERS проверките.
    "anthropic": ("native://anthropic", "ANTHROPIC_API_KEY"),
    # ── Google (добавено 2026-09-20) ──────────────────────────────────────────
    # И двата адреса са проверени срещу живите услуги, не преписани по памет:
    # заявка с невалиден ключ връща 400 „Please pass a valid API key" от
    # generativelanguage и 401 „Expected OAuth 2 access token" от Vertex —
    # тоест пътищата съществуват точно така.
    #
    # gemini = Gemini API (AI Studio): статичен ключ, има безплатен слой,
    # OpenAI-съвместим е, значи влиза като всеки друг доставчик — включително
    # в ротацията на ключове (GEMINI_API_KEY_2..10).
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    # vertex = същите модели през Google Cloud: без статичен ключ, с OAuth
    # токен и адрес, който зависи от проекта и локацията. Затова има свой клон
    # в `_call` и свой модул (genesis_agent.vertex_auth), вместо ред тук с
    # фиксиран base_url. key_env сочи проекта — „конфигуриран" значи „има проект".
    "vertex": ("dynamic://vertex", "GOOGLE_CLOUD_PROJECT"),
    # Claude през официалния `claude` CLI с абонамента на оператора (2026-10-12,
    # genesis_agent/claude_cli.py). Без ключ: входът е на `claude` (/login).
    # Не е в нито една верига от config.yaml — само ръчен избор (`/model` или
    # GENESIS_START_PROVIDER), и никога в облака (виж _call_claude_cli).
    "claude_cli": ("native://claude_cli", None),
}

# Кои доставчици се викат през native SDK вместо през OpenAI-съвместим HTTP.
_NATIVE_PROVIDERS = {"anthropic"}


def _print_skip(provider: str, model: str, error: str, seconds: float) -> None:
    """Един ред, когато модел отпадне и веригата продължи.

    Досега се печаташе само кой е отговорил („↪ модел: …“), не защо горният
    е отпаднал. 2026-09-25 (ЕГН проект): 24 от 42 обръщения отидоха на
    nvidia ultra (5–10× по-бавен), 399 s за малка задача, а в лога нямаше
    нито една причина за ollama. Грешките на ниво ключ (429 на ключ) си имат
    свой ред в _call и не минават оттук.
    """
    # Липсващ ключ е настройка, не отпадане (2026-10-12): с Claude абонамента
    # и без безплатни ключове всяка лека задача при изход печаташе 13 реда
    # „skip: no GROQ_API_KEY configured“, а истинските причини се губеха в тях.
    if error.startswith("skip: no ") and error.rstrip().endswith("configured"):
        return
    reason = " ".join(error.split())[:80]
    print(f"  [Brain] ✗ {provider}/{model} след {seconds:.1f}s: {reason} → следващ")


def _is_transient(error: str) -> bool:
    return any(error.startswith(f"HTTP_{c}") for c in _TRANSIENT_CODES)


def _hit_output_cap(error: str) -> bool:
    """Отговор, отрязан на тавана на изходните токени (HTTP_TRUNCATED) — лимит на
    модела за дълъг отговор, не болен доставчик. Същото понятие, по което
    complete() не праща втори кръг и пази отрязания tool_call (2026-10-11):
    празен отговор с finish_reason=length остава обикновен провал, както в базата."""
    return "HTTP_TRUNCATED" in error


# Заявка над лимита на модела за един път (безплатният Groq: 8 000 токена в
# минута → HTTP_413 „Request too large“). Bench all-2026-10-11-mega: 127 такива
# отказа на groq/gpt-oss-120b (86 s), по един на всеки рунд, щом историята
# порасне — и всеки се броеше за провал на доставчика, тоест бързият стартов
# модел слизаше надолу във веригата за всички. Помни се най-малкият отказан
# размер (знаци JSON) за модела: по-голяма или равна заявка не тръгва към него,
# по-малка (нова задача, свита история) — пак.
_TOO_LARGE: dict[str, int] = {}


def _too_large(error: str) -> bool:
    return error.startswith("HTTP_413")


def _request_size(messages: list[dict], tools: list[dict] | None) -> int:
    return len(json.dumps([messages, tools], ensure_ascii=False, default=str))


def _openai_usage(usage: Any) -> Any:
    """usage от OpenAI-съвместим отговор + `cached_read_tokens` за budget.py.

    Ollama Cloud кешира префикса сам и връща колко е прочетено от кеша в
    `prompt_tokens_details.cached_tokens` (измерено 2026-09-25: 4 064 от 4 089
    на второто обръщение със същия системен промпт). Досега полето не се
    четеше и `genesis budget` показваше кеш 0 при всички освен Anthropic.
    NVIDIA nemotron-3-super го връща също (2026-10-11: 6 864 от 8 343), ultra —
    понякога; безплатният Groq — не. DeepSeek брои в `prompt_cache_hit_tokens`.
    Без поле usage остава както е.
    """
    if not isinstance(usage, dict):
        return usage
    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    cached = cached or usage.get("prompt_cache_hit_tokens")
    if not cached:
        return usage
    return {**usage, "cached_read_tokens": int(cached)}


def provider_base_url(provider: str) -> str:
    """Адресът, на който се вика доставчикът.

    Онлайн услугата (cloud/gateway): задачата е в контейнер без изход навън и
    без истински ключове. `GENESIS_MODEL_GATEWAY` я праща към шлюза на сървъра
    (`<шлюз>/<доставчик>/chat/completions`), който слага ключа и брои токените.
    Само отдалечените OpenAI-съвместими — локалният, native и Vertex не минават.
    """
    base_url = _PROVIDERS[provider][0]
    gateway = os.environ.get("GENESIS_MODEL_GATEWAY", "").strip().rstrip("/")
    if gateway and base_url.startswith("https://") and provider not in _NATIVE_PROVIDERS:
        return f"{gateway}/{provider}"
    return base_url

# Локалният модел (собственият мозък на Genesis). Празно → изключен.
LOCAL_MODEL = os.environ.get("GENESIS_LOCAL_MODEL", "qwen2.5-coder:3b")

# Двете фиксирани нива за изричен офлайн режим (design note, 2026-07-31):
# "MAX" — най-мощният локален модел, инсталиран на машината, бавен; "NORMAL" —
# по-лек и по-бърз. Общи константи за всеки фронтенд (терминал, GUI), за да не
# се разминат имената на моделите на две места.
LOCAL_TIER_MAX = "qwen3:14b"
LOCAL_TIER_NORMAL = "qwen2.5-coder:7b"


def set_local_only(model_id: str | None) -> None:
    """Форсира ("model_id" зададен) или освобождава (None) изричен офлайн режим
    за следващите Brain() извиквания в текущия процес.

    LOCAL_MODEL е модулна константа, четена от Brain.__init__ през global lookup
    ПРИ ВСЯКО извикване (не се заключва на import) — затова смяната ѝ тук е
    достатъчна, без да се минава през env var (GENESIS_LOCAL_MODEL се чете само
    веднъж, на import). GENESIS_LOCAL_ONLY кара Brain.complete() да пропусне
    ЦЯЛАТА облачна верига и да ползва само local. Споделено между терминала
    (/local_model_max, /local_model_normal) и GUI-то (LocalModePicker), за да не
    се разминават двата фронтенда в поведение."""
    global LOCAL_MODEL
    if model_id:
        os.environ["GENESIS_LOCAL_ONLY"] = "1"
        LOCAL_MODEL = model_id
    else:
        os.environ.pop("GENESIS_LOCAL_ONLY", None)
        LOCAL_MODEL = os.environ.get("GENESIS_LOCAL_MODEL", "qwen2.5-coder:3b")


def _ollama_installed() -> list[str]:
    """Имената (с таг) на инсталираните в локалния Ollama модели; [] без Ollama."""
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=2)
        if r.status_code == 200:
            return [str(m.get("name", "")) for m in r.json().get("models", [])]
    except Exception:
        return []
    return []


def _same_ollama_model(model: str, name: str) -> bool:
    """Точно този таг; име без таг значи `:latest`, както го чете Ollama."""
    return name == model or (":" not in model and name == f"{model}:latest")


def _local_available(model: str) -> bool:
    """Проверява дали Ollama върви локално и има ТОЧНО този модел.

    Сравнението беше само по семейство (одит 2026-10-11): с инсталиран само
    qwen2.5-coder:7b `qwen2.5-coder:3b` излизаше наличен, последната резерва
    викаше липсващ модел и получаваше 404, а `/local_model_max` (qwen3:14b)
    се включваше при инсталиран само qwen3:4b."""
    if not model:
        return False
    return any(_same_ollama_model(model, n) for n in _ollama_installed())


def _installed_local_tier(names: list[str] | None = None) -> str | None:
    """Първото инсталирано ниво от model_router.LOCAL_TIERS (от бързо към мощно).

    `names` — вече прочетеният /api/tags, за да не се пита Ollama втори път."""
    try:
        from genesis_agent.model_router import LOCAL_TIERS
    except Exception:
        return None
    if names is None:
        names = _ollama_installed()
    return next((t for t in LOCAL_TIERS if any(_same_ollama_model(t, n) for n in names)), None)
# Грешки, при които минаваме към следващия модел (вкл. остарял/невалиден модел).
_FALLBACK_CODES = {400, 401, 402, 403, 404, 408, 429, 500, 502, 503}
# Кодове, при които моделът/квотата е ВРЕМЕННО изчерпан → cooldown, не го пробвай пак веднага.
_EXHAUST_CODES = {429, 402, 503}
# Кодове, които значат, че КЛЮЧЪТ е негоден (квота, плащане, auth) — тогава
# се спира целият ключ. 503 „претоварен" НЕ е тук (bench 2026-09-23): той е за
# един модел, а спирането на ключа прати 0/21 и на здравия nemotron-super под
# същия NVIDIA ключ. 503 остава cooldown само за модела (в complete()).
_KEY_DEAD_CODES = {402, 401, 403}
# 429 е за МОДЕЛ при Groq/Cerebras/OpenRouter (лимитите им са по модел), а за
# целия ключ — при дневна квота. Одит 2026-10-07: един 429 спираше ключа и
# здравите groq/B и groq/C не бяха пробвани. Ключът спира едва когато ВТОРИ
# различен модел под него върне 429 в рамките на cooldown-а.
_KEY_429_MODELS: dict[str, dict[str, float]] = {}
# При въртене на няколко ключа/проекта 429 праща към СЛЕДВАЩИЯ ключ веднага —
# там цената е само един опит с друг ключ, не загубен здрав модел.
_ROTATE_CODES = _KEY_DEAD_CODES | {429}


def _note_key_429(key_id: str, model: str) -> bool:
    """Записва 429 за модел под ключ; True, ако вече е за ≥2 модела (→ целият ключ)."""
    now = time.time()
    seen = {m: t for m, t in _KEY_429_MODELS.get(key_id, {}).items() if now - t < _EXHAUST_COOLDOWN}
    seen[model] = now
    _KEY_429_MODELS[key_id] = seen
    return len(seen) >= 2
_EXHAUST_COOLDOWN = 300  # секунди (5 мин) — колкото типичен OpenRouter free reset


def _rotation_cooldown(kid: str, model: str, error: str) -> bool:
    """Cooldown при въртене на ключове/проекти; True → пробвай следващия.

    Одит 2026-10-11: _call_rotating и _call_vertex спираха ЦЕЛИЯ ключ при
    първия 429 от който и да е модел. С GROQ_API_KEY + _2 + _3 един TPM 429 на
    модел A изгаряше и трите ключа, и здравият groq/B (лекият модел) отпадаше
    за 5 минути без нито една заявка. 429 вече спира само двойката
    (ключ, модел); ключът спира при 401/402/403 или когато ВТОРИ модел под
    него върне 429 — същото правило като в _call (_note_key_429)."""
    if any(f"HTTP_{c}" in error for c in _KEY_DEAD_CODES):
        _mark_exhausted(kid)
        return True
    if "HTTP_429" in error:
        if _note_key_429(kid, model):
            _mark_exhausted(kid, _cooldown_for(error))
        else:
            _mark_exhausted(f"{kid}::{model}", _cooldown_for(error))
        return True
    return False
# 410 Gone = доставчикът е спрял модела завинаги (на живо 2026-09-23: NVIDIA
# openai/gpt-oss-120b). Пет минути cooldown само го отлагат — пропуска се до
# края на процеса.
_GONE_COOLDOWN = 24 * 3600
# Сървърна грешка, която обикновено минава за секунди → същият модел още веднъж,
# след кратка пауза, преди следващия във веригата. Измерено в bench-а 26–30.09:
# 131 от 286 отговора на NVIDIA/OpenRouter (медиана 29 s / 17.6 s) дойдоха след
# HTTP 500 на ollama (медиана 3.5 s), а следващият опит на ollama мина в 104 от
# 139 случая. „празен отговор“ НЕ е тук: повторен е празен и 70 от 70 пъти.
_TRANSIENT_CODES = {500, 502, 504}
_TRANSIENT_PAUSE_S = 2.0

# Модул-ниво: изчерпани модели (survive-ва между мисии в един процес, напр. маратона).
_EXHAUSTED: dict[str, float] = {}


def _is_exhausted(key: str) -> bool:
    exp = _EXHAUSTED.get(key)
    if exp is None:
        return False
    if time.time() >= exp:
        _EXHAUSTED.pop(key, None)
        return False
    return True


def _mark_exhausted(key: str, seconds: float | None = None) -> None:
    _EXHAUSTED[key] = time.time() + (_EXHAUST_COOLDOWN if seconds is None else seconds)


# Подсказката на сървъра кога да опитаме пак (одит 2026-10-11): Groq връща
# 429 с `retry-after: 2` и „Please try again in 1.2s.“ за TPM лимит, а
# веригата вадеше бързия стартов модел (0.4–1.5 s) за фиксирани 300 s и чатът
# минаваше на ollama/nvidia (2–14 s на отговор) за 5 минути. Подсказката се
# пренася в текста на грешката като `HTTP_429[retry=N]`, за да стигне до
# complete() без нов канал; долната граница пази от удряне на модела в цикъл,
# горната — от дневна квота, обявена като часове.
_RETRY_HINT_MIN_S = 5
_TRY_AGAIN_RE = re.compile(r"try again in\s+(?:(\d+)\s*m(?!s))?\s*(?:([\d.]+)\s*(ms|s))?",
                           re.IGNORECASE)
_RETRY_TAG_RE = re.compile(r"HTTP_429\[retry=(\d+)\]")


def _retry_after_s(r: Any) -> float | None:
    """Секунди до следващ опит според 429 отговора, или None без подсказка."""
    headers = getattr(r, "headers", None) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw:
        try:
            return max(0.0, float(str(raw).strip()))
        except ValueError:
            pass  # HTTP-дата — рядко при тези доставчици; остава текстът
    m = _TRY_AGAIN_RE.search(getattr(r, "text", "") or "")
    if not m or not (m.group(1) or m.group(2)):
        return None
    try:
        seconds = float(m.group(1) or 0) * 60
        if m.group(2):
            seconds += float(m.group(2)) / (1000 if m.group(3).lower() == "ms" else 1)
    except ValueError:
        return None
    return seconds


def _cooldown_for(error: str) -> float:
    """Cooldown за 429 с подсказка `HTTP_429[retry=N]` → clamp(N, 5, 300)."""
    m = _RETRY_TAG_RE.search(error)
    if not m:
        return _EXHAUST_COOLDOWN
    return float(min(max(int(m.group(1)), _RETRY_HINT_MIN_S), _EXHAUST_COOLDOWN))


def _is_truncated(text: str) -> bool:
    """True ако `text` свършва посред код-ограда (нечетен брой ``` маркери).

    Точно този случай е опасен (design note, 2026-08-12): извличането на код
    навсякъде в проекта е `raw_text.split("```python")[1].split("```")[0]`.
    При НЕЗАТВОРЕНА ограда вторият split няма на какво да се раздели и връща
    целия остатък — тоест полуписан, синтактично счупен код се извлича така,
    сякаш е завършен. После пада във verifier-а като SyntaxError, изгаря цял
    рунд, и нищо никъде не показва, че истинската причина е ударен таван на
    токените, а не сгрешен код. `finish_reason` не се проверяваше НИКЪДЕ по
    целия LLM път до тази поправка.
    """
    return text.count("```") % 2 == 1


def _has_cut_tool_arguments(tool_calls: Any) -> bool:
    """True ако аргументите на някой tool_call не са цял JSON (отрязани).

    Със същия парсер като цикъла (ревю 2026-10-11): plain json.loads отхвърляше
    `"\\d{4}"` от регекс код, който load_tool_arguments поправя — цял call с
    finish_reason=length падаше като HTTP_TRUNCATED към следващия модел."""
    for tc in tool_calls or []:
        fn = tc.get("function") if isinstance(tc, dict) else None
        args = fn.get("arguments") if isinstance(fn, dict) else None
        if isinstance(args, str) and args.strip():
            try:
                load_tool_arguments(args)
            except ValueError:
                return True
    return False


def _load_keys() -> dict[str, str]:
    """
    Every key Genesis might need, with the precedence documented in
    `paths.get_secret`: a real environment variable beats ./.env, which beats
    ~/.genesis/.env.

    The order matters more than it looks. Exporting a key to override a stale
    one in ~/.genesis/.env is the obvious way to try a replacement, and it used
    to do nothing here — the file won, so Brain kept using the old key while
    every other module (which goes through get_secret) used the new one.
    """
    keys: dict[str, str] = {}
    for envf in ENV_FILES:  # project-local first, then ~/.genesis
        p = Path(envf)
        if not p.exists():
            continue
        # Същото четене като paths.read_env_files (одит 2026-10-11): с utf-8
        # .env от PowerShell 5.1 започваше с BOM и първият ключ ставаше
        # „\ufeffGROQ_API_KEY“ — терминалът го показваше активен, а Brain
        # мълчаливо прескачаше groq; `"sk-proj #1"` се режеше до `sk-proj` → 401.
        for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip().removeprefix("export ").strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                keys.setdefault(k.strip(), env_value(v))
    for k, v in os.environ.items():
        # Exported-but-empty is not an override — it falls through to the file,
        # exactly as get_secret does.
        if v:
            keys[k] = v
        else:
            keys.setdefault(k, v)
    return keys


def _load_chain() -> list[dict]:
    """Веригата от модели: default + fallback_models от config.yaml.
    Пази `size_b` (милиарди параметри) на всеки запис — Brain(min_size_b=...)
    филтрира по него за различни контексти (чат/терминал/умения)."""
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    models = cfg.get("models", {})
    fallback_list = models.get("fallback_models", [])
    meta_lookup = {
        (fb.get("provider"), fb.get("model")): fb for fb in fallback_list
    }

    chain: list[dict] = []
    seen: set[tuple[str | None, str | None]] = set()

    def _add(provider: str | None, model: str | None, size_b: float, supports_tools: bool) -> None:
        key = (provider, model)
        if provider and model and key not in seen:
            seen.add(key)
            chain.append({"provider": provider, "model": model, "size_b": size_b or 0,
                          "supports_tools": supports_tools})

    dp, dm = models.get("default_provider"), models.get("default_model_id")
    dmeta = meta_lookup.get((dp, dm), {})
    _add(dp, dm, dmeta.get("size_b", 0), bool(dmeta.get("supports_tools", False)))
    for fb in fallback_list:
        _add(fb.get("provider"), fb.get("model"), fb.get("size_b", 0),
             bool(fb.get("supports_tools", False)))

    # Автоматично откритите безплатни модели вървят НАКРАЯ, като резерва под
    # ръчно курираните: всеки запис в config.yaml е проверен наживо с реален
    # ключ, докато тези идват от каталог, който казва само какво твърди
    # доставчикът. `_add` вече дедуплицира, така че модел, присъстващ и на
    # двете места, запазва ръчната си позиция и ръчните си метаданни.
    try:
        from genesis_agent import free_models
        for fm in free_models.cached():
            _add(fm.get("provider"), fm.get("model"), fm.get("size_b", 0),
                 bool(fm.get("supports_tools", False)))
    except Exception as e:
        # Резервен слой — никога не спира старта.
        log.debug("_load_chain: без автоматично открити модели (%s)", e)

    # Мъртвите според последната `genesis models --check` (404/410) се
    # прескачат, докато следваща проверка не ги види живи — без да се пипа
    # config.yaml. Зает (429/503) не е мъртъв и остава.
    dead = _dead_models()

    # Само доставчици, които знаем как да викаме.
    return [c for c in chain
            if c["provider"] in _PROVIDERS and (c["provider"], c["model"]) not in dead]


def _dead_models() -> set:
    """Мъртвите според последната `genesis models --check` — за ВСИЧКИ вериги.
    Одит 2026-10-07: филтърът беше само в _load_chain, а coding/light веригите
    слагаха мъртвия модел първи и го викаха при всяко обръщение (404 не дава
    cooldown)."""
    try:
        from genesis_agent.model_check import dead_models
        return set(dead_models())
    except Exception as e:
        log.debug("model_check недостъпен (%s)", e)
        return set()


def _load_coding_chain() -> list[dict]:
    """
    Най-силната БЕЗПЛАТНА верига за код (`models.coding_models`) — това, което
    `/maxcoding` включва.

    Нарочно отделна от нормалната верига, а не просто пренареждане по `size_b`:
    „голям" и „добър в код" не са едно и също. Част от записите тук са
    специализирани кодинг-агентни модели, по-малки от 550B общ модел и
    по-подходящи точно за тази работа. За критериите и мерките зад подредбата
    виж коментара в config.yaml.

    Нула платени ключове, нула разход: същите безплатни доставчици, само друг
    ред и по-строг подбор.
    """
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    out: list[dict] = []
    dead = _dead_models()
    for entry in (cfg.get("models", {}) or {}).get("coding_models", []) or []:
        provider, model = entry.get("provider"), entry.get("model")
        if not provider or not model or provider not in _PROVIDERS or (provider, model) in dead:
            continue
        out.append({"provider": provider, "model": model,
                    "size_b": entry.get("size_b", 0),
                    "supports_tools": bool(entry.get("supports_tools", True)),
                    "coding": True})
    return out


def _load_light_chain() -> list[dict]:
    """`models.light_models` — бързите модели за второстепенната работа
    (извличане на памет, резюмета). Виж коментара в config.yaml за мерките."""
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    out: list[dict] = []
    dead = _dead_models()
    for entry in (cfg.get("models", {}) or {}).get("light_models", []) or []:
        provider, model = entry.get("provider"), entry.get("model")
        if provider in _PROVIDERS and model and (provider, model) not in dead:
            out.append({"provider": provider, "model": model,
                        "size_b": entry.get("size_b", 0),
                        "supports_tools": bool(entry.get("supports_tools", False))})
    return out


def _load_premium_chain() -> list[dict]:
    """
    Платените модели от config.yaml (`models.premium_models`).

    Отделен списък, а не флаг в основната верига, за да е невъзможно нещо да
    се озове там случайно: тези записи се четат САМО когато операторът изрично
    поиска quality="max", и се филтрират по наличен ключ. Липсва ли ключът,
    списъкът излиза празен и Brain се държи точно както преди — това е
    единственият начин "по-добро качество" да не се превърне в неочаквана
    сметка за някой, който е клонирал проекта.

    `premium: True` ги освобождава от min_size_b филтъра. Той рангира
    БЕЗПЛАТНИ модели по брой параметри (безплатният 8B наистина е по-слаб от
    безплатния 70B); за назован платен frontier модел параметрите нито са
    публични, нито са смисленият критерий.
    """
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    out: list[dict] = []
    for entry in (cfg.get("models", {}) or {}).get("premium_models", []) or []:
        provider, model = entry.get("provider"), entry.get("model")
        if not provider or not model or provider not in _PROVIDERS:
            continue
        out.append({"provider": provider, "model": model, "size_b": 0,
                    "supports_tools": bool(entry.get("supports_tools", True)),
                    "premium": True, "effort": entry.get("effort", "")})
    return out


def any_model_reachable() -> bool:
    """Има ли поне един модел, който Brain изобщо би извикал: ключ за доставчик
    от някоя верига, проект за Vertex или локален Ollama.

    Одит 2026-10-11: първо пускане без ключове — едно „здравей“ в чата минаваше
    два пъти през цялата верига (25 реда „[Brain] ✗ … skip: no KEY“) и
    завършваше с грешка; изходът правеше още 13. Чатът пита тук ПРЕДИ хода и
    казва какво да се направи, вместо да вика модел, който няма как да отговори.
    """
    keys = _load_keys()
    local_models = {LOCAL_MODEL}
    # Избран Claude абонамент (GENESIS_START_PROVIDER=claude_cli) не е в нито
    # една верига от config.yaml, а отговаря без ключ — стига CLI-то да е тук.
    if os.environ.get("GENESIS_START_PROVIDER", "").strip() == "claude_cli":
        from genesis_agent import claude_cli
        if claude_cli.available():
            return True
    for c in _load_chain() + _load_light_chain() + _load_coding_chain() + _load_premium_chain():
        key_env = _PROVIDERS.get(c["provider"], ("", None))[1]
        if key_env is None:          # ollama_local във веригата — решава пробата долу
            local_models.add(c["model"])
            continue
        if c["provider"] == "vertex":
            try:
                from genesis_agent import vertex_auth
                if vertex_auth.projects():
                    return True
            except Exception:
                return True          # не знаем — по-добре опит, отколкото отказ
            continue
        if str(keys.get(key_env) or "").strip():
            return True
    # Една проба, не по една на модел: затворен порт на Windows чака ~2 s.
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=2)
        names = [m.get("name", "") for m in r.json().get("models", [])] if r.status_code == 200 else []
    except Exception:
        return False
    return any(m and m.split(":")[0] in n for m in local_models for n in names)


class Brain:
    # Кеш за config.yaml tool_tag_prompt (виж _tool_tag_prompt).
    _TAG_PROMPT_CACHE: str | None = None

    def __init__(self, prefer_provider: str | None = None, use_local: bool = True,
                 min_size_b: float = 0, pin_model: tuple[str, str] | None = None,
                 quality: str | None = None, light: bool = False):
        self.keys = _load_keys()
        self.chain = _load_chain()
        self.timeout = CLOUD_TIMEOUT
        self._fail_count = 0
        self._last_usage: dict | None = None
        self._last_local_error: str | None = None
        self._cut_reply: tuple[str, list | None] | None = None  # виж _http, HTTP_TRUNCATED
        # min_size_b (design note, 2026-07-25): филтрира облачната верига по размер —
        # различни контексти искат различно качество/скорост:
        # терминален чат ≥32B, умения/самонадграждане ≥120B (виж config.yaml
        # size_b анотациите). Локалният мозък НЕ се филтрира — той си остава
        # последна резерва независимо от размера, по-добре слаб отговор,
        # отколкото никакъв при пълен облачен провал.
        # Курираните слоеве (premium/coding) се добавят ПОД тази точка и затова
        # не минават през филтъра — умишлено. size_b е сигнал за НЕкурирани
        # безплатни модели, където размерът е всичко, с което разполагаме; при
        # изрично избран модел той е грешният критерий (north-mini-code е 30B и
        # е кодинг-агентен — за редакция на код бие доста по-голям общ модел).
        if min_size_b:
            self.chain = [c for c in self.chain if c.get("size_b", 0) >= min_size_b]
        # MAX качество (design note, 2026-07-30): платените модели минават ПРЕД
        # цялата безплатна верига, която остава като резерва — изтекла карта или
        # свършена квота значи по-слаб отговор, не спрял агент. Прилага се
        # СЛЕД min_size_b нарочно: филтърът рангира безплатни модели по размер и
        # няма власт над изричен избор на оператора.
        # light (design note, 2026-09-23): второстепенна работа — извличане на
        # памет, резюмета — отива първо на бързите `light_models` (0.6-3s срещу
        # 8-45s на голям модел), с цялата верига отдолу като резерва. Курира се
        # под min_size_b по същата причина като coding: изричен избор, не размер.
        self.light = light
        if light:
            fast = _load_light_chain()
            already = {(c["provider"], c["model"]) for c in fast}
            self.chain = fast + [c for c in self.chain
                                 if (c["provider"], c["model"]) not in already]
            # Операторът е избрал Claude абонамента (2026-10-12): леката работа
            # (памет, резюмета, граф) има последна резерва в haiku. Без безплатни
            # ключове паметта иначе тихо не се записваше — цялата лека верига
            # беше „skip: no KEY“. Отзад: безплатните пак са първи.
            from genesis_agent import claude_cli
            if claude_cli.chosen() and claude_cli.available():
                self.chain.append({"provider": "claude_cli", "model": "haiku",
                                   "size_b": 0, "supports_tools": False})
        # Леката работа не наследява GENESIS_QUALITY (2026-10-11): с `max` от
        # средата резюмето между ходовете, паметта и графът на знанието тръгваха
        # към claude-opus-5 при xhigh — платен размисъл за второстепенна работа.
        # Изрично `quality=` пак важи.
        env_quality = "" if light else os.environ.get("GENESIS_QUALITY")
        self.quality = (quality or env_quality or "").strip().lower()
        self.premium: list[dict] = []
        self._premium_meta: dict[tuple[str, str], dict] = {}

        # Безплатният кодинг слой (`/maxcoding`). Влиза и при quality="max" —
        # така „максимално качество" остава смислена заявка и без платен ключ:
        # платените отпред (ако има), после най-силните безплатни за код, после
        # обикновената верига. Никой режим не оставя агента без резерва.
        if self.quality in ("coding", "max"):
            coding = _load_coding_chain()
            if coding:
                already = {(c["provider"], c["model"]) for c in coding}
                self.chain = coding + [c for c in self.chain
                                       if (c["provider"], c["model"]) not in already]
                if self.quality == "coding":
                    print(f"  [Brain] 🛠️  Кодинг режим: {coding[0]['model']} "
                          f"+ още {len(coding) - 1} безплатни")

        if self.quality == "max":
            # Всеки платен доставчик има env ключ по дефиниция (само локалният
            # е с None) — но проверката е изрична, за да не мине мълчаливо
            # доставчик без auth, ако някой добави нов запис небрежно.
            self.premium = [c for c in _load_premium_chain()
                            if (_PROVIDERS[c["provider"]][1]
                                and self._provider_key(str(_PROVIDERS[c["provider"]][1])))]
            self._premium_meta = {(c["provider"], c["model"]): c for c in self.premium}
            if self.premium:
                self.chain = self.premium + self.chain
                names = ", ".join(f"{c['provider']}/{c['model']}" for c in self.premium)
                print(f"  [Brain] 💎 MAX режим: {names}")
            else:
                print("  [Brain] 🛠️  MAX без платен ключ → най-силната БЕЗПЛАТНА "
                      "кодинг верига (ANTHROPIC_API_KEY / OPENAI_API_KEY / "
                      "DEEPSEEK_API_KEY биха я подсилили, но не са нужни).")
        # prefer_provider (за паралелна работа) — заковава worker-а към един
        # доставчик (отделна квота), реди неговите модели пръв и без локален.
        if prefer_provider:
            pinned = [c for c in self.chain if c["provider"] == prefer_provider]
            rest = [c for c in self.chain if c["provider"] != prefer_provider]
            self.chain = pinned + rest
            self.local = None
        else:
            # СОБСТВЕН локален мозък (ако е наличен) — ВИНАГИ последна резерва
            # (design note, 2026-07-25): облакът се пробва пръв изцяло, локалният
            # (какъвто и tier — 3b/7b/14b) само ако цялата облачна верига
            # откаже. use_local=False изрично го изключва напълно (за
            # self-modification — виж genesis_agent/self_modify.py).
            self.local = None
            # ЕДНА проба на /api/tags (ревю 2026-10-11): _local_available и
            # _installed_local_tier питаха поотделно, а ask_genesis строи силен
            # Brain всеки ход — при висящ Ollama 4 s на ход вместо 2 s.
            installed = _ollama_installed() if use_local else []
            if any(_same_ollama_model(LOCAL_MODEL, n) for n in installed):
                self.local = {"provider": "ollama_local", "model": LOCAL_MODEL}
            elif installed and os.environ.get("GENESIS_LOCAL_ONLY") != "1" \
                    and "GENESIS_LOCAL_MODEL" not in os.environ:
                # Подразбиращият се модел липсва → инсталирано ниво вместо 404
                # (одит 2026-10-11). Изрично избраният модел (офлайн режим или
                # GENESIS_LOCAL_MODEL) не се подменя: там честата грешка е
                # по-добра от тиха смяна. Проверката е за наличие, не за
                # истинност: GENESIS_LOCAL_MODEL="" изключва локалния модел и
                # не бива да го включва обратно с друго ниво (ревю 2026-10-11).
                tier = _installed_local_tier(installed)
                if tier:
                    self.local = {"provider": "ollama_local", "model": tier}

        # Последният модел НЕ се помни между сесиите (махнато 2026-09-23 по
        # искане на оператора): стартът е винаги default_model_id от
        # config.yaml. Помненето залепваше чата за бавен 550B модел, а тест с
        # фалшива верига веднъж го презаписа с несъществуващ модел.

        # pin_model (design note, 2026-07-25): ТОЧЕН (provider, model) отпред — за
        # ръчния избор от терминалния `/model` меню. Не РЕЖЕ веригата (за
        # разлика от min_size_b) — ако избраният модел откаже, нормалният
        # fallback продължава, точно както преди сливането. Ако моделът не е
        # в config.yaml веригата (напр. избран от живия `/models` списък на
        # доставчика), се добавя отпред като валиден запис.
        self._pinned: dict | None = None
        if pin_model:
            p_prov, p_model = pin_model
            if p_prov in _PROVIDERS:
                rest = [c for c in self.chain
                        if not (c["provider"] == p_prov and c["model"] == p_model)]
                existing = next((c for c in self.chain
                                 if c["provider"] == p_prov and c["model"] == p_model), None)
                # Anthropic, избран в `/model` (2026-10-12): записът от
                # premium_models носи effort-а от config.yaml и native tools —
                # същото, което --max праща за този модел. Иначе ръчният избор
                # минаваше на текстови тагове. Пинът е изричен избор на
                # оператора, затова не чака quality="max".
                if not existing and p_prov in _NATIVE_PROVIDERS:
                    existing = next((dict(c) for c in _load_premium_chain()
                                     if c["provider"] == p_prov and c["model"] == p_model),
                                    {"provider": p_prov, "model": p_model, "size_b": 0,
                                     "supports_tools": True})
                    self._premium_meta.setdefault((p_prov, p_model), existing)
                self._pinned = existing or {"provider": p_prov, "model": p_model,
                                            "size_b": 0, "supports_tools": False}
                self.chain = [self._pinned] + rest

        self.current = (self.chain[0] if self.chain else None) or self.local

    def _on_local_tier(self) -> bool:
        """Дали `self.current` реално сочи локалния модел точно сега."""
        if self.local is None or not isinstance(self.current, dict):
            return False
        return (self.current is self.local
                or (self.current.get("provider") == self.local.get("provider")
                    and self.current.get("model") == self.local.get("model")))

    def _set_local(self, model: str) -> None:
        """Сменя локалния tier. `self.current` се пренасочва САМО ако мисията
        реално върви на локалния модел в момента (виж `escalate`)."""
        was_local = self._on_local_tier() or self.current is None
        self.local = {"provider": "ollama_local", "model": model}
        if was_local:
            self.current = self.local

    def route_for_goal(self, goal: str) -> None:
        """Адаптивно избира локалния модел според сложността на задачата."""
        try:
            from genesis_agent.model_router import pick_model
            m = pick_model(goal)
            if m:
                self._set_local(m)
        except Exception as e:
            log.debug("route_for_goal: model_router недостъпен, местният модел остава по подразбиране: %s", e)

    def escalate(self) -> bool:
        """Качва ЛОКАЛНИЯ мозък на следващия по-голям НАЛИЧЕН модел. True ако е сменен.

        Само локалния tier — облачната верига се качва през отделния
        `escalate_to_coding_chain()`. Затова и `self.current` се пренасочва
        само когато мисията РЕАЛНО върви на локалния модел.

        Бъг, хванат на живо (2026-08-12, проследена мисия): `self.current` се
        пренасочваше безусловно, така че мисия на `ollama_cloud/gpt-oss:120b-
        cloud` при първия провален рунд печаташе "⬆️ Ескалация към по-голям
        модел: qwen2.5-coder:7b-instruct-q3_K_M" — 120B облачен модел, "качен"
        до локален 7B. Самата маршрутизация не се променяше (`complete()`
        винаги обхожда веригата отгоре и презаписва `current` при успех), но
        два реални консуматора четат `brain.current` МЕЖДУ два `complete()`
        разговора:
          • `autonomous_loop._note_quality_failure` записва провала на
            качеството срещу `current["provider"]` — тоест срещу
            "ollama_local" вместо срещу облачния доставчик, който наистина е
            написал лошия код (точно обратното на целта: деприоритизира
            здравия и оставя проблемния недокоснат);
          • критикът подава `avoid=writer_pair` от `current`, за да не си
            рецензира собствената работа — сгрешена двойка изключва невинен
            модел и оставя истинския автор да се самооцени.
        Плюс операторът чете лъжлив ред в лога за нещо, което не се е случило.
        """
        try:
            from genesis_agent.model_router import next_tier_model
            cur = self.local["model"] if self.local else None
            nxt = next_tier_model(cur)
            if nxt and nxt != cur:
                on_local = self._on_local_tier()
                self._set_local(nxt)
                if on_local:
                    print(f"  [Brain] ⬆️ Ескалация към по-голям модел: {nxt}")
                else:
                    # Не сме на локалния tier — само подготвяме по-силна резерва
                    # за евентуален fallback; нищо в текущия рунд не се сменя,
                    # затова и не се обявява като ескалация пред оператора.
                    log.debug("escalate: локалната резерва вдигната до %s "
                              "(мисията върви на %s — маршрутът не се пипа)",
                              nxt, self.current)
                return True
        except Exception as e:
            log.debug("escalate: model_router недостъпен, оставам на текущия модел: %s", e)
        return False

    def escalate_to_coding_chain(self) -> bool:
        """Качва към най-силната БЕЗПЛАТНА кодинг верига (`/maxcoding` еквивалент),
        автоматично — след повторни ПРОВАЛИ НА КАЧЕСТВОТО (verifier/критик отхвърлят
        резултата), не само HTTP/execution грешки. Досега това ставаше само ръчно
        през GENESIS_QUALITY=coding (design note, 2026-08-11: свободен модел, който
        връща HTTP 200 с грешен/боклук код, никога не биваше деприоритизиран или
        заменян — `escalate()` качва само ЛОКАЛНИЯ tier, не облачната верига).
        True ако веригата е реално сменена (idempotent — no-op ако вече сме на
        coding/max)."""
        if self.quality in ("coding", "max"):
            return False
        coding = _load_coding_chain()
        if not coding:
            return False
        already = {(c["provider"], c["model"]) for c in coding}
        self.chain = coding + [c for c in self.chain if (c["provider"], c["model"]) not in already]
        self.quality = "coding"
        self.current = self.chain[0] if self.chain else self.current
        print(f"  [Brain] ⬆️ Ескалация към кодинг веригата след повторни провали "
              f"на КАЧЕСТВОТО (не грешка): {coding[0]['model']}")
        return True

    def system_prompt_base(self) -> str:
        return (
            "ABSOLUTE DIRECTIVE: You are Genesis Agent — an autonomous agent running on your "
            "operator's own machine. You are NOT Llama, Nvidia, Mistral or ChatGPT; you use "
            "their networks only as an engine. Your job is to carry out missions as Python code.\n"
            "Always return WORKING code in a single ```python ... ``` block, with an "
            "assert-based self-test that prints 'OK' on success. No explanation, code only.\n"
            "NEVER write a trivial/tautological assert (assert True, assert 1==1) just to "
            "satisfy the self-test requirement — it must actually verify the goal was achieved, "
            "or the verifier will reject it.\n"
            "If the context below contains ready code from an existing skill that solves part "
            "of the goal, reuse or extend it directly instead of reinventing it."
        )

    def build_context(self, goal: str) -> str:
        """
        RAG: събира релевантен контекст ПРЕДИ генериране — подобни съществуващи
        умения + минали уроци — за да не преоткрива колелото. Безопасно (не хвърля).

        Композиция: за топ 2 VERIFIED съвпадения инжектираме пълния им код (не само
        име+описание), за да може Brain-ът реално да го преизползва/разшири в скрипта
        си, вместо само да знае, че "нещо подобно" съществува.

        Прагът по-долу (_kw_score>=2) съществува, защото goal_engine-ните шаблони
        споделят едно и също изречение между напълно несвързани цели ("...with
        type hints, docstring, and an assert-based self-test..."). Измерено
        2026-07-27: БЕЗ прага заявка за "повърхнина на тор" инжектираше 795
        токена код за exponential-backoff — нулева реална релевантност.
        _STOPWORDS (skill_loader.py) вече маха шаблонните думи от keyword
        overlap-а, но семантичният слой е засегнат по-дълбоко: cosine similarity
        между СЪЩАТА заявка и напълно несвързано умение скача от 0.51 на 0.61
        само от добавяне на шаблонната опашка към заявката — embeddings.db
        е обучен/индексиран върху описания, които в 90%+ от случаите носят
        същото изречение, затова семантичният праг в skill_loader.search_skills НЕ е
        надежден сигнал за истинска релевантност тук. Истинската поправка (re-
        indexиране без шаблона) е по-голяма задача за отделна сесия — засега
        `_semantic_hit` съзнателно НЕ се доверява за скъпото инжектиране на
        пълен код (остава полезен за евтиния "други подобни" списък по-долу).
        """
        parts: list[str] = []
        try:
            from genesis_agent.skill_loader import search_skills, skill_view
            hits = search_skills(goal, top_n=4)
            if hits:
                verified_hits = [h for h in hits if h.get("verification", {}).get("verified")]
                relevant_hits = [h for h in verified_hits if h.get("_kw_score", 0) >= 2]
                code_hits = relevant_hits[:2]
                code_names = {h["name"] for h in code_hits}

                code_blocks = []
                for h in code_hits:
                    try:
                        code = skill_view(h["name"])["code"]
                        lines_code = code.splitlines()[:40]
                        code_blocks.append(
                            f"# от умение: {h['name']}\n" + "\n".join(lines_code)
                        )
                    except Exception:
                        code_names.discard(h["name"])  # неуспешно зареждане → трети в списъка
                if code_blocks:
                    parts.append(
                        "Готов, проверен код от съществуващи умения (ПРЕИЗПОЛЗВАЙ/РАЗШИРИ "
                        "директно в скрипта си вместо да пишеш еквивалентна логика от нула, "
                        "ако решава част от целта):\n\n```python\n" + "\n\n".join(code_blocks) + "\n```"
                    )

                rest = [h for h in hits if h["name"] not in code_names]
                if rest:
                    lines = [f"- {h['name']}: {h.get('description', '')[:80]}" for h in rest]
                    parts.append("Други подобни умения (само за идея):\n" + "\n".join(lines))
        except Exception as e:
            log.debug("build_context: skill_loader недостъпен, без инжектирани умения: %s", e)
        try:
            from genesis_agent.memory import memory_search
            eps = memory_search(goal, top_k=3)
            lessons = []
            for ep in eps:
                for les in ep.get("lessons_learned", [])[:1]:
                    lessons.append(f"- {les[:120]}")
            if lessons:
                parts.append("Уроци от минали мисии:\n" + "\n".join(lessons))
        except Exception as e:
            log.debug("build_context: memory_search недостъпен, без инжектирани уроци: %s", e)
        return "\n\n".join(parts)

    def _trim_messages(self, messages: list[dict], max_chars: int = 6000) -> list[dict]:
        """Ограничава общия размер на историята — само за ЛОКАЛНИЯ модел
        (виж _call_local). Досега методът беше документиран no-op ("облачните
        модели имат голям контекст — не режем"), вярно за облака, но пропуска
        точно случая, за който е кръстен: локалният 3B/7B/14B fallback има
        МНОГО по-малък прозорец от облачната верига, а нищо на локалния път
        не проверяваше сериализирания размер преди изпращане (design note,
        2026-08-11). System съобщението и последното (най-скорошно)
        съобщение се пазят непокътнати; по-старите се режат до опашката им."""
        total = sum(len(str(m.get("content") or "")) for m in messages)
        if total <= max_chars or not messages:
            return messages

        out = list(messages)
        system_idx = next((i for i, m in enumerate(out) if m.get("role") == "system"), None)
        protected = {i for i in (system_idx, len(out) - 1) if i is not None}

        for i in range(len(out)):
            if i in protected:
                continue
            content = str(out[i].get("content") or "")
            if len(content) > 400:
                out[i] = {**out[i], "content": "...(съкратено)...\n" + content[-400:]}
            if sum(len(str(m.get("content") or "")) for m in out) <= max_chars:
                break
        return out

    @staticmethod
    def trim_round_history(messages: list[dict]) -> list[dict]:
        """
        Реже растящата история на многорундова мисия (autonomous_loop/orchestrator):
        пази system + оригиналната цел (messages[0:2]) + САМО последния разменен чифт
        (последен assistant отговор + последна user обратна връзка/грешка).

        Без това всеки следващ рунд пренаписва ЦЕЛИЯ натрупан разговор (всички
        предишни версии на кода + всички traceback-ове) — квадратичен разход на
        токени вместо линеен. Моделът реално се нуждае само от целта + последния
        код + последната грешка, за да поправи следващия рунд — не от пълната
        история на неуспешните опити.

        Tool-call-safe (design note, 2026-07-25, мисии с native USE_SKILL): фиксираният
        разрез на последните 2 съобщения може да падне точно ВЪТРЕ в native
        tool_calls група (assistant с tool_calls + следващи role='tool' резултати
        — може да са повече от едно) — ако разрежем там, пращаме 'tool'
        съобщение без родителския му assistant, което чупи OpenAI схемата.
        Разширяваме назад до последния assistant САМО в този случай; за
        обикновени (не-tool) рундове поведението е байт-по-байт същото както
        преди — извикващите БЕЗ tools (orchestrator/project_builder) никога не
        произвеждат role='tool' съобщения, значи условието долу никога не се
        задейства за тях.

        Проверката гледаше само ПОСЛЕДНОТО съобщение в разреза (bug fix,
        2026-09-20). Така пазеше сирака, когато tool резултатът е последен, но
        пропускаше точно толкова невалидния случай, в който след него стои още
        едно съобщение — а autonomous_loop добавя такова при всяка обратна
        връзка след tool рунд (заповедта "STOP calling tools", резултат от
        read-only таг, ruff бележка). Разрезът тогава е
        [system, цел, tool, user] — 'tool' без родителския си assistant, тоест
        HTTP 400 от всеки OpenAI-съвместим доставчик. Сега условието пита
        дали в разреза ИЗОБЩО има 'tool' без родител, независимо къде стои.
        """
        if len(messages) <= 4:
            return messages
        tail = messages[-2:]
        if (any(m.get("role") == "tool" for m in tail)
                and not tail[0].get("tool_calls")):
            for i in range(len(messages) - 1, 1, -1):
                if messages[i].get("role") == "assistant":
                    tail = messages[i:]
                    break
        return messages[:2] + tail

    @staticmethod
    def compact_chat_history(messages, threshold: int = 16, keep_recent: int = 10):
        """
        Превантивна компресия на дълъг РАЗГОВОРЕН чат (различно от
        trim_round_history — това е многотемен чат: system + произволен брой
        user/assistant/tool съобщения, не номерирани рундове на ЕДНА мисия).

        Над threshold съобщения (без system) — по-старата част (без последните
        keep_recent) се обобщава с 1 евтин извикване в едно system резюме,
        вместо да се пренася сурова на всяко следващо обаждане. Без това всяко
        следващо съобщение плаща за ЦЕЛИЯ растящ разговор.

        Извадено от genesis_terminal_agent.py (2026-07-25) в Brain, за да го
        ползва и genesis_agent/gui/genesis_gui.py (2026-07-27): GUI-то дефинираше същите
        COMPACT_THRESHOLD/COMPACT_KEEP_RECENT константи, но никога не викаше
        компресия — трупаше сурово до твърдия deque(maxlen=30) cutoff, което е
        едновременно по-скъпо (плаща пълната растяща история по-дълго) И губи
        по-старото без резюме (твърд truncate вместо мек compact) — по-лошо от
        терминала по двата критерия едновременно.

        Безопасно — при грешка/празно резюме пази последното (fallback без
        резюме), никога не чупи разговора. Приема list или deque; ако входът
        е deque, резултатът пази същия maxlen.
        """
        msgs = list(messages)
        if not msgs or msgs[0].get("role") != "system":
            return messages
        system_msg = msgs[0]
        rest = msgs[1:]
        if len(rest) <= threshold:
            return messages

        # Срезът не бива да пада между извикване и неговите tool резултати:
        # `[system, резюме, tool(c5), …]` е невалидна история (400 от всеки
        # доставчик, после веригата пада надолу на всяко следващо обръщение).
        # Затова срезът се мести назад до асистента, който ги е поискал.
        cut = len(rest) - keep_recent
        while cut > 0 and rest[cut].get("role") == "tool":
            cut -= 1
        if cut <= 0:
            return messages
        old_part = rest[:cut]
        recent_part = rest[cut:]
        # Предишното резюме влиза цяло (до 3000 знака) и ПЪРВО; само обикновените
        # реплики се режат до 500 (bug fix, 2026-10-11). Измерено: резюме от 586
        # знака, завършващо с „РЕШЕНИЕ: базата е PostgreSQL 16“, стигаше до
        # обобщаващия модел отрязано на 500 — решението изчезваше при следващото
        # компактиране (на ~3 хода), всеки път по малко от по-ранния разговор.
        def _is_summary(m: dict) -> bool:
            return (m.get("role") == "system"
                    and str(m.get("content", "")).startswith("## Резюме на по-ранния разговор:"))
        transcript = "\n".join(
            [f"system: {str(m.get('content', ''))[:3000]}" for m in old_part if _is_summary(m)]
            + [f"{m.get('role', '?')}: {str(m.get('content', ''))[:500]}" for m in old_part
               if not _is_summary(m)]
        )
        maxlen = getattr(messages, "maxlen", None)

        def _rebuild(parts: list) -> deque | list:
            return deque(parts, maxlen=maxlen) if maxlen else parts

        try:
            brain = Brain(light=True)  # резюме — второстепенна работа
            summary_reply = brain.complete([
                {"role": "system", "content": (
                    "Обобщи накратко (5-8 изречения) ключовите факти, решения и контекст "
                    "от този разговор. Само същественото за бъдещи реплики, не преразказ."
                )},
                {"role": "user", "content": transcript[:8000]},
            ])
            summary = (summary_reply.raw_text or "").strip()
            # "Error:" се проверява изрично, защото Brain.complete() НИКОГА не
            # хвърля — при изчерпана верига връща _error_result(), тоест обект
            # с raw_text = "Error: цялата верига е изчерпана | последна: ...".
            # Това е непразен низ, така че проверката само за празнота (както
            # беше досега) не се задействаше и грешката се озоваваше право в
            # summary_msg по-долу: целият по-ранен разговор биваше заменен с
            # "## Резюме на по-ранния разговор:\nError: ..." (bug fix,
            # 2026-08-12). Fallback-ът точно под този ред съществува заради
            # този случай, но беше недостижим. Загубата удряше именно когато
            # доставчиците са изчерпани — тоест когато вече върви зле.
            # `startswith("Error:")` е установената конвенция в проекта: седем
            # други места (autonomous_loop, orchestrator, project_builder,
            # терминала) вече проверяват точно това.
            if not summary or summary.startswith("Error:"):
                raise ValueError(f"неизползваемо резюме: {summary[:80] or 'празно'}")
        except Exception:
            # Fallback: просто пази последните, без резюме (губи старото честно,
            # не гърми разговора).
            return _rebuild([system_msg] + recent_part)

        summary_msg = {"role": "system", "content": "## Резюме на по-ранния разговор:\n" + summary}
        return _rebuild([system_msg, summary_msg] + recent_part)

    def _provider_key(self, key_env: str) -> str | None:
        """
        The one key for this provider, or None if it is not configured.

        Deliberately one key per provider. Rotating several free-tier accounts
        to multiply a quota is against most providers' terms of service, so it
        is not something this project ships by default. Resilience comes from
        breadth instead — many providers in the chain, each used within its
        own limits. `_ollama_cloud_keys` below is the one opt-in exception,
        gated on the operator's own env vars — see the module docstring.
        """
        val = self.keys.get(key_env)
        if val is None:
            return None
        val = str(val).strip()
        return val or None

    def _ollama_cloud_keys(self) -> list[str]:
        """
        Every OLLAMA_API_KEY[_2.._5] the operator has actually set, in order.

        Empty unless they added the extra numbered vars themselves — a bare
        OLLAMA_API_KEY still returns exactly the one-item list it always did,
        so nothing changes for anyone who never touches this."""
        return self._numbered_keys("OLLAMA_API_KEY")

    def _numbered_keys(self, base_env: str) -> list[str]:
        """
        Every `<BASE>` / `<BASE>_2` .. `<BASE>_10` the operator has actually
        set in their OWN gitignored ~/.genesis/.env, in order.

        Generalised from `_ollama_cloud_keys` (2026-08-11) at the operator's
        request, so the same opt-in mechanism covers any provider they hold
        several keys for — the numbered vars are read, never written, by this
        code. The caveat in the module docstring applies unchanged and is
        worth restating here, because this generalisation makes it easy to
        reach for: rotating keys belonging to SEPARATE free-tier accounts to
        multiply a quota is against most providers' terms of service. Whether
        that line is being crossed depends on where the keys came from, which
        only the operator knows; nothing here contacts a provider to check,
        and no numbered key is ever suggested or created by this project.

        Worth knowing before bothering: if several keys belong to the SAME
        account, rotation buys nothing at all — providers rate-limit per
        account/organisation, not per key (Groq documents this explicitly).
        In that case this is pure overhead.

        A single unnumbered key returns a one-item list, i.e. the normal
        single-key path, unchanged for anyone who never sets the extras."""
        names = [base_env] + [f"{base_env}_{i}" for i in range(2, 11)]
        out = []
        for env_name in names:
            v = self.keys.get(env_name)
            if v and str(v).strip():
                out.append(str(v).strip())
        return out

    def _http(self, base_url: str, key: str | None, model: str, messages: list[dict], timeout: int,
             tools: list[dict] | None = None, extra: dict[str, Any] | None = None
             ) -> tuple[str, list | None]:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload: dict[str, Any] = {"model": model, "messages": messages,
                                   "temperature": 0.7, "max_tokens": _output_cap(model)}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if extra:
            payload.update(extra)
        r = _post_with_deadline(f"{base_url}/chat/completions", headers=headers,
                                json=payload, timeout=timeout)
        if r.status_code == 429:
            hint = _retry_after_s(r)
            tag = f"[retry={math.ceil(hint)}]" if hint is not None else ""
            raise RuntimeError(f"HTTP_429{tag}: {r.text[:150]}")
        if r.status_code != 200:
            raise RuntimeError(f"HTTP_{r.status_code}: {r.text[:150]}")
        # Открито живо (design note, 2026-07-25, forge тест): някои доставчици връщат
        # 200 OK с тяло БЕЗ "choices" (напр. OpenRouter при upstream грешка,
        # обвита в 200 вместо в статус кода). Суров KeyError/IndexError тук НЕ
        # се хваща от `except RuntimeError` в complete()'s кръг — събаряше
        # ЦЯЛАТА мисия вместо да падне към следващия модел. Сега — RuntimeError,
        # за да мине през нормалната fallback логика.
        try:
            data = r.json()
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content")
            # Съдържание като списък от части (`[{"type": "text", ...}]`) —
            # някои OpenAI-съвместими шлюзове го връщат така; `.strip()` по-долу
            # гърмеше с AttributeError, който излизаше от complete() и губеше
            # целия ход (одит 2026-10-11). Друг тип не е текст → MALFORMED.
            if isinstance(content, list):
                content = "".join(str(part.get("text") or "") for part in content
                                  if isinstance(part, dict))
            elif content is not None and not isinstance(content, str):
                raise TypeError(f"content е {type(content).__name__}")
            tool_calls = message.get("tool_calls") or None
            finish_reason = choice.get("finish_reason") or ""
        # AttributeError: `"message": null` (OpenRouter при upstream грешка,
        # одит 2026-10-11) — без него следващият модел не се пробваше изобщо.
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            raise RuntimeError(f"HTTP_200_MALFORMED: {type(e).__name__}: {r.text[:150]}")
        if not content and not tool_calls:
            # Причината в лога (2026-09-30): ollama връщаше празно след 0.3 s в 13
            # от 210 bench разговора, до края им — а редът не казваше защо.
            usage = data.get("usage") or {}
            # Празният отговор също е платен (одит 2026-10-11: gpt-oss изяжда
            # тавана в размисъл — out=4096, content="" — а редът не стигаше до
            # budget_log, защото usage се записваше чак след тази проверка).
            self._last_usage = _openai_usage(data.get("usage"))
            self._usage_attempt = getattr(self, "_attempt_id", None)
            raise RuntimeError(f"празен отговор (finish_reason={finish_reason or '—'}, "
                               f"prompt={usage.get('prompt_tokens', '?')}, "
                               f"out={usage.get('completion_tokens', '?')}, "
                               f"reasoning={len(message.get('reasoning') or '')} знака)")
        # Ударен таван на изходните токени (design note, 2026-08-12): досега
        # `finish_reason` не се четеше НИКЪДЕ и отрязан отговор се връщаше като
        # нормален. Ако при това код-оградата е останала незатворена, счупен
        # полукод се извличаше като готов резултат — виж _is_truncated. Вдигаме
        # RuntimeError, за да мине през СЪЩАТА fallback логика като всяка друга
        # грешка (следващ модел във веригата), вместо да върнем нещо, което
        # изглежда като валиден отговор. Разпознаваемо име, за да е ясно в лога
        # каква е реалната причина, вместо мистериозен SyntaxError два слоя
        # по-надолу. НЕ съвпада с _EXHAUST_CODES (429/402/503) нарочно — таванът
        # не е изчерпана квота и не бива да вкарва модела в cooldown.
        # Същото за аргументите на tool_call (одит 2026-10-11): WRITE_FILE,
        # отрязан на 4096, стигаше до цикъла като „невалидни аргументи“ и
        # моделът пренаписваше същия голям файл в същия таван рунд след рунд.
        cut_args = finish_reason == "length" and _has_cut_tool_arguments(tool_calls)
        if cut_args or (finish_reason == "length" and _is_truncated(content or "")):
            self._last_usage = _openai_usage(data.get("usage"))
            self._usage_attempt = getattr(self, "_attempt_id", None)
            # Отрязаният call се пази: ако го отреже ВСЕКИ модел, complete() го
            # връща на цикъла вместо „Error“ (ревю 2026-10-11, виж там).
            self._cut_reply = ((content or "").strip(), tool_calls) if cut_args else None
            raise RuntimeError(
                f"HTTP_TRUNCATED: отговорът е отрязан на тавана от {_output_cap(model)} "
                f"токена, посред код-ограда или tool_call (finish_reason=length). Вдигни го с "
                f"`max_tokens:` за модела в config.yaml, ако доставчикът го позволява."
            )
        # usage липсва при локален Ollama /v1 понякога — None е ОК, budget.py го обработва.
        self._last_usage = _openai_usage(data.get("usage"))
        return (content or "").strip(), tool_calls

    # ── Anthropic native (Messages API) ──────────────────────────────────────────
    # Не минава през _http: Messages API-то не е OpenAI-съвместимо (system извън
    # messages, tool_use/tool_result блокове, adaptive thinking, refusal като
    # успешен 200). Преводът е тук, за да остане ВСИЧКО останало в проекта
    # непроменено — извикващият код продължава да получава (text, tool_calls) в
    # точно същия OpenAI формат, който вече обработва.

    @staticmethod
    def _to_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
        """(system, messages) в Anthropic формат."""
        system_parts: list[str] = []
        out: list[dict] = []
        leading = True
        for m in messages:
            role = m.get("role")
            content = m.get("content") or ""
            if role == "system" and leading:
                if content:
                    system_parts.append(content)
                continue
            leading = False
            if role == "system":
                # System съобщение СЛЕД разговора (резултат в текстов режим,
                # подкана на claim_check/code_check) е ход към модела, не част от
                # системния промпт: иначе разговорът свършваше с assistant ход —
                # prefill, който claude-opus-5/sonnet-5 отказват с 400, а кешът
                # на system блока се пренаписваше на всеки рунд (одит 2026-10-07).
                if content:
                    out.append({"role": "user", "content": f"[system] {content}"})
            elif role == "tool":
                out.append({"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or "",
                    "content": content or "(празен резултат)",
                }]})
            elif role == "assistant":
                blocks: list[dict] = []
                if content.strip():
                    blocks.append({"type": "text", "text": content})
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function", {}) or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except (ValueError, TypeError):
                        args = {}
                    blocks.append({"type": "tool_use", "id": tc.get("id") or "",
                                   "name": fn.get("name", ""), "input": args})
                # Празен assistant ход се изпуска: Anthropic отхвърля съобщение
                # без съдържание, а такова носи и нулева информация.
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": "user", "content": content or "(празно)"})
        # Разговорът трябва да започва с user ход, и tool_result без своя
        # tool_use (изпаднал при рязане на историята) дава 400 на всеки ход.
        # Двете се повтарят, докато нищо не се променя: махането на водещ
        # assistant може да осироти резултатите му (преглед 2026-10-07).
        while True:
            while out and out[0]["role"] != "user":
                out.pop(0)
            asked = {b["id"] for m in out if m["role"] == "assistant"
                     for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"}
            cleaned: list[dict] = []
            for m in out:
                if m["role"] == "user" and isinstance(m["content"], list):
                    kept = [b for b in m["content"]
                            if not (b.get("type") == "tool_result" and b.get("tool_use_id") not in asked)]
                    if not kept:
                        continue
                    m = {**m, "content": kept}
                cleaned.append(m)
            if len(cleaned) == len(out) and (not cleaned or cleaned[0]["role"] == "user"):
                return "\n\n".join(system_parts), cleaned
            out = cleaned

    @staticmethod
    def _to_anthropic_tools(tools: list[dict]) -> list[dict]:
        out = []
        for t in tools:
            fn = t.get("function", {}) or {}
            out.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            })
        return out

    @staticmethod
    def _anthropic_final(api: Any, params: dict[str, Any], **extra: Any) -> Any:
        """Поточна заявка, върнат е само крайният отговор (виж
        ANTHROPIC_STREAM_MAX_TOKENS) — за веригата същото като create()."""
        with api.stream(**extra, **params) as stream:
            return stream.get_final_message()

    def _call_anthropic(self, key: str, model: str, messages: list[dict],
                        tools: list[dict] | None, effort: str) -> tuple[str, list | None]:
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("skip: липсва пакетът `anthropic` (pip install anthropic)")

        system, msgs = self._to_anthropic_messages(messages)
        if not msgs:
            raise RuntimeError("няма съобщения за изпращане")
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": ANTHROPIC_STREAM_MAX_TOKENS,
            "messages": msgs,
        }
        if _supports_adaptive_effort(model):
            # Adaptive thinking: моделът сам решава колко да мисли по задачата.
            # Точно това искаме тук — MAX режимът съществува заради трудните
            # случаи, а лесните не бива да плащат за размисъл, който не им трябва.
            params["thinking"] = {"type": "adaptive"}
            chosen = os.environ.get("GENESIS_EFFORT", "").strip().lower()
            params["output_config"] = {
                "effort": chosen if chosen in _EFFORTS else (effort or ANTHROPIC_EFFORT)}
        if system:
            # Prompt caching (design note, 2026-09-20). Редът на рендиране е
            # tools → system → messages, затова една точка на последния
            # системен блок покрива И схемите на инструментите: при тази
            # конфигурация това е ~10 100 символа схеми + ~8 500 системен
            # промпт, които се изпращат наново при ВСЯКО обръщение, а при
            # 25 рунда инструменти се плащат 25 пъти.
            #
            # Нарочно НЯМА точка в `messages`: budget.budget_history свива
            # по-старите резултати с напредването на рундовете, тоест байтовете
            # в средата на историята се променят между заявките. Кешът е
            # съвпадение по префикс — точка там щеше да се разминава почти
            # винаги и само да харчи запис в кеша.
            # 1 час, не 5 минути (2026-10-11): между две съобщения на оператора
            # минават повече от 5 минути, а записът за час струва 2× веднъж,
            # не 1.25× при всяко връщане. По-дългият TTL трябва да е пред
            # по-краткия — този е първият.
            params["system"] = [{"type": "text", "text": system,
                                 "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
        # Опашката на разговора — като Claude Code (2026-10-11): автоматичната
        # точка стои на последния блок и се мести напред с всеки рунд, а
        # complete() праща на native доставчика историята само допълвана
        # (budget.cache_history) — следващият рунд чете от кеша всичко досегашно.
        params["cache_control"] = {"type": "ephemeral"}
        if tools:
            params["tools"] = self._to_anthropic_tools(tools)

        client = anthropic.Anthropic(api_key=key, timeout=float(self.timeout))
        try:
            # Класификаторите за безопасност могат да откажат заявка (връща се
            # 200 с stop_reason="refusal"). `fallbacks="default"` оставя сървъра
            # да я довърши на друг модел вместо да ни върне празен отговор.
            resp = self._anthropic_final(client.beta.messages, params,
                                         betas=["server-side-fallback-2026-07-01"],
                                         fallbacks="default")
        except anthropic.BadRequestError as e:
            # Акаунт без достъп до тази beta (или SDK, който не я познава) —
            # същата заявка без нея. Нашата собствена верига поема отказа.
            if "fallback" not in str(e).lower() and "beta" not in str(e).lower():
                raise RuntimeError(f"HTTP_400: {e}") from e
            resp = self._anthropic_final(client.messages, params)
        except anthropic.AuthenticationError as e:
            raise RuntimeError(f"HTTP_401: {e}") from e
        except anthropic.PermissionDeniedError as e:
            raise RuntimeError(f"HTTP_403: {e}") from e
        except anthropic.RateLimitError as e:
            raise RuntimeError(f"HTTP_429: {e}") from e
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"HTTP_{e.status_code}: {e}") from e
        except anthropic.APIConnectionError as e:
            raise RuntimeError(f"мрежа: {e}") from e

        usage = getattr(resp, "usage", None)
        # Прочетените от кеша токени се отчитат ОТДЕЛНО, за да е проверимо, че
        # кеширането работи: ако `cached_read_tokens` стои на нула при
        # повтарящи се заявки, нещо мълчаливо разваля префикса (променлив
        # системен промпт, различен набор инструменти) — и това се вижда в
        # `genesis budget`, вместо да се приема на доверие. Записва се ПРЕДИ
        # проверката за отказ: отказаният отговор също е платен.
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        # input_tokens на Anthropic е БЕЗ кеша, prompt_tokens на OpenAI — с него.
        # Тук се привежда към OpenAI смисъла, иначе отчетът казваше „кеш 7500%
        # от prompt“ (одит 2026-10-07).
        self._last_usage = {
            "prompt_tokens": (getattr(usage, "input_tokens", 0) or 0) + cache_read + cache_write,
            "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
            "cached_read_tokens": cache_read,
            "cached_write_tokens": cache_write,
        } if usage else None
        # Отказан/отрязан отговор по-долу пак е платен — complete() го записва.
        self._usage_attempt = getattr(self, "_attempt_id", None) if usage else None

        if getattr(resp, "stop_reason", "") == "refusal":
            # Не е техническа грешка — моделът е отказал темата. Като RuntimeError,
            # за да продължи веригата към следващия модел вместо да върне празно.
            raise RuntimeError(f"HTTP_REFUSAL: моделът отказа заявката "
                               f"({getattr(resp, 'stop_details', None)})")

        text_parts, tool_calls = [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({"id": block.id, "type": "function",
                                   "function": {"name": block.name,
                                                "arguments": json.dumps(block.input)}})

        text = "".join(text_parts).strip()
        if not text and not tool_calls:
            raise RuntimeError("празен отговор")
        # Същото като при OpenAI-съвместимия път (виж _http): досега тук се
        # проверяваше само "refusal", а изчерпан max_tokens минаваше за
        # нормален отговор — с незатворена код-ограда това значи счупен
        # полукод, приет за завършен.
        if getattr(resp, "stop_reason", "") == "max_tokens" and _is_truncated(text):
            raise RuntimeError(
                f"HTTP_TRUNCATED: отговорът е отрязан на тавана от "
                f"{ANTHROPIC_STREAM_MAX_TOKENS} токена, посред код-ограда "
                f"(stop_reason=max_tokens)."
            )
        return text, (tool_calls or None)

    def _cooldown_left(self, provider: str, model: str) -> float:
        """Секунди, след които (provider, model) пак може да тръгне; 0 — веднага.

        Огледало на проверките в _call/_call_rotating/_call_vertex: свободни
        трябва да са моделът и поне един негов ключ (с двойката ключ::модел).
        Ревю 2026-10-11: паузата между кръговете гледаше най-ранния край в
        ЦЕЛИЯ _EXHAUSTED — чужд модел, лекият модел или чужд ключ#n даваха
        8 s пауза и втори кръг без нито една заявка."""
        now = time.time()

        def left(k: str) -> float:
            return max(0.0, _EXHAUSTED.get(k, now) - now)

        key_env = _PROVIDERS.get(provider, ("", None))[1]
        if provider == "vertex":
            try:
                from genesis_agent import vertex_auth
                kids = [f"key::VERTEX#{i}" for i in range(1, len(vertex_auth.projects()) + 1)]
            except Exception:
                kids = []
        elif provider == "claude_cli":
            kids = ["key::claude_cli"]   # лимитът на абонамента (_call_claude_cli)
        elif not key_env:
            kids = []
        elif provider not in _NATIVE_PROVIDERS and len(self._numbered_keys(key_env)) > 1:
            kids = [f"key::{key_env}#{i}" for i in range(1, len(self._numbered_keys(key_env)) + 1)]
        else:
            kids = [f"key::{key_env}"]
        key_wait = min((max(left(k), left(f"{k}::{model}")) for k in kids), default=0.0)
        return max(left(f"{provider}::{model}"), key_wait)

    def _call(self, provider: str, model: str, messages: list[dict],
             tools: list[dict] | None = None,
             extra: dict[str, Any] | None = None) -> tuple[str, list | None]:
        key_env = _PROVIDERS[provider][1]
        base_url = provider_base_url(provider)
        if provider == "claude_cli":
            return self._call_claude_cli(model, messages)
        if provider == "vertex":
            # Адресът и токенът се смятат при извикване (проект + локация +
            # OAuth), затова Vertex не минава през общия път с фиксиран
            # base_url и статичен ключ.
            return self._call_vertex(model, messages, tools, extra)
        if not key_env:  # локален — без ключ
            # reasoning_effort="none" (design note, 2026-07-31, живо измерено):
            # Qwen3 мисли по подразбиране дори за тривиални задачи — >3 минути
            # за палиндром функция, срещу ~16-30с изключено. `think:false`
            # НЕ работи през OpenAI-съвместимия /v1/chat/completions endpoint
            # (само native /api/chat го чете) — правилното поле е
            # reasoning_effort. Езикът (BG срещу EN prompt) добавя само ~30%,
            # не десетократния ефект на мисленето — превод не е нужен.
            # extra (design note, 2026-08-11): опционален override/добавка —
            # Brain.generate_local_candidates ползва го за temperature, за да
            # вземе разнообразни кандидати от локалния tier вместо еднакви.
            local_extra = {"reasoning_effort": "none"}
            if extra:
                local_extra.update(extra)
            return self._http(base_url, None, model, messages, LOCAL_TIMEOUT, tools=tools,
                              extra=local_extra)

        # Opt-in multi-key rotation (see module docstring + _numbered_keys):
        # only takes this branch if the OPERATOR set `<KEY>_2`.. themselves in
        # their own gitignored .env. A single key falls straight through to
        # the normal one-key path below, unchanged. Generalised beyond
        # ollama_cloud on 2026-08-11 at the operator's request — same
        # mechanism, same caveats, now any provider.
        if provider not in _NATIVE_PROVIDERS:
            rotating = self._numbered_keys(key_env)
            if len(rotating) > 1:
                return self._call_rotating(base_url, key_env, model, messages, tools, rotating)

        key = self._provider_key(key_env)
        if not key:
            raise RuntimeError(f"skip: no {key_env} configured")

        kid = f"key::{key_env}"
        if _is_exhausted(kid):
            raise RuntimeError(f"HTTP_429: {key_env} is rate-limited, cooling down")

        try:
            if provider in _NATIVE_PROVIDERS:
                meta = self._premium_meta.get((provider, model), {})
                return self._call_anthropic(key, model, messages, tools,
                                            str(meta.get("effort", "")))
            return self._http(base_url, key, model, messages, self.timeout, tools=tools)
        except RuntimeError as e:
            last = str(e)
            # 429/402 (quota gone) or 401/403 (bad or forbidden key): this
            # provider is unusable for a while. Mark it and let the chain move
            # on to the next provider rather than hammering a dead endpoint.
            if any(f"HTTP_{c}" in last for c in _KEY_DEAD_CODES) or (
                    "HTTP_429" in last and _note_key_429(kid, model)):
                _mark_exhausted(kid, _cooldown_for(last))
                print(f"  [Brain] 🔑 {key_env} unavailable ({last[:60]}) → next provider")
            raise

    def _call_rotating(self, base_url: str, key_env: str, model: str, messages: list[dict],
                        tools: list[dict] | None, keys: list[str]) -> tuple[str, list | None]:
        """
        Opt-in multi-key path — only reached when `keys` has more than one
        entry, i.e. the operator set `<KEY>_2`.. themselves in their own
        gitignored .env. Tries each key in the order they set them, skipping
        ones already on cooldown, and marks only the SPECIFIC key that fails
        (not the whole provider) so a healthy key #3 still gets used after #1
        and #2 hit their cap.

        Was `_call_ollama_cloud_rotating`; generalised to any provider on
        2026-08-11 at the operator's request. The terms-of-service caveat in
        the module docstring and `_numbered_keys` applies to every provider
        this now covers, not just Ollama — it is the operator's call, and
        this code neither checks nor encourages it.
        """
        last_err: Exception | None = None
        tried_any = False
        for idx, key in enumerate(keys, start=1):
            kid = f"key::{key_env}#{idx}"
            if _is_exhausted(kid) or _is_exhausted(f"{kid}::{model}"):
                continue
            tried_any = True
            try:
                return self._http(base_url, key, model, messages, self.timeout, tools=tools)
            except RuntimeError as e:
                last = str(e)
                last_err = e
                if _rotation_cooldown(kid, model, last):
                    print(f"  [Brain] 🔑 {key_env}#{idx} unavailable ({last[:60]}) → next key")
                    continue
                raise
        if not tried_any:
            raise RuntimeError(f"HTTP_429: all configured {key_env}* are cooling down")
        raise last_err or RuntimeError(f"HTTP_502: {key_env} key rotation exhausted")

    def _call_claude_cli(self, model: str, messages: list[dict]) -> tuple[str, list | None]:
        """Claude през `claude -p` с абонамента на оператора — текстов модел:
        инструментите са таговете на Genesis, не native tool_calls.

        В облака (GENESIS_MODEL_GATEWAY) се отказва: абонаментът е за личната
        употреба на оператора, не за задачи на други хора (правилата на
        Anthropic, проверено 2026-10-12)."""
        if os.environ.get("GENESIS_MODEL_GATEWAY", "").strip():
            raise RuntimeError("skip: claude абонаментът не се ползва в онлайн услугата")
        from genesis_agent import claude_cli
        if any(m.get("role") == "tool" or m.get("tool_calls") for m in messages):
            messages = self._sanitize_for_textmode(messages)
        kid = f"key::{claude_cli.PROVIDER}"
        if _is_exhausted(kid):
            raise RuntimeError("HTTP_429: claude абонаментът е на пауза (лимит), cooling down")
        try:
            text, usage = claude_cli.complete(messages, model)
        except RuntimeError as e:
            last = str(e)
            # Лимитът на абонамента е за целия акаунт, не за модел: друг модел
            # през същия вход пак ще удари стената. 401 — входът липсва.
            if "HTTP_429" in last or any(f"HTTP_{c}" in last for c in _KEY_DEAD_CODES):
                _mark_exhausted(kid, _cooldown_for(last))
                print(f"  [Brain] 🔑 claude абонамент недостъпен ({last[:80]}) → следващ")
            raise
        self._last_usage = usage
        self._usage_attempt = getattr(self, "_attempt_id", None) if usage else None
        return text, None

    def _call_vertex(self, model: str, messages: list[dict],
                      tools: list[dict] | None,
                      extra: dict[str, Any] | None = None) -> tuple[str, list | None]:
        """Google Vertex AI през OpenAI-съвместимия му endpoint.

        Ротацията тук е по ПРОЕКТ, не по ключ: всеки проект в Google Cloud има
        своя квота и своя сметка, тоест това са собствени ресурси на оператора,
        а не няколко безплатни акаунта при един доставчик (разликата е описана
        в `_numbered_keys`). Проект, който върне 429/403, влиза в същия
        cooldown като всеки друг ключ и веригата продължава.

        Липсващ проект, липсващ `google-auth` или липсващи credentials дават
        `skip:`, което веригата третира като „този доставчик го няма" — без
        грешка и без прекъсване на мисията.
        """
        from genesis_agent import vertex_auth

        project_list = vertex_auth.projects()
        if not project_list:
            raise RuntimeError("skip: no GOOGLE_CLOUD_PROJECT configured")
        if not vertex_auth.auth_available():
            raise RuntimeError("skip: google-auth not installed (pip install 'genesis-agent[google]')")

        last_err: Exception | None = None
        tried_any = False
        no_token = False
        for idx, project in enumerate(project_list, start=1):
            kid = f"key::VERTEX#{idx}"
            if _is_exhausted(kid) or _is_exhausted(f"{kid}::{model}"):
                continue
            access_token = vertex_auth.token(project)
            if not access_token:
                no_token = True
                continue
            tried_any = True
            try:
                return self._http(vertex_auth.endpoint(project), access_token, model,
                                  messages, self.timeout, tools=tools, extra=extra)
            except RuntimeError as e:
                last = str(e)
                last_err = e
                if _rotation_cooldown(kid, model, last):
                    print(f"  [Brain] 🔑 vertex/{project} unavailable ({last[:60]}) → next")
                    continue
                raise
        if not tried_any:
            # „cooling down“ само когато наистина всеки проект е в cooldown
            # (ревю 2026-10-11): при изтекли ADC веригата броеше Vertex за
            # охлаждащ се с 0 s оставащи — 8 s пауза, празен втори кръг и
            # „най-рано след 0 s“ вместо истинските 290 s на другите модели.
            if no_token:
                raise RuntimeError("skip: no usable Vertex credentials (ADC not configured or expired)")
            raise RuntimeError("skip: every Vertex project is cooling down")
        raise last_err or RuntimeError("HTTP_502: vertex project rotation exhausted")

    def _call_local(self, messages: list[dict], attempts: int = 1) -> tuple[str, str] | None:
        """Пробва локалния мозък (текущия tier — 3b/7b/14b, каквото е в self.local).
        Връща (raw_text, code) при успех, None при провал. Локалният никога не
        получава tools (проверено наживо — не връща структуриран tool_calls,
        а суров JSON текст в content), винаги text-tag режим."""
        last_error = None
        loc = self.local
        if not loc:
            return None
        trimmed = self._trim_messages(messages)
        for _try in range(attempts):
            try:
                raw_text, _tc = self._call(loc["provider"], loc["model"], trimmed)
                self._fail_count = 0
                self.current = self.local
                code = ""
                if "```python" in raw_text:
                    code = raw_text.split("```python")[1].split("```")[0].strip()
                elif raw_text.lstrip().startswith(("def ", "import ", "from ", "class ")):
                    code = raw_text
                if code or raw_text.strip():
                    self._log_usage()
                    return raw_text, code
            except Exception as e:
                last_error = f"локален: {e}"
        self._last_local_error = last_error
        return None

    def generate_local_candidates(self, messages: list[dict], *, n: int = 2
                                  ) -> list[tuple[str, str, str]]:
        """"Best-of-N" + ансамбъл от МОДЕЛИ, специално за локалния tier (design
        note, 2026-08-11): локалният inference е безплатен (собствен GPU), само
        латентност — затова вместо да приемем сляпо първия отговор на слаб
        3B/7B/14B модел, вземаме до `n` ДОПЪЛНИТЕЛНИ кандидата и оставяме
        извикващия (autonomous_loop) да ги верифицира и избере най-добрия.

        Разнообразието идва от ДВЕ оси, в ред на приоритет:
          1. РАЗЛИЧЕН инсталиран tier (3b/7b/14b, ако е наличен друг освен
             текущия) — истинско разнообразие на модел, не само temperature.
          2. Различна temperature на СЪЩИЯ модел за оставащите слотове, ако
             няма друг инсталиран tier (не форсираме model-swap — на слаб GPU
             това е реален latency разход).

        Връща [(raw_text, code, model_used), ...] — само успешните опити,
        никога не хвърля (провалени опити просто липсват от резултата)."""
        if not self.local:
            return []
        try:
            from genesis_agent.model_router import LOCAL_TIERS, available_tiers
            avail = available_tiers()
            installed = [t for t, ok in zip(LOCAL_TIERS, avail) if ok]
        except Exception:
            installed = []
        base_model = self.local["model"]
        others = [m for m in installed if m != base_model]
        model_plan = (others + [base_model] * n)[:n]

        trimmed = self._trim_messages(messages)
        temps = [0.2, 0.9, 0.5, 1.1]
        out: list[tuple[str, str, str]] = []
        for i, model in enumerate(model_plan):
            temp = temps[i % len(temps)]
            try:
                raw_text, _tc = self._call(self.local["provider"], model, trimmed,
                                           extra={"temperature": temp})
            except Exception:
                log.debug("ensemble candidate %s failed, skipping it", model, exc_info=True)
                continue
            if not raw_text or not raw_text.strip():
                continue
            code = ""
            if "```python" in raw_text:
                code = raw_text.split("```python")[1].split("```")[0].strip()
            elif raw_text.lstrip().startswith(("def ", "import ", "from ", "class ")):
                code = raw_text
            out.append((raw_text, code, model))
        return out

    @staticmethod
    def _tool_tag_prompt() -> str:
        """Текстовата документация на тул-таговете (config.yaml tool_tag_prompt).

        Живее ОТДЕЛНО от system_prompt-а, защото при native tool-calling същите
        инструменти пристигат като JSON схеми — да го пращаме и двата пъти
        струваше ~530 токена на ВСЯКО обаждане, без да добавя нищо (измерено
        2026-07-27: system_prompt 1899 → 1199 токена след изнасянето). Кешира
        се, за да не чете config.yaml от диска на всяка реплика."""
        if Brain._TAG_PROMPT_CACHE is None:
            try:
                cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
                Brain._TAG_PROMPT_CACHE = (cfg.get("tool_tag_prompt") or "").strip()
            except Exception:
                Brain._TAG_PROMPT_CACHE = ""
        return Brain._TAG_PROMPT_CACHE

    @classmethod
    def _with_tool_tag_docs(cls, messages: list[dict]) -> list[dict]:
        """Добавя тул-таговете към system съобщението — САМО за модели без
        native tool-calling, които иначе не биха знали как да викат инструмент."""
        tags = cls._tool_tag_prompt()
        if not tags:
            return messages
        out = list(messages)
        for i, m in enumerate(out):
            if m.get("role") == "system":
                out[i] = {**m, "content": (m.get("content") or "") + "\n\n" + tags}
                return out
        # Няма system съобщение (рядко) → добавяме таговете като собствено.
        return [{"role": "system", "content": tags}] + out

    def _cache_stable_textmode(self, history: list[dict], tools: list[dict] | None,
                               fallback: list[dict]) -> list[dict]:
        """Текстовата история за доставчик с кеш по префикс (claude_cli) — без
        свиването на budget_history, което сменя вече пратени байтове (виж
        budget.cache_stable_history). Над тавана — `fallback`, свитата."""
        from genesis_agent.budget import cache_stable_history
        msgs = cache_stable_history(history)
        if msgs is None:
            return fallback
        if tools:
            return self._with_tool_tag_docs(self._sanitize_for_textmode(msgs))
        if any(m.get("role") == "tool" or m.get("tool_calls") for m in msgs):
            return self._sanitize_for_textmode(msgs)
        return msgs

    @staticmethod
    def _sanitize_for_textmode(messages: list[dict]) -> list[dict]:
        """Превръща native tool_calls/tool-role съобщения в обикновен текст.

        Нужно е, когато веригата пада от tools-способен модел към такъв БЕЗ
        tools параметър в СЪЩИЯ разговор — много доставчици връщат 400 при
        role='tool' съобщение, ако заявката не декларира и 'tools'. No-op ако
        историята вече е чист текст (обичайният случай)."""
        out: list[dict] = []
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
                out.append({"role": "assistant",
                            "content": (content + f"\n[извикани tool-ове: {calls_desc}]").strip()})
            else:
                out.append(m)
        return out

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 avoid: tuple[str, str] | None = None):
        """
        tools (design note, 2026-07-25): опционален списък OpenAI tool schemas
        (genesis_agent.tool_schemas). Ако е зададен — веригата пробва ПЪРВО
        моделите с потвърдена native function-calling поддръжка
        (config.yaml supports_tools), реален структуриран tool_calls в
        отговора. Ако паднат, останалите (или ако tools=None изобщо) минават
        в стария text-tag режим — извикващият код проверява
        `reply.tool_calls`: ако е зададен → native dispatch; иначе → старото
        regex парсене на [TAG: ...] от reply.raw_text. Без tools параметър
        поведението е 100% същото като преди (нулев риск за съществуващите
        извиквания от autonomous_loop/orchestrator/self_modify).

        avoid (design note, 2026-08-11): опционален (provider, model) чифт за
        изключване от тази конкретна заявка — за "критик" second-opinion
        разговори, извиквани на СЪЩИЯ Brain instance, който току-що е писал
        кода. Без това критикът много често пада на същия provider/model
        (chain-ът винаги обхожда отгоре надолу от началото), т.е. "второто
        мнение" структурно се съгласява със слепите петна на първото. Не
        засяга GENESIS_LOCAL_ONLY клона нарочно — там няма къде другаде да
        отиде заявката.
        """
        if not self.chain and not self.local:
            return self._error_result("Error: няма конфигурирани модели (config.yaml)")

        # Бюджетът на контекста се прилага ТУК, а не във фронтендите: това е
        # единствената точка, през която минава всичко — терминал, GUI, Jarvis,
        # мисиите, ensemble, orchestrator. Историята на извикващия
        # остава пълна (budget_history не мутира входа); свива се само копието,
        # което тръгва по мрежата.
        from genesis_agent.budget import budget_history, cache_history
        from genesis_agent.config import (
            CACHE_HISTORY_MAX_CHARS,
            CACHE_HISTORY_MAX_CHARS_FREE,
            CACHE_PROVIDERS,
        )
        # Доставчик, който кешира по префикс (Anthropic винаги, безплатните от
        # GENESIS_CACHE_PROVIDERS), получава историята само допълвана, без
        # свиването на budget_history, което сменя средата ѝ на всеки рунд
        # (виж config.CACHE_HISTORY_MAX_CHARS). Изгледът се смята при нужда.
        raw_history = list(messages)
        cache_views: dict[int, list[dict]] = {}

        def cache_view(provider: str) -> list[dict]:
            limit = (CACHE_HISTORY_MAX_CHARS if provider in _NATIVE_PROVIDERS
                     else CACHE_HISTORY_MAX_CHARS_FREE)
            if limit not in cache_views:
                cache_views[limit] = cache_history(raw_history, max_chars=limit)
            return cache_views[limit]

        full_history = messages
        messages = budget_history(messages)
        stable_notools: list[dict] | None = None   # за claude_cli (кеш по префикс)

        last_error = "неизвестна грешка"
        # Последната ИСТИНСКА грешка (тръгнала заявка). `last_error` се
        # презаписва и от синтетичните „skip: no KEY“ след нея — измерено
        # 2026-10-11: groq 429 и ollama_cloud отрязан → операторът и
        # `genesis -p --json` виждаха „последна: skip: no HF_TOKEN configured“.
        last_real_error = ""
        local_only = os.environ.get("GENESIS_LOCAL_ONLY") == "1"

        # LOCAL-ONLY: изричен офлайн режим — никога не пипа облака.
        if local_only:
            if not self.local:
                return self._error_result("Error: локален режим — няма наличен локален модел")
            # Бъг, хванат наживо 2026-07-31 (изричен локален режим + реални tools):
            # този клон връщаше рано БЕЗ да мине през _with_tool_tag_docs/
            # _sanitize_for_textmode по-долу — локалният модел никога не
            # научаваше какъв е синтаксисът на тул-таговете, затова само
            # разказваше намерение ("Използвам браузър...") без да върне нищо
            # парсваемо. Локалният модел НИКОГА не връща native tool_calls
            # (виж _call_local) — единственият му път е text-tag режимът, а той
            # изисква точно тази документация в system съобщението.
            local_messages = (
                self._with_tool_tag_docs(self._sanitize_for_textmode(messages))
                if tools else messages
            )
            hit = self._call_local(local_messages, attempts=2)
            if hit:
                raw_text, code = hit
                return type("Obj", (object,), {"raw_text": raw_text, "code": code,
                                               "usage": self._last_usage, "tool_calls": None})
            return self._error_result(f"Error: локален режим — {self._last_local_error}")

        # ОБЛАКЪТ Е ВИНАГИ ПЪРВИ (design note, 2026-07-25): локалният мозък — малък,
        # среден или голям tier, без значение — е ПОСЛЕДНА резерва. Пробва се
        # само ако ЦЯЛАТА облачна верига (всички доставчици, всички ключове,
        # RETRY_ROUNDS обхождания) е изчерпана. Облакът е безплатен и по-силен
        # от локалния 3B/7B/14B — няма смисъл да предпочитаме по-слабия модел,
        # докато има свободна облачна квота.
        # Data-driven деприоритизация (design note, 2026-07-25): доставчик с
        # ПОТВЪРДЕН (≥5 извадки) rolling success rate <50% минава на края на
        # веригата ТОЗИ рунд — не се маркира изчерпан, не се трие, просто не
        # хабим първия опит на здравите доставчици върху нещо болно. Никога
        # не хвърля — статистиката е "nice to have", не критичен път.
        # Reordered into a LOCAL list, never back into self.chain: this is a
        # judgement about right now, and writing it back made it permanent —
        # the reorder was stable and each call re-sorted the already-sorted
        # chain, so a provider demoted during one bad minute never climbed back
        # to its configured position even after it started answering again.
        chain = self.chain
        try:
            from genesis_agent.provider_stats import deprioritize_flaky
            chain = deprioritize_flaky(chain)
        except Exception as e:
            log.debug("provider_stats недостъпен, реда на веригата остава непроменен: %s", e)

        # tools заявен → tools-способните (config.yaml supports_tools) вървят
        # ПРЪВ (native, реален tool_calls), останалите — след тях, в стария
        # text-tag режим (историята се "почиства" от native формат за тях,
        # виж _sanitize_for_textmode). Без tools — редът е непроменен.
        if tools:
            capable = [c for c in chain if c.get("supports_tools")]
            rest = [c for c in chain if not c.get("supports_tools")]
            ordered_chain = capable + rest
            # Текстовият вариант получава И документацията на таговете — native
            # моделите я НЕ получават (имат JSON схемите), което е спестяването.
            messages_notools = self._with_tool_tag_docs(
                self._sanitize_for_textmode(messages)
            )
        else:
            ordered_chain = chain
            # Без tools, но с native tool история (лекият чат маршрут след
            # tool ход): role='tool' без декларирани 'tools' е 400 при много
            # доставчици и винаги при Anthropic, тоест ходът падаше по цялата
            # лека верига и ескалираше (одит 2026-10-11). Чиста текстова
            # история остава байт по байт същата.
            messages_notools = (self._sanitize_for_textmode(messages)
                                if any(m.get("role") == "tool" or m.get("tool_calls")
                                       for m in messages)
                                else messages)

        # An explicit pin outranks any reordering above (tools-capability sort
        # or deprioritize_flaky), in EITHER branch. The operator picked that
        # model in the `/model` menu; quietly answering from a different one —
        # because theirs lacks native tool-calling, or because provider_stats
        # decided it looked flaky — is not our call to make. This used to live
        # only inside the `if tools:` branch, so a pinned provider that
        # deprioritize_flaky pushed to the back stayed there for plain
        # (tools=None) calls, with no error and no log line distinguishing it
        # from a normal fallback. Pinned still means first, not only: it falls
        # back normally if it fails.
        if self._pinned is not None:
            ordered_chain = ([self._pinned]
                             + [c for c in ordered_chain if c is not self._pinned])

        if avoid:
            ordered_chain = [c for c in ordered_chain
                             if (c["provider"], c["model"]) != avoid]

        n = len(ordered_chain)
        cut_hit: tuple | None = None  # (отговор, usage, запис) на първия отрязан tool_call
        if n > 0:
            # ВИНАГИ отгоре надолу, прескачайки временно изчерпаните.
            #
            # По-рано тук имаше ротиращ офсет, който се местеше след всеки
            # УСПЕХ, за да разпределя товара. Три неща не работеха:
            #   • всяко съобщение идваше от друг модел, с видимо различно
            #     качество — потребителят го усеща като нестабилност;
            #   • веригата стигаше до дъното си, докато горните модели са
            #     напълно свободни;
            #   • и най-лошото: щом веднъж слезеше долу, оставаше там —
            #     офсетът не се връщаше нагоре, когато горните излязат от
            #     cooldown.
            # Обхождане отгоре дава и залепване (докато моделът работи, той
            # отговаря), и самолечение (щом cooldown-ът мине, се връщаме на
            # най-добрия наличен), без нищо да се помни между извикванията.
            # Леката работа (light=True) обхожда веригата веднъж: викащият има
            # своя резерва — терминалът ескалира към силния Brain, резюмето и
            # паметта просто се пропускат. С два кръга „здрасти“ при паднала
            # мрежа чакаше 8 s в лекия Brain и още 8 s в силния — 10 заявки и
            # 16 s преди какъвто и да е отговор (одит 2026-10-11).
            rounds = 1 if self.light else RETRY_ROUNDS
            for round_i in range(rounds):
                # Кръг, в който НИТО един модел няма ключ, не се повтаря: паузата
                # и новите кръгове не могат да добавят ключ. Без ключове всяко
                # съобщение чакаше 16 s, преди да каже нещо (2026-10-09, `genesis -p`
                # на чиста инсталация).
                only_unconfigured = True
                sent_any = False  # тръгнала ли е поне една истинска заявка
                # Нов кръг има смисъл само след грешка, която може да мине:
                # HTTP_TRUNCATED не слага cooldown и същата заявка се реже пак.
                retry_useful = False
                cooled: list[tuple[str, str]] = []  # пропуснати заради cooldown
                for step in range(n):
                    attempt = ordered_chain[step]
                    prov, model = attempt["provider"], attempt["model"]
                    key = f"{prov}::{model}"
                    if _is_exhausted(key):
                        only_unconfigured = False
                        cooled.append((prov, model))
                        continue  # временно изчерпан → пропускаме
                    use_tools = tools if (tools and attempt.get("supports_tools")) else None
                    msgs = messages if use_tools else messages_notools
                    if use_tools and (prov in _NATIVE_PROVIDERS or prov in CACHE_PROVIDERS
                                      or key in CACHE_PROVIDERS):
                        msgs = cache_view(prov)
                    if prov == "claude_cli":
                        if stable_notools is None:
                            stable_notools = self._cache_stable_textmode(
                                full_history, tools, messages_notools)
                        msgs = stable_notools
                    size = _request_size(msgs, use_tools)
                    if size >= _TOO_LARGE.get(key, size + 1):
                        only_unconfigured = False
                        last_error = f"{prov}/{model}: заявката е над лимита му за един път"
                        continue
                    t0 = time.time()
                    self._attempt_id = (prov, model)
                    self._usage_attempt = None
                    self._cut_reply = None
                    try:
                        try:
                            raw_text, tool_calls = self._call(prov, model, msgs, tools=use_tools)
                        except RuntimeError as e:
                            if not _is_transient(str(e)):
                                raise
                            # В статистиката влиза само крайният изход на
                            # обръщението: 500 + провал на повторния опит се
                            # броеше за два провала и сваляше ollama в края на
                            # веригата за 15 мин (bench 2026-09-30: 78 отговора
                            # на NVIDIA без нито един опит на ollama срещу 25).
                            reason = " ".join(str(e).split())[:80]
                            print(f"  [Brain] ↻ {prov}/{model} след {time.time() - t0:.1f}s: "
                                  f"{reason} → същият пак след {_TRANSIENT_PAUSE_S:.0f}s")
                            time.sleep(_TRANSIENT_PAUSE_S)
                            t0 = time.time()
                            raw_text, tool_calls = self._call(prov, model, msgs, tools=use_tools)
                        sent_any = True
                        self._record_stat(prov, time.time() - t0, True)
                        self._fail_count = 0
                        self.current = attempt
                        if step > 0 or round_i > 0:
                            print(f"  [Brain] ↪ модел: {prov}/{model}")
                        code = ""
                        if "```python" in raw_text:
                            code = raw_text.split("```python")[1].split("```")[0].strip()
                        elif raw_text.lstrip().startswith(("def ", "import ", "from ", "class ")):
                            code = raw_text
                        self._log_usage()
                        return type("Obj", (object,), {"raw_text": raw_text, "code": code,
                                                       "usage": self._last_usage, "tool_calls": tool_calls})
                    except requests.exceptions.RequestException as e:
                        only_unconfigured = False
                        sent_any = True
                        retry_useful = True
                        last_error = last_real_error = f"мрежа: {e}"
                        self._fail_count += 1
                        self._record_stat(prov, time.time() - t0, False)
                        _print_skip(prov, model, last_error, time.time() - t0)
                        continue
                    except RuntimeError as e:
                        last_error = str(e)
                        # Пропуск без заявка („skip: няма ключ“, „cooling down“) не
                        # е провал на доставчика — в статистиката влизат само
                        # истински обръщения (одит 2026-10-07: 3 провала на groq
                        # за 1 заявка).
                        synthetic = last_error.startswith("skip:") or "cooling down" in last_error
                        if not (last_error.startswith("skip:") and "configured" in last_error):
                            only_unconfigured = False
                        if "cooling down" in last_error:
                            cooled.append((prov, model))
                        # Грешка, която слага cooldown (429/402/503), купува втори
                        # кръг само ако cooldown-ът свършва в паузата — затова
                        # моделът влиза в `cooled`, не в retry_useful (e2e
                        # 2026-10-11). Измерено: само groq с ключ, 429 без
                        # подсказка (300 s) → 8 s пауза и кръг без нито една
                        # заявка; 429 на A + отрязан WRITE_FILE на C → C се
                        # питаше (и плащаше) втори път за същия сигурен провал.
                        cools = not synthetic and any(f"HTTP_{c}" in last_error
                                                      for c in (*_EXHAUST_CODES, 410))
                        if cools:
                            cooled.append((prov, model))
                        if not synthetic:
                            sent_any = True
                            last_real_error = last_error
                            if _too_large(last_error):
                                _TOO_LARGE[key] = min(_TOO_LARGE.get(key, size), size)
                            elif not _hit_output_cap(last_error):
                                retry_useful = retry_useful or not cools
                                # Изяден таван не е болен доставчик (2026-10-11):
                                # 5 отрязани големи WRITE_FILE на groq даваха
                                # success_rate 0.0 и deprioritize_flaky го местеше
                                # последен за 15 мин за всички без пин (genesis
                                # fix, леката верига, компресията). Разходът пак
                                # се записва по-долу.
                                self._fail_count += 1
                                self._record_stat(prov, time.time() - t0, False)
                            elif cut_hit is None and getattr(self, "_cut_reply", None):
                                cut_hit = (self._cut_reply, self._last_usage, attempt)
                            # Отрязан/отказан отговор е платен — разходът се записва.
                            if getattr(self, "_usage_attempt", None) == (prov, model):
                                previous, self.current = getattr(self, "current", None), attempt
                                self._log_usage()
                                self.current = previous
                        self._usage_attempt = None
                        _print_skip(prov, model, last_error, time.time() - t0)
                        # При 429/503/402 → маркирай изчерпан за cooldown (спестява безсмислени опити).
                        # Без заявка (synthetic) — не: „GROQ_API_KEY … cooling
                        # down“ започва с HTTP_429 и всеки модел под ключа
                        # получаваше свежи 300 s, тоест оставаше извън веригата
                        # до 5 мин след като ключът се възстанови (одит
                        # 2026-10-11: ключ с 1 s оставащ cooldown → groq/A и
                        # groq/B с по 300 s). Cooldown-ът на ключа покрива това.
                        for c in () if synthetic else _EXHAUST_CODES:
                            if f"HTTP_{c}" in last_error:
                                _mark_exhausted(key, _cooldown_for(last_error))
                                break
                        if "HTTP_410" in last_error:
                            _EXHAUSTED[key] = time.time() + _GONE_COOLDOWN
                            print(f"  [Brain] ⛔ {prov}/{model} е спрян от доставчика (410) — "
                                  "пропускам го; махни го от config.yaml")
                        continue
                if only_unconfigured:
                    last_error = "нито един модел няма API ключ — пусни `genesis setup`"
                    break
                if sent_any and not retry_useful and not any(
                        self._cooldown_left(p, m) <= _ROUND_PAUSE_S for p, m in cooled):
                    # Всяка изпратена заявка е отрязана на тавана (ревю
                    # 2026-10-11): втори кръг е още веднъж цялата верига,
                    # платена, след 8 s — със същия сигурен провал. Само за
                    # изпратените: модел, пропуснат заради cooldown, който
                    # свършва в паузата (Groq TPM 429 с 5 s), може да напише
                    # файла цял — за него вторият кръг остава (ревю 2026-10-11,
                    # регресия на F4 спрямо базовия клон).
                    break
                if not sent_any:
                    # Кръг без нито една заявка — всичко е в cooldown или без
                    # ключ. Нов кръг помага само ако някой cooldown свършва
                    # по време на паузата (одит 2026-10-11: всички модели с
                    # ~290 s оставащи → 0 заявки, 8 s чакане и „неизвестна
                    # грешка“).
                    wait = min((self._cooldown_left(p, m) for p, m in cooled), default=None)
                    if wait is not None:
                        last_error = (f"всички модели са в cooldown "
                                      f"(най-рано след {math.ceil(wait)} s)")
                    if wait is None or wait > _ROUND_PAUSE_S:
                        break
                if round_i + 1 < rounds:
                    print("  [Brain] Всички облачни модели заети/изчерпани, кратка пауза и нов кръг...")
                    time.sleep(_ROUND_PAUSE_S)

        if cut_hit is not None:
            # Файл, по-голям от тавана на ВСЕКИ модел (ревю 2026-10-11): преди
            # F4 отрязаният WRITE_FILE стигаше до цикъла с „Невалидни
            # аргументи“ и моделът можеше да раздели записа; с HTTP_TRUNCATED
            # ходът свършваше с „Error“ след 2 кръга × веригата. Връща се
            # първият отрязан call — локалният модел (без native tools, по-малък
            # таван) няма да напише същия файл цял.
            (raw_text, tool_calls), usage, self.current = cut_hit
            print("  [Brain] ✂ всеки модел отряза tool_call на тавана → към цикъла")
            return type("Obj", (object,), {"raw_text": raw_text, "code": "",
                                           "usage": usage, "tool_calls": tool_calls})

        # ПОСЛЕДНА РЕЗЕРВА: локалният собствен мозък — само ако облакът напълно
        # отказа (или изобщо няма конфигурирани облачни модели).
        local_avoided = bool(avoid and self.local
                             and (self.local["provider"], self.local["model"]) == avoid)
        if self.local and not local_avoided:
            print("  [Brain] ☁️ Облакът е изчерпан/недостъпен → 🏠 локален мозък като резерва...")
            hit = self._call_local(messages_notools, attempts=1)
            if hit:
                raw_text, code = hit
                return type("Obj", (object,), {"raw_text": raw_text, "code": code,
                                               "usage": self._last_usage, "tool_calls": None})
            if self._last_local_error:
                last_error = last_real_error = self._last_local_error

        return self._error_result(
            f"Error: цялата верига е изчерпана | последна: {last_real_error or last_error}")

    def _error_result(self, msg: str):
        return type("Obj", (object,), {"raw_text": msg, "code": "", "usage": None, "tool_calls": None})

    def _record_stat(self, provider: str, latency_s: float, success: bool) -> None:
        """Безопасно логва latency+success за data-driven ред (genesis_agent.provider_stats).
        Никога не хвърля — статистиката не бива да чупи мисии."""
        try:
            from genesis_agent.provider_stats import record_call
            record_call(provider, latency_s, success)
        except Exception as e:
            log.debug("provider_stats недостъпен, повикването не е записано: %s", e)

    def _log_usage(self) -> None:
        """Безопасно логва token usage за последното успешно извикване (ако има).
        Никога не хвърля — бюджетното логване не бива да чупи мисии."""
        if not self._last_usage or not self.current:
            return
        try:
            from genesis_agent.budget import record_usage
            record_usage(
                provider=self.current.get("provider", "?"),
                model=self.current.get("model", "?"),
                prompt_tokens=int(self._last_usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(self._last_usage.get("completion_tokens", 0) or 0),
                cached_read_tokens=int(self._last_usage.get("cached_read_tokens", 0) or 0),
                cached_write_tokens=int(self._last_usage.get("cached_write_tokens", 0) or 0),
                cost_usd=self._last_usage.get("cost_usd"),
            )
        except Exception as e:
            log.debug("budget недостъпен, usage не е записан: %s", e)
