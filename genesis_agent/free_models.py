"""
genesis_agent.free_models — открива БЕЗПЛАТНИТЕ модели на доставчика сам,
вместо списъкът да се поддържа на ръка в config.yaml.

Защо не просто още записи в config.yaml: защото този списък не стои мирен.
Само за OpenRouter броят безплатни модели падна 29 (юни 2026) → 25 (юли) →
23 (септември) — а git историята на config.yaml показва същото от другата
страна: `mistralai/mixtral-8x7b-instruct-v0.1` отпадна с HTTP 410 Gone,
`nemotron-4-340b` и `llama-3.1-nemotron-ultra-253b` върнаха 404 на реално
обаждане, макар да фигурират в /models. Ръчно поддържан списък остарява за
седмици и се проваля мълчаливо — в момента, в който потребителят има нужда
от резерва.

Затова: config.yaml остава РЪЧНО КУРИРАНАТА основа (всеки запис там е
проверен наживо с реален ключ — виж коментара му), а този модул добавя
автоматично откритите безплатни ПОД нея, като резерва. Ръчно проверените
водят винаги; откритите се включват само след като веригата ги изчерпи.

Обхват (важно, за да не заблуждава): "безплатен модел" е понятие, което
реално съществува само при доставчици, които маркират конкретни модели като
безплатни — OpenRouter с `:free` суфикс и нулева цена. При NVIDIA NIM, Groq,
Cerebras, SambaNova и Ollama Cloud безплатното е КВОТА върху акаунта, не
свойство на модела: там всеки модел е "безплатен", докато квотата стигне.
За тях този модул нарочно не гадае — те се водят от config.yaml.

Никога не хвърля и никога не блокира: липса на мрежа/ключ/каталог просто
значи "без допълнителни модели", не счупен старт.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from genesis_agent.config import DATA_DIR

log = logging.getLogger("genesis.free_models")

CACHE_PATH = DATA_DIR / "free_models.json"
_TIMEOUT = 15
_CATALOG_URL = "https://openrouter.ai/api/v1/models"

# Под този размер моделът не си струва мястото във веригата — проектът вече
# е взел това решение веднъж (config.yaml: "МИНИМУМ ~20-30B за ВСЕКИ модел",
# по-малките бяха премахнати ИЗЦЯЛО, не просто деприоритизирани, защото при
# верижен fallback дори "резерва" реално се вика).
_MIN_SIZE_B = 20.0

# "nemotron-3-ultra-550b-a55b" → 550 (не 55: за MoE се брои ОБЩИЯТ брой
# параметри, същата конвенция като config.yaml's size_b).
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)


def _parse_size_b(model_id: str) -> float:
    """Най-големият брой-с-B в името. 0.0 ако името не издава размер —
    извикващият решава какво да прави с неизвестното."""
    hits = [float(m) for m in _SIZE_RE.findall(model_id.replace("_", "-"))]
    return max(hits) if hits else 0.0


def _fetch_catalog(url: str = _CATALOG_URL) -> list[dict[str, Any]]:
    """Суровият каталог на OpenRouter. Публичен е — не иска ключ."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "genesis-agent"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        log.debug("free_models: каталогът е недостъпен (%s)", e)
        return []
    data = payload.get("data")
    return data if isinstance(data, list) else []


def _is_free(entry: dict[str, Any]) -> bool:
    """Безплатен = всяка обявена цена е нула.

    Проверява ЦЕНАТА, не `:free` суфикса в името. Суфиксът е конвенция и я
    има, но цената е това, което реално ще бъде таксувано — а някои записи
    носят нулев prompt и ненулев completion. Тук се пита "ще ми вземат ли
    пари", затова се гледат всички ценови полета.
    """
    pricing = entry.get("pricing")
    if not isinstance(pricing, dict):
        return False
    values = [pricing.get(k) for k in ("prompt", "completion", "request")]
    seen_any = False
    for v in values:
        if v is None:
            continue
        seen_any = True
        try:
            if float(v) != 0.0:
                return False
        except (TypeError, ValueError):
            return False
    return seen_any


def discover(*, min_size_b: float = _MIN_SIZE_B) -> list[dict[str, Any]]:
    """Безплатните модели, годни за веригата — най-големите първи.

    `supports_tools` идва от собственото `supported_parameters` поле на
    каталога, не от предположение: проектът вече е плащал за такова гадаене
    (config.yaml пази бележка, че Qwen2.5-Coder-32B и NVIDIA Mixtral приемат
    параметъра, но връщат 400).
    """
    out: list[dict[str, Any]] = []
    for entry in _fetch_catalog():
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not _is_free(entry):
            continue
        size_b = _parse_size_b(model_id)
        if size_b and size_b < min_size_b:
            continue
        params = entry.get("supported_parameters")
        out.append({
            "provider": "openrouter",
            "model": model_id,
            "size_b": size_b,
            "supports_tools": isinstance(params, list) and "tools" in params,
            "context_length": entry.get("context_length") or 0,
        })
    out.sort(key=lambda m: (m["size_b"], m["context_length"]), reverse=True)
    return out


def refresh() -> tuple[int, str]:
    """Обновява кеша от живия каталог. Връща (брой, съобщение).

    При провал НЕ пипа кеша: стар списък е по-добър от никакъв, защото е
    резервата, която се ползва точно когато нещо друго вече е отказало.
    """
    found = discover()
    if not found:
        return 0, "Каталогът е недостъпен (мрежа/политика) — кешът остава както е."
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "models": found,
    }
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except OSError as e:
        return len(found), f"Намерени {len(found)}, но записът се провали: {e}"
    return len(found), f"Записани {len(found)} безплатни модела в {CACHE_PATH}"


def cached() -> list[dict[str, Any]]:
    """Кешираните безплатни модели. Празен списък, ако няма кеш или е повреден.

    GENESIS_NO_FREE_MODELS=1 го изключва изцяло — за случая, в който
    операторът иска само ръчно проверената верига от config.yaml.
    """
    if os.environ.get("GENESIS_NO_FREE_MODELS") == "1":
        return []
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    return [m for m in models if isinstance(m, dict) and m.get("provider") and m.get("model")]


def cache_age_days() -> float | None:
    """На колко дни е кешът, или None ако го няма/е нечетим — за да може
    извикващият да подскаже „обнови го", вместо да вярва сляпо на стар списък."""
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(str(payload.get("fetched_at")))
    except (OSError, ValueError, TypeError):
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - fetched).total_seconds() / 86400
