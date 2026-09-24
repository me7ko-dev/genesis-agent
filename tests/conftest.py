"""Shared fixtures. Every test that touches persisted state (JSON/JSONL/SQLite
files under DATA_DIR) must redirect the module-level path constant to a tmp
file first — these modules bind their path at import time from
genesis_agent.config, so patching config.DATA_DIR afterwards has no effect on
an already-imported module; patch the module's own attribute instead."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_OLLAMA_PORT = 11434


@pytest.fixture(autouse=True)
def _no_real_ollama(monkeypatch):
    """Връзка към локалния ollama по време на тест се отказва веднага.

    118 теста стигат до проба към localhost:11434 (embeddings, наличност на
    модели, лекият мозък). На Linux отказът е мигновен, но на Windows всяко
    свързване към затворен порт чака ~2 s повторни SYN-ове — 165 такива
    опита държаха Windows CI 13 мин срещу 50 s на Linux (измерено 2026-09-24).
    Вдигнат ollama на машината на разработчика пък правеше тестовете зависими
    от него. Отказът тук е същият резултат, който тестовете вече очакват."""
    import socket
    real_connect = socket.socket.connect

    def connect(self, address):
        if isinstance(address, tuple) and len(address) >= 2 and address[1] == _OLLAMA_PORT:
            raise ConnectionRefusedError("tests: ollama is not reachable")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture(autouse=True)
def _no_real_notifications(monkeypatch):
    """genesis_agent.autonomous_loop.run_autonomous_loop() calls notifier.notify()
    unconditionally on every mission, success or failure — and tests/test_autonomous_loop.py
    calls run_autonomous_loop() (not the private _impl) directly with real goals like
    "kill the background process" / "a goal that always fails". Caught live on
    2026-07-31: every `pytest` run was posting those fake mission results to the
    operator's real, configured notification channel. resolve_setting() is the
    single place notify()/send_message() look up GENESIS_TELEGRAM_*,
    so forcing it empty here blocks the network call regardless of which module
    calls notify() next, without having to chase every call site individually."""
    monkeypatch.setattr("genesis_agent.notifier.resolve_setting", lambda *a, **kw: "")


# ── Персистираното състояние никога не е истинското ─────────────────────────
# Горният docstring казва, че всеки тест, който пипа персистирано състояние,
# сам трябва да пренасочи пътя си. Това е уговорка, която нищо не налага — и
# тя не се спазваше. Измерено: `pytest tests/test_autonomous_loop.py
# tests/test_ensemble.py` записа 16 епизода в НАСТОЯЩАТА episodes.db на
# оператора, с цели като "a goal that always fails" и "a goal with no matching
# skill in the library".
#
# Това не е козметика. Тази база захранва:
#   • блока „скорошна активност“ в системния промпт (memory.memory_context);
#   • генерирането на цели (goal_engine.goals_from_real_work) — чийто собствен
#     тест описва как най-честият кандидат е бил точно "a goal that always
#     fails", повторен 85 пъти;
#   • дестилираните уроци (reflection.distill_lessons).
# Тоест тестовият пакет тихо обучаваше агента върху собствените си фикстури.
#
# Същият клас грешка вече е хващан веднъж тук (известията по-горе, 2026-07-31).
# Затова сега се налага от харнеса, а не от помненето на уговорка.
_PERSISTED_STATE = (
    ("genesis_agent.episodic_memory", "DB_PATH", "episodes.db"),
    ("genesis_agent.memory", "DB_PATH", "persistent_memory.db"),
    ("genesis_agent.workspace_memory", "DB_PATH", "workspace_memory.db"),
    ("genesis_agent.conversation_memory", "DB_PATH", "conversation_memory.db"),
    ("genesis_agent.knowledge_graph", "GRAPH_PATH", "knowledge_graph.json"),
    ("genesis_agent.provider_stats", "_STATS_PATH", "provider_stats.json"),
    ("genesis_agent.budget", "LOG_PATH", "budget_log.jsonl"),
    ("genesis_agent.free_models", "CACHE_PATH", "free_models.json"),
    ("genesis_agent.model_check", "CHECK_PATH", "model_check.json"),
    ("genesis_agent.telemetry", "STATUS_FILE", "live_status.json"),
    ("genesis_agent.embeddings", "DB_PATH", "embeddings.db"),
    ("genesis_agent.benchmark", "HISTORY", "benchmark_history.json"),
    ("genesis_agent.web_search", "CACHE_DIR", ".search_cache"),
)


@pytest.fixture(autouse=True)
def _isolated_persisted_state(monkeypatch, tmp_path):
    """Пренасочва всеки модулен път към състояние в tmp директория на теста.

    Типът се запазва: някои модули държат `str`, други `Path`, и подменен
    с грешния тип път чупи модула по начин, който няма нищо общо с теста.
    Модул, който не се внася в тази среда (GTK и подобни), просто се
    пропуска — изолацията му не е нужна, щом не може да се зареди.
    """
    import importlib

    data_dir = tmp_path / "genesis_data"
    data_dir.mkdir(exist_ok=True)
    for module_name, attr, filename in _PERSISTED_STATE:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: S112 — липсващ GTK/незадължителна зависимост:
            continue       # щом модулът не се зарежда, няма какво да се изолира
        current = getattr(module, attr, None)
        if current is None:
            continue
        target = data_dir / filename if filename else data_dir
        monkeypatch.setattr(module, attr,
                            str(target) if isinstance(current, str) else target)

    # episodic_memory създава схемата си при ВНАСЯНЕ, тоест върху стария път.
    # Без този ред всеки тест, който пише епизод, получава "no such table" —
    # и единственият начин да се избегне щеше да е всеки тест сам да помни да
    # извика _init_db(), тоест пак уговорка. Останалите хранилища създават
    # схемата си при всяко отваряне на връзка и нямат нужда от нищо.
    import genesis_agent.episodic_memory as _em
    _em._init_db()
    yield
