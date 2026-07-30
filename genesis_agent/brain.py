"""
genesis_agent.brain — the single provider chain behind every frontend.

One `Brain` call walks the model chain from `config.yaml` top to bottom:
strongest cloud model first, falling through to the next on any failure, and
finally to a local Ollama model if one is installed. Providers you have no key
for are skipped silently, so the agent works with one key or with six.

Design notes worth knowing before you change anything here:
  • ONE key per provider. Rotating several free accounts to multiply a quota
    violates most providers' terms; breadth across providers is the supported
    way to get headroom.
  • Models that support native OpenAI tool-calling are preferred when `tools=`
    is passed; the rest fall back to the text-tag protocol in `config.yaml`.
  • A provider that returns 429/402/503/401/403 is put on a cooldown rather
    than retried immediately — see `_mark_exhausted`.

Public interface: `Brain()`, `system_prompt_base()`, `complete(messages)` →
object with `.raw_text` and `.code`.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

import requests
import yaml

from genesis_agent.paths import (
    CONFIG_PATH,
    ENV_FILES,
    _strip_inline_comment,
)

log = logging.getLogger("genesis.brain")

CLOUD_TIMEOUT = 90
LOCAL_TIMEOUT = 240  # локалният 3B модел на слаб GPU е бавен — даваме му време
RETRY_ROUNDS = 2  # колко пъти да обходим цялата верига при пълен провал

# OpenAI-съвместими доставчици (base_url + env ключ; None ключ = без auth, локален).
_PROVIDERS = {
    "ollama_local": ("http://localhost:11434/v1", None),   # СОБСТВЕН локален мозък
    "huggingface": ("https://router.huggingface.co/v1", "HF_TOKEN"),  # голями бързи, отделна квота
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "cohere":     ("https://api.cohere.ai/compatibility/v1", "COHERE_API_KEY"),
    "groq":       ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "nvidia":     ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
    # Ollama Cloud (различно от ollama_local!) — ~5M токена/седмица безплатно,
    # СЪЩИЯТ OpenAI-съвместим формат, но много по-големи модели (:cloud суфикс)
    # отколкото GTX 1650 4GB може да върти локално. Добавено 2026-07-25.
    "ollama_cloud": ("https://ollama.com/v1", "OLLAMA_API_KEY"),
}

# Локалният модел (собственият мозък на Genesis). Празно → изключен.
LOCAL_MODEL = os.environ.get("GENESIS_LOCAL_MODEL", "qwen2.5-coder:3b")


def _local_available(model: str) -> bool:
    """Проверява дали Ollama върви локално и има модела."""
    if not model:
        return False
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=2)
        if r.status_code == 200:
            names = [m.get("name", "") for m in r.json().get("models", [])]
            return any(model.split(":")[0] in n for n in names)
    except Exception:
        return False
    return False
# Грешки, при които минаваме към следващия модел (вкл. остарял/невалиден модел).
_FALLBACK_CODES = {400, 401, 402, 403, 404, 408, 429, 500, 502, 503}
# Кодове, при които моделът/квотата е ВРЕМЕННО изчерпан → cooldown, не го пробвай пак веднага.
_EXHAUST_CODES = {429, 402, 503}
_EXHAUST_COOLDOWN = 300  # секунди (5 мин) — колкото типичен OpenRouter free reset

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


def _mark_exhausted(key: str) -> None:
    _EXHAUSTED[key] = time.time() + _EXHAUST_COOLDOWN


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
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip().removeprefix("export ").strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                keys.setdefault(k.strip(), _strip_inline_comment(v.strip()).strip('"').strip("'"))
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

    # Само доставчици, които знаем как да викаме.
    return [c for c in chain if c["provider"] in _PROVIDERS]


class Brain:
    # Кеш за config.yaml tool_tag_prompt (виж _tool_tag_prompt).
    _TAG_PROMPT_CACHE: str | None = None

    def __init__(self, prefer_provider: str | None = None, use_local: bool = True,
                 min_size_b: float = 0, pin_model: tuple[str, str] | None = None):
        self.keys = _load_keys()
        self.chain = _load_chain()
        self.timeout = CLOUD_TIMEOUT
        self._fail_count = 0
        self._last_usage: dict | None = None
        self._last_local_error: str | None = None
        # min_size_b (design note, 2026-07-25): филтрира облачната верига по размер —
        # различни контексти искат различно качество/скорост: Discord чат ≥17B,
        # терминален чат ≥32B, умения/самонадграждане ≥120B (виж config.yaml
        # size_b анотациите). Локалният мозък НЕ се филтрира — той си остава
        # последна резерва независимо от размера, по-добре слаб отговор,
        # отколкото никакъв при пълен облачен провал.
        if min_size_b:
            self.chain = [c for c in self.chain if c.get("size_b", 0) >= min_size_b]
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
            if use_local and _local_available(LOCAL_MODEL):
                self.local = {"provider": "ollama_local", "model": LOCAL_MODEL}

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
                self._pinned = existing or {"provider": p_prov, "model": p_model,
                                            "size_b": 0, "supports_tools": False}
                self.chain = [self._pinned] + rest

        self.current = (self.chain[0] if self.chain else None) or self.local

    def route_for_goal(self, goal: str) -> None:
        """Адаптивно избира локалния модел според сложността на задачата."""
        try:
            from genesis_agent.model_router import pick_model
            m = pick_model(goal)
            if m:
                self.local = {"provider": "ollama_local", "model": m}
                self.current = self.local
        except Exception as e:
            log.debug("route_for_goal: model_router недостъпен, местният модел остава по подразбиране: %s", e)

    def escalate(self) -> bool:
        """Качва локалния мозък на следващия по-голям НАЛИЧЕН модел. True ако е сменен."""
        try:
            from genesis_agent.model_router import next_tier_model
            cur = self.local["model"] if self.local else None
            nxt = next_tier_model(cur)
            if nxt and nxt != cur:
                self.local = {"provider": "ollama_local", "model": nxt}
                self.current = self.local
                print(f"  [Brain] ⬆️ Ескалация към по-голям модел: {nxt}")
                return True
        except Exception as e:
            log.debug("escalate: model_router недостъпен, оставам на текущия модел: %s", e)
        return False

    def system_prompt_base(self) -> str:
        return (
            "ABSOLUTE DIRECTIVE: You are Genesis Agent — an autonomous agent running on your "
            "operator's own machine. You are NOT Llama, Nvidia, Mistral or ChatGPT; you use "
            "their networks only as an engine. Your job is to carry out missions as Python code.\n"
            "Always return WORKING code in a single ```python ... ``` block, with an "
            "assert-based self-test that prints 'OK' on success. No explanation, code only.\n"
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
        същото изречение, затова 0.55 прагът в skill_loader.search_skills НЕ е
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

    def _trim_messages(self, messages: list[dict], max_chars: int = 2000) -> list[dict]:
        # Облачните модели имат голям контекст — не режем.
        return messages

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
        """
        if len(messages) <= 4:
            return messages
        tail = messages[-2:]
        if tail and tail[-1].get("role") == "tool" and not tail[0].get("tool_calls"):
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

        old_part = rest[:-keep_recent]
        recent_part = rest[-keep_recent:]
        transcript = "\n".join(
            f"{m.get('role', '?')}: {str(m.get('content', ''))[:500]}" for m in old_part
        )
        maxlen = getattr(messages, "maxlen", None)

        def _rebuild(parts: list) -> deque | list:
            return deque(parts, maxlen=maxlen) if maxlen else parts

        try:
            brain = Brain()
            summary_reply = brain.complete([
                {"role": "system", "content": (
                    "Обобщи накратко (5-8 изречения) ключовите факти, решения и контекст "
                    "от този разговор. Само същественото за бъдещи реплики, не преразказ."
                )},
                {"role": "user", "content": transcript[:8000]},
            ])
            summary = (summary_reply.raw_text or "").strip()
            if not summary:
                raise ValueError("празно резюме")
        except Exception:
            # Fallback: просто пази последните, без резюме (губи старото честно,
            # не гърми разговора).
            return _rebuild([system_msg] + recent_part)

        summary_msg = {"role": "system", "content": "## Резюме на по-ранния разговор:\n" + summary}
        return _rebuild([system_msg, summary_msg] + recent_part)

    def _check_backend(self) -> bool:
        return bool(self.chain)

    def _provider_key(self, key_env: str) -> str | None:
        """
        The one key for this provider, or None if it is not configured.

        Deliberately one key per provider. Rotating several free-tier accounts
        to multiply a quota is against most providers' terms of service, so it
        is not something this project ships. Resilience comes from breadth
        instead — many providers in the chain, each used within its own limits.
        """
        val = self.keys.get(key_env)
        if val is None:
            return None
        val = str(val).strip()
        return val or None

    def _http(self, base_url: str, key: str | None, model: str, messages: list[dict], timeout: int,
             tools: list[dict] | None = None) -> tuple[str, list | None]:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload: dict[str, Any] = {"model": model, "messages": messages,
                                   "temperature": 0.7, "max_tokens": 4096}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        r = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout,
        )
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
            message = data["choices"][0]["message"]
            content = message.get("content")
            tool_calls = message.get("tool_calls") or None
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"HTTP_200_MALFORMED: {type(e).__name__}: {r.text[:150]}")
        if not content and not tool_calls:
            raise RuntimeError("празен отговор")
        # usage липсва при локален Ollama /v1 понякога — None е ОК, budget.py го обработва.
        self._last_usage = data.get("usage")
        return (content or "").strip(), tool_calls

    def _call(self, provider: str, model: str, messages: list[dict],
             tools: list[dict] | None = None) -> tuple[str, list | None]:
        base_url, key_env = _PROVIDERS[provider]
        if not key_env:  # локален — без ключ
            return self._http(base_url, None, model, messages, LOCAL_TIMEOUT, tools=tools)

        key = self._provider_key(key_env)
        if not key:
            raise RuntimeError(f"skip: no {key_env} configured")

        kid = f"key::{key_env}"
        if _is_exhausted(kid):
            raise RuntimeError(f"HTTP_429: {key_env} is rate-limited, cooling down")

        try:
            return self._http(base_url, key, model, messages, self.timeout, tools=tools)
        except RuntimeError as e:
            last = str(e)
            # 429/402/503 (quota gone) or 401/403 (bad or forbidden key): this
            # provider is unusable for a while. Mark it and let the chain move
            # on to the next provider rather than hammering a dead endpoint.
            if any(f"HTTP_{c}" in last for c in _EXHAUST_CODES | {401, 403}):
                _mark_exhausted(kid)
                print(f"  [Brain] 🔑 {key_env} unavailable ({last[:60]}) → next provider")
            raise

    def _call_local(self, messages: list[dict], attempts: int = 1) -> tuple[str, str] | None:
        """Пробва локалния мозък (текущия tier — 3b/7b/14b, каквото е в self.local).
        Връща (raw_text, code) при успех, None при провал. Локалният никога не
        получава tools (проверено наживо — не връща структуриран tool_calls,
        а суров JSON текст в content), винаги text-tag режим."""
        last_error = None
        loc = self.local
        if not loc:
            return None
        for _try in range(attempts):
            try:
                raw_text, _tc = self._call(loc["provider"], loc["model"], messages)
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

    def complete(self, messages: list[dict], tools: list[dict] | None = None):
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
        """
        if not self.chain and not self.local:
            return self._error_result("Error: няма конфигурирани модели (config.yaml)")

        last_error = "неизвестна грешка"
        local_only = os.environ.get("GENESIS_LOCAL_ONLY") == "1"

        # LOCAL-ONLY: изричен офлайн режим — никога не пипа облака.
        if local_only:
            if not self.local:
                return self._error_result("Error: локален режим — няма наличен локален модел")
            hit = self._call_local(messages, attempts=2)
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
            messages_notools = messages

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

        n = len(ordered_chain)
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
            for round_i in range(RETRY_ROUNDS):
                for step in range(n):
                    attempt = ordered_chain[step]
                    prov, model = attempt["provider"], attempt["model"]
                    key = f"{prov}::{model}"
                    if _is_exhausted(key):
                        continue  # временно изчерпан → пропускаме
                    use_tools = tools if (tools and attempt.get("supports_tools")) else None
                    msgs = messages if use_tools else messages_notools
                    t0 = time.time()
                    try:
                        raw_text, tool_calls = self._call(prov, model, msgs, tools=use_tools)
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
                        last_error = f"мрежа: {e}"
                        self._fail_count += 1
                        self._record_stat(prov, time.time() - t0, False)
                        continue
                    except RuntimeError as e:
                        last_error = str(e)
                        self._fail_count += 1
                        self._record_stat(prov, time.time() - t0, False)
                        # При 429/503/402 → маркирай изчерпан за cooldown (спестява безсмислени опити).
                        for c in _EXHAUST_CODES:
                            if f"HTTP_{c}" in last_error:
                                _mark_exhausted(key)
                                break
                        continue
                if round_i + 1 < RETRY_ROUNDS:
                    print("  [Brain] Всички облачни модели заети/изчерпани, кратка пауза и нов кръг...")
                    time.sleep(8)

        # ПОСЛЕДНА РЕЗЕРВА: локалният собствен мозък — само ако облакът напълно
        # отказа (или изобщо няма конфигурирани облачни модели).
        if self.local:
            print("  [Brain] ☁️ Облакът е изчерпан/недостъпен → 🏠 локален мозък като резерва...")
            hit = self._call_local(messages_notools, attempts=1)
            if hit:
                raw_text, code = hit
                return type("Obj", (object,), {"raw_text": raw_text, "code": code,
                                               "usage": self._last_usage, "tool_calls": None})
            last_error = self._last_local_error or last_error

        return self._error_result(f"Error: цялата верига е изчерпана | последна: {last_error}")

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
            )
        except Exception as e:
            log.debug("budget недостъпен, usage не е записан: %s", e)
