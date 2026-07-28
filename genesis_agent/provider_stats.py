"""
genesis_agent.provider_stats — data-driven ред на веригата (design note, 2026-07-25).

Пази rolling latency+success статистика ПО ДОСТАВЧИК (не по модел — доставчик
е достатъчна детайлност за "този цял акаунт в момента лош ли е"). Само
деприоритизираме, В РАМКИТЕ на текущото извикване, доставчик с ПОТВЪРДЕН (≥5
извадки) лош success rate (<50%) — минава последен вместо пръв, вместо
слепешката да му хабим първия опит всеки път, докато е "болен".
"""
from __future__ import annotations

import json
import time
from threading import Lock

from genesis_agent.config import DATA_DIR

_STATS_PATH = DATA_DIR / "provider_stats.json"
_MAX_SAMPLES = 30          # rolling прозорец на извикване по доставчик
_MIN_SAMPLES_TO_JUDGE = 5  # под това не съдим — недостатъчно данни
_BAD_SUCCESS_RATE = 0.5    # под 50% успех (при ≥5 извадки) → деприоритизирай

_lock = Lock()


def _load() -> dict:
    try:
        return json.loads(_STATS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    try:
        _STATS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass  # статистиката е "nice to have" — никога не бива да чупи мисия


def record_call(provider: str, latency_s: float, success: bool) -> None:
    """Записва резултат от едно API извикване (успех/провал + латентност)."""
    with _lock:
        data = _load()
        entry = data.setdefault(provider, {"samples": []})
        entry["samples"].append({"t": round(time.time()), "ok": success, "lat": round(latency_s, 2)})
        entry["samples"] = entry["samples"][-_MAX_SAMPLES:]
        _save(data)


def success_rate(provider: str) -> float | None:
    """None = недостатъчно данни (<5 извадки) — не съди все още."""
    samples = _load().get(provider, {}).get("samples", [])
    if len(samples) < _MIN_SAMPLES_TO_JUDGE:
        return None
    return sum(1 for s in samples if s["ok"]) / len(samples)


def avg_latency(provider: str) -> float | None:
    samples = [s for s in _load().get(provider, {}).get("samples", []) if s["ok"]]
    if not samples:
        return None
    return sum(s["lat"] for s in samples) / len(samples)


def deprioritize_flaky(chain: list[dict]) -> list[dict]:
    """
    Стабилно пренарежда веригата: записи от доставчик с ПОТВЪРДЕНО лош rolling
    success rate минават в края на СПИСЪКА (не се трият, не се маркират
    изчерпани — просто не пречат на здравите доставчици да се пробват първи).
    Относителният ред вътре във всяка група (здрави/болни) се пази.

    Викащият (brain.py) НЕ бива да записва резултата обратно в self.chain —
    това е преценка за момента, не нова конфигурация.

    Чете статистиката ВЕДНЪЖ: през success_rate() всяка проверка беше отделно
    четене+парсване на JSON файла, т.е. 2×N дискови четения преди всяко
    обръщение към модел.
    """
    data = _load()

    def _is_flaky(provider: str) -> bool:
        samples = data.get(provider, {}).get("samples", [])
        if len(samples) < _MIN_SAMPLES_TO_JUDGE:
            return False  # недостатъчно данни — не съдим
        return sum(1 for s in samples if s["ok"]) / len(samples) < _BAD_SUCCESS_RATE

    sick = {c["provider"] for c in chain if _is_flaky(c["provider"])}
    healthy = [c for c in chain if c["provider"] not in sick]
    flaky = [c for c in chain if c["provider"] in sick]
    return healthy + flaky


def report() -> str:
    """Четим отчет за !stats/CLI — success rate + латентност по доставчик."""
    data = _load()
    if not data:
        return "Няма данни все още."
    lines = []
    for prov, entry in sorted(data.items()):
        n = len(entry.get("samples", []))
        sr = success_rate(prov)
        lat = avg_latency(prov)
        sr_txt = f"{sr*100:.0f}%" if sr is not None else "—"
        lat_txt = f"{lat:.2f}s" if lat is not None else "—"
        lines.append(f"  {prov:14} success={sr_txt:>5} avg_latency={lat_txt:>7} (n={n})")
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
