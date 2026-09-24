"""
genesis_agent.model_check — кой от моделите в config.yaml реално отговаря.

    genesis models --check

Защо (design note, 2026-09-23): безплатните каталози гният. За шест седмици
4 от ~17 ръчно проверени модела умряха (410 Gone / 404) и останаха във
веригата — мъртъв №2 при NVIDIA изглеждаше като „NVIDIA пада 40%", а мъртъв
№1 в /maxcoding правеше режима по-бавен, без никой да разбере защо.

Проверката праща по една минимална заявка до всеки модел (паралелно) и
записва резултата. Мъртвите (404/410) Brain прескача сам, докато следваща
проверка не ги види живи — без да се пипа config.yaml. Чатът пуска проверката
на заден план, ако последната е по-стара от седмица.

Зает (429/503) НЕ е мъртъв: това е лимит или претоварване за момента, и
такъв модел остава във веригата.
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import requests
import yaml

from genesis_agent.config import DATA_DIR
from genesis_agent.paths import CONFIG_PATH

log = logging.getLogger("genesis.model_check")

CHECK_PATH = DATA_DIR / "model_check.json"
STALE_DAYS = 7          # чатът пуска нова проверка след толкова дни
DEAD_TRUST_DAYS = 14    # присъда „мъртъв" по-стара от това не се ползва
_TIMEOUT = 30

# status → какво значи за оператора
LABELS = {
    "ok": "✅ работи",
    "busy": "⏳ зает (лимит/претоварен)",
    "dead": "⛔ мъртъв (404/410)",
    "key": "🔑 проблем с ключа",
    "nokey": "·  няма ключ",
    "error": "❓ грешка/таймаут",
    "skip": "·  не се проверява",
}


def configured_models() -> list[tuple[str, str]]:
    """Всички (provider, model) от config.yaml: старт, верига, кодинг, леки."""
    try:
        models = (yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}).get("models", {}) or {}
    except (OSError, yaml.YAMLError):
        return []
    out: list[tuple[str, str]] = []
    if models.get("default_provider") and models.get("default_model_id"):
        out.append((models["default_provider"], models["default_model_id"]))
    for group in ("fallback_models", "coding_models", "light_models"):
        for e in models.get(group, []) or []:
            if e.get("provider") and e.get("model"):
                out.append((e["provider"], e["model"]))
    return list(dict.fromkeys(out))


def _classify(code: int) -> str:
    if code == 200:
        return "ok"
    if code in (404, 410):
        return "dead"
    if code in (429, 503):
        return "busy"
    if code in (401, 402, 403):
        return "key"
    return "error"


def probe(provider: str, model: str, keys: Any) -> dict[str, Any]:
    """Една минимална заявка. `keys` е Brain (за _provider_key) — ключовете
    се четат по същия път като при истинските обаждания."""
    from genesis_agent.brain import _NATIVE_PROVIDERS, _PROVIDERS

    row: dict[str, Any] = {"provider": provider, "model": model}
    if provider not in _PROVIDERS or provider in _NATIVE_PROVIDERS or provider == "vertex":
        return {**row, "status": "skip", "detail": "платен/локален/Vertex"}
    base_url, key_env = _PROVIDERS[provider]
    key = keys._provider_key(key_env) if key_env else None
    if key_env and not key:
        return {**row, "status": "nokey", "detail": key_env}
    t0 = time.time()
    try:
        r = requests.post(f"{base_url}/chat/completions", timeout=_TIMEOUT,
                          headers={"Authorization": f"Bearer {key}"} if key else {},
                          json={"model": model, "max_tokens": 16,
                                "messages": [{"role": "user", "content": "Say OK."}]})
    except requests.RequestException as e:
        return {**row, "status": "error", "detail": type(e).__name__,
                "latency": round(time.time() - t0, 1)}
    return {**row, "status": _classify(r.status_code), "code": r.status_code,
            "latency": round(time.time() - t0, 1),
            "detail": "" if r.status_code == 200 else r.text[:120].replace("\n", " ")}


def run_check(models: list[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    """Проверява паралелно и записва резултата. Никога не хвърля нагоре."""
    from genesis_agent.brain import Brain

    models = models if models is not None else configured_models()
    keys = Brain(use_local=False)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda pm: probe(pm[0], pm[1], keys), models))
    try:
        CHECK_PATH.parent.mkdir(parents=True, exist_ok=True)
        CHECK_PATH.write_text(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(),
                                          "results": results}, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except OSError as e:
        log.warning("model_check: резултатът не се записа: %s", e)
    return results


def _load() -> tuple[float | None, list[dict[str, Any]]]:
    try:
        payload = json.loads(CHECK_PATH.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(str(payload.get("checked_at")))
    except (OSError, ValueError, TypeError):
        return None, []
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - at).total_seconds() / 86400
    return age, list(payload.get("results") or [])


def age_days() -> float | None:
    return _load()[0]


def last_results() -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["provider"], r["model"]): r for r in _load()[1] if "provider" in r}


def dead_models() -> set[tuple[str, str]]:
    """Моделите, които последната проверка видя мъртви — ако е достатъчно
    прясна. Стара присъда не се ползва: моделът може да е върнат."""
    age, results = _load()
    if age is None or age > DEAD_TRUST_DAYS:
        return set()
    return {(r["provider"], r["model"]) for r in results if r.get("status") == "dead"}


def needs_check() -> bool:
    age = age_days()
    return age is None or age > STALE_DAYS


def format_report(results: list[dict[str, Any]]) -> str:
    lines = []
    for r in results:
        lat = f"{r['latency']:>5.1f}s" if r.get("latency") is not None else "      "
        extra = f"  {r['detail'][:60]}" if r.get("status") not in ("ok",) and r.get("detail") else ""
        lines.append(f"  {LABELS.get(r['status'], r['status']):<28} {lat}  "
                     f"{r['provider']:<13} {r['model']}{extra}")
    dead = [r for r in results if r["status"] == "dead"]
    ok = sum(1 for r in results if r["status"] == "ok")
    lines.append(f"\n{ok}/{len(results)} отговарят.")
    if dead:
        lines.append(f"{len(dead)} мъртви — Brain ги прескача сам; махни ги от config.yaml, "
                     "когато имаш време: " + ", ".join(f"{r['provider']}/{r['model']}" for r in dead))
    return "\n".join(lines)
