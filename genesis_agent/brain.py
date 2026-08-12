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
    last_model_path,
)

# Extra ollama_cloud keys an operator may add THEMSELVES to their own
# gitignored ~/.genesis/.env — OLLAMA_API_KEY_2 through _10. Not suggested,
# not enabled by default, not touched unless these exact names are present.
# See the module docstring above.
_OLLAMA_CLOUD_KEY_ENVS = ["OLLAMA_API_KEY"] + [f"OLLAMA_API_KEY_{i}" for i in range(2, 11)]

log = logging.getLogger("genesis.brain")

CLOUD_TIMEOUT = 90
LOCAL_TIMEOUT = 240  # локалният 3B модел на слаб GPU е бавен — даваме му време
RETRY_ROUNDS = 2  # колко пъти да обходим цялата верига при пълен провал

# Таван на изходните токени за OpenAI-съвместимите доставчици. 4096 е стойността,
# с която проектът работи от началото — оставена е като подразбиране нарочно
# (всеки безплатен доставчик във веригата я приема; по-висока стойност някои
# отхвърлят с 400). Env override-ът съществува, защото 4096 е РЕАЛНО ограничение
# за по-дълъг скрипт: виж _is_truncated и коментара в _http за какво се случваше
# преди, когато таванът се удари.
MAX_OUTPUT_TOKENS = int(os.environ.get("GENESIS_MAX_TOKENS", "4096"))

# Anthropic native (само при quality="max"). max_tokens покрива И размисъла, И
# отговора — 16k стига за код без да изисква streaming (над ~16k SDK-то иска
# stream, за да не удари HTTP timeout). `xhigh` е препоръчаното ниво за кодинг
# и агентна работа; отделен запис в config.yaml може да го смени за конкретен модел.
ANTHROPIC_MAX_TOKENS = 16000
ANTHROPIC_EFFORT = "xhigh"

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
}

# Кои доставчици се викат през native SDK вместо през OpenAI-съвместим HTTP.
_NATIVE_PROVIDERS = {"anthropic"}

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


def _load_last_model() -> tuple[str, str] | None:
    """(provider, model) Brain last completed successfully with, or None on
    first run / corrupt file. Never raises — a missing or bad file just means
    'start from the top of the chain', the same as before this existed."""
    try:
        data = json.loads(last_model_path().read_text(encoding="utf-8"))
        provider, model = data.get("provider"), data.get("model")
        if provider and model:
            return str(provider), str(model)
    except Exception:
        pass
    return None


def _save_last_model(provider: str, model: str) -> None:
    """Best-effort — a failed write should never break a completion that
    already succeeded, so this swallows its own errors."""
    try:
        p = last_model_path()
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        p.write_text(json.dumps({"provider": provider, "model": model}), encoding="utf-8")
    except Exception as e:
        log.debug("could not persist last_model: %s", e)


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
    for entry in (cfg.get("models", {}) or {}).get("coding_models", []) or []:
        provider, model = entry.get("provider"), entry.get("model")
        if not provider or not model or provider not in _PROVIDERS:
            continue
        out.append({"provider": provider, "model": model,
                    "size_b": entry.get("size_b", 0),
                    "supports_tools": bool(entry.get("supports_tools", True)),
                    "coding": True})
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


class Brain:
    # Кеш за config.yaml tool_tag_prompt (виж _tool_tag_prompt).
    _TAG_PROMPT_CACHE: str | None = None

    def __init__(self, prefer_provider: str | None = None, use_local: bool = True,
                 min_size_b: float = 0, pin_model: tuple[str, str] | None = None,
                 quality: str | None = None):
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
        self.quality = (quality or os.environ.get("GENESIS_QUALITY") or "").strip().lower()
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
            if use_local and _local_available(LOCAL_MODEL):
                self.local = {"provider": "ollama_local", "model": LOCAL_MODEL}

        # Резюме на последния успешен модел (design note, added at operator's
        # request): ако никой не е избрал изрично pin_model за тази сесия (и
        # не сме в prefer_provider паралелен worker, който има собствена
        # логика), опитваме последното (provider, model), с което Brain е
        # завършил успешно предния път — записано от `_save_last_model` в
        # complete()/_call_local(). Съвсем същият механизъм като pin_model
        # по-долу: първо в опашката, но с нормален fallback ако вече не е наличен.
        if not pin_model and not prefer_provider:
            last = _load_last_model()
            if last:
                pin_model = last

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
            # thread_worker, терминала) вече проверяват точно това.
            if not summary or summary.startswith("Error:"):
                raise ValueError(f"неизползваемо резюме: {summary[:80] or 'празно'}")
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
                                   "temperature": 0.7, "max_tokens": MAX_OUTPUT_TOKENS}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if extra:
            payload.update(extra)
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
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content")
            tool_calls = message.get("tool_calls") or None
            finish_reason = choice.get("finish_reason") or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"HTTP_200_MALFORMED: {type(e).__name__}: {r.text[:150]}")
        if not content and not tool_calls:
            raise RuntimeError("празен отговор")
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
        if finish_reason == "length" and _is_truncated(content or ""):
            raise RuntimeError(
                f"HTTP_TRUNCATED: отговорът е отрязан на тавана от {MAX_OUTPUT_TOKENS} "
                f"токена, посред код-ограда (finish_reason=length). Вдигни го с "
                f"GENESIS_MAX_TOKENS, ако доставчикът го позволява."
            )
        # usage липсва при локален Ollama /v1 понякога — None е ОК, budget.py го обработва.
        self._last_usage = data.get("usage")
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
        for m in messages:
            role = m.get("role")
            content = m.get("content") or ""
            if role == "system":
                if content:
                    system_parts.append(content)
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
        # Разговорът трябва да започва с user ход.
        while out and out[0]["role"] != "user":
            out.pop(0)
        return "\n\n".join(system_parts), out

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
            "max_tokens": ANTHROPIC_MAX_TOKENS,
            "messages": msgs,
            # Adaptive thinking: моделът сам решава колко да мисли по задачата.
            # Точно това искаме тук — MAX режимът съществува заради трудните
            # случаи, а лесните не бива да плащат за размисъл, който не им трябва.
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort or ANTHROPIC_EFFORT},
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = self._to_anthropic_tools(tools)

        client = anthropic.Anthropic(api_key=key, timeout=float(self.timeout))
        try:
            # Класификаторите за безопасност могат да откажат заявка (връща се
            # 200 с stop_reason="refusal"). `fallbacks="default"` оставя сървъра
            # да я довърши на друг модел вместо да ни върне празен отговор.
            resp = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
        except anthropic.BadRequestError as e:
            # Акаунт без достъп до тази beta (или SDK, който не я познава) —
            # същата заявка без нея. Нашата собствена верига поема отказа.
            if "fallback" not in str(e).lower() and "beta" not in str(e).lower():
                raise RuntimeError(f"HTTP_400: {e}") from e
            resp = client.messages.create(**params)
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
        usage = getattr(resp, "usage", None)
        self._last_usage = {
            "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
        } if usage else None

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
                f"{ANTHROPIC_MAX_TOKENS} токена, посред код-ограда "
                f"(stop_reason=max_tokens)."
            )
        return text, (tool_calls or None)

    def _call(self, provider: str, model: str, messages: list[dict],
             tools: list[dict] | None = None,
             extra: dict[str, Any] | None = None) -> tuple[str, list | None]:
        base_url, key_env = _PROVIDERS[provider]
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
            # 429/402/503 (quota gone) or 401/403 (bad or forbidden key): this
            # provider is unusable for a while. Mark it and let the chain move
            # on to the next provider rather than hammering a dead endpoint.
            if any(f"HTTP_{c}" in last for c in _EXHAUST_CODES | {401, 403}):
                _mark_exhausted(kid)
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
            if _is_exhausted(kid):
                continue
            tried_any = True
            try:
                return self._http(base_url, key, model, messages, self.timeout, tools=tools)
            except RuntimeError as e:
                last = str(e)
                last_err = e
                if any(f"HTTP_{c}" in last for c in _EXHAUST_CODES | {401, 403}):
                    _mark_exhausted(kid)
                    print(f"  [Brain] 🔑 {key_env}#{idx} unavailable ({last[:60]}) → next key")
                    continue
                raise
        if not tried_any:
            raise RuntimeError(f"HTTP_429: all configured {key_env}* are cooling down")
        raise last_err or RuntimeError(f"HTTP_502: {key_env} key rotation exhausted")

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
                _save_last_model(loc["provider"], loc["model"])
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

        last_error = "неизвестна грешка"
        local_only = os.environ.get("GENESIS_LOCAL_ONLY") == "1"

        # LOCAL-ONLY: изричен офлайн режим — никога не пипа облака.
        if local_only:
            if not self.local:
                return self._error_result("Error: локален режим — няма наличен локален модел")
            # Бъг, хванат наживо 2026-07-31 (Discord !local_max + реални tools):
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

        if avoid:
            ordered_chain = [c for c in ordered_chain
                             if (c["provider"], c["model"]) != avoid]

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
                        _save_last_model(prov, model)
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
        local_avoided = bool(avoid and self.local
                             and (self.local["provider"], self.local["model"]) == avoid)
        if self.local and not local_avoided:
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
