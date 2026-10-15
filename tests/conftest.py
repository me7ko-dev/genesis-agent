"""Shared fixtures. Every test that touches persisted state (JSON/JSONL/SQLite
files under DATA_DIR) must redirect the module-level path constant to a tmp
file first — these modules bind their path at import time from
genesis_agent.config, so patching config.DATA_DIR afterwards has no effect on
an already-imported module; patch the module's own attribute instead."""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Папката на пакета (2026-10-15). В checkout DATA_DIR е genesis_agent/ —
# там е истинската памет на оператора (episodes.db, persistent_memory.db).
# memory и episodic_memory създават базите си при ВНАСЯНЕ, преди която и да е
# фикстура да е пренасочила пътя: пълният пакет оставяше двете бази (и -wal/
# -shm) в genesis_agent/ на чист checkout. Снимката е преди всякакво внасяне;
# _package_dir_untouched по-долу проверява след всеки тест.
_PACKAGE_DIR = ROOT / "genesis_agent"


def _package_entries() -> set[str]:
    found: set[str] = set()
    for base in (_PACKAGE_DIR, _PACKAGE_DIR / "skills"):
        try:
            names = os.listdir(base)
        except OSError:
            continue
        rel = base.relative_to(_PACKAGE_DIR)
        found.update(str(rel / n) for n in names if n != "__pycache__")
    return found


_PACKAGE_BEFORE = _package_entries()

# Затова DATA_DIR на целия пакет — и на подпроцесите, които наследяват средата —
# е временна папка, зададена ПРЕДИ genesis_agent.config да се внесе. Операторски
# GENESIS_DATA_DIR/GENESIS_MEMORY_DIR също не стига до тестовете: и той е
# истинско състояние. Фикстурите по-долу пак пренасочват всеки път поотделно.
_SESSION_DATA = Path(tempfile.mkdtemp(prefix="genesis-tests-"))
atexit.register(shutil.rmtree, _SESSION_DATA, ignore_errors=True)
os.environ["GENESIS_DATA_DIR"] = str(_SESSION_DATA / "data")
os.environ.pop("GENESIS_MEMORY_DIR", None)


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
    ("genesis_agent.embeddings", "SKILLS_INDEX", "skills.json"),
    ("genesis_agent.benchmark", "HISTORY", "benchmark_history.json"),
    ("genesis_agent.web_search", "CACHE_DIR", ".search_cache"),
    # Ключът за подписване на умения (e2e 2026-10-11): пълният пакет създаде
    # ~/.genesis/private_key.pem — пътят се връзва при внасяне, а тест, който
    # задава само GENESIS_KEY_DIR, подписваше с ключа на оператора.
    ("genesis_agent.cryptography_utils", "KEY_DIR", "keys"),
    ("genesis_agent.cryptography_utils", "PRIVATE_KEY_PATH", "keys/private_key.pem"),
    ("genesis_agent.cryptography_utils", "PUBLIC_KEY_PATH", "keys/public_key.pem"),
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


@pytest.fixture(autouse=True)
def _isolated_operator_settings(monkeypatch, tmp_path):
    """~/.genesis на оператора (GENESIS.md, hooks.json, commands/) не стига до
    тестовете: истински hooks.json би пускал командите на оператора при всеки
    WRITE_FILE в тестовете. И режимът план / историята за /undo са на модула —
    всеки тест започва без тях."""
    home = tmp_path / "genesis_home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr("genesis_agent.paths.GENESIS_HOME", home)
    # .env файловете с ключовете (одит 2026-10-11): ENV_FILES се смята при
    # внасяне от истинската ~/.genesis, така че Brain() и vertex_auth в тест
    # четяха ключовете и проектите на оператора. brain го внася по име.
    env_files = (str(tmp_path / "project.env"), str(home / ".env"))
    monkeypatch.setattr("genesis_agent.paths.ENV_FILES", env_files)
    monkeypatch.setattr("genesis_agent.brain.ENV_FILES", env_files)
    # Отказаните като твърде големи размери са на модула (2026-10-11) — тест с
    # HTTP_413 не бива да скрива модела от следващите тестове.
    monkeypatch.setattr("genesis_agent.brain._TOO_LARGE", {})
    # debug.log (2026-10-15): операторски export GENESIS_DEBUG=1 не стига до
    # тестовете, а handler-ът от тест с cli.main се маха след него — иначе
    # следващите тестове пишат във файла на предишния tmp home.
    monkeypatch.delenv("GENESIS_DEBUG", raising=False)
    # Историята на чата: тестове с run_turn пишеха session_*.json и
    # by_workspace.json в истинската ~/.local/share/genesis-agent/history —
    # `genesis -c` после продължаваше тестов разговор (одит 2026-10-09).
    import sys as _sys
    gta = _sys.modules.get("genesis_terminal_agent")
    if gta is not None:
        history = tmp_path / "history"
        history.mkdir(exist_ok=True)
        monkeypatch.setattr(gta, "HISTORY_DIR", history)
        monkeypatch.setattr(gta, "_SESSION_FILE", None)
    # Глобалната политика на sandbox-а и куката преди изпълнение: `rs.serve`
    # в тест оставяше „интерактивна, пита телефона“ и следващите тестове
    # получаваха SANDBOX DECLINED (2026-10-09).
    from genesis_agent import sandbox as _sandbox
    monkeypatch.setattr(_sandbox, "_POLICY", _sandbox._POLICY)
    monkeypatch.setattr(_sandbox, "before_exec", None)
    # Домашната папка на уменията е в системната временна папка (2026-10-09).
    from genesis_agent import skill_loader
    monkeypatch.setattr(skill_loader, "SKILL_HOME_ROOT", tmp_path / "skill_tmp")
    from genesis_agent import (
        agents,
        edit_history,
        permissions,
        plan_mode,
        todos,
        vision,
    )
    # Картинките от VIEW_IMAGE и кешът „вижда ли веригата“ са на модула
    # (2026-10-15); байтовете на картинките в историята са под HISTORY_DIR горе.
    vision.take_pending()
    monkeypatch.setattr(vision, "_sees_cache", None)
    monkeypatch.setattr(vision, "_cfg_cache", None)
    plan_mode.set_active(False)
    edit_history.clear()
    todos.clear()
    # Одобренията „за тази сесия“ са на модула (2026-10-15): одобрено в един
    # тест не бива да се пуска без въпрос в следващия. Постоянните правила са
    # в GENESIS_HOME отгоре.
    permissions.clear_session()
    monkeypatch.setattr(permissions, "workspace", None)
    yield
    plan_mode.set_active(False)
    edit_history.clear()
    todos.clear()
    permissions.clear_session()
    agents.progress = None
    from genesis_agent import debug_log
    debug_log.uninstall()
    from genesis_agent import mcp_client
    mcp_client.stop_all()


class _RealDeviceInTest(BaseException):
    """Тест стигна до истински микрофон или високоговорител."""


@pytest.fixture(autouse=True)
def _no_real_audio(monkeypatch):
    """Микрофонът и високоговорителите на машината не се пипат (2026-10-15):
    всяко истинско записване минава през voice._capture_sounddevice или
    voice._capture_arecord, всяко пускане — през voice._run_player. Тест, който ги стигне без фалшив заместител, пада
    веднага, вместо да запише стаята или да проговори на лаптопа. Средата на
    оператора (GENESIS_STT_*/GENESIS_TTS_*/GENESIS_VOICE_*) не стига до
    тестовете — те задават сами каквото искат."""
    from genesis_agent import voice

    def refuse(*_a, **_k):
        # BaseException: `except Exception` в _capture не бива да го превърне
        # в тиха резерва (arecord) и тестът да мине.
        raise _RealDeviceInTest("tests: истински микрофон/звук — подмени го с фалшив")

    # Само тези пипат устройство; изборът преди тях (_capture, choose_player)
    # остава истински и се проверява.
    monkeypatch.setattr(voice, "_capture_sounddevice", refuse)
    monkeypatch.setattr(voice, "_capture_arecord", refuse)
    monkeypatch.setattr(voice, "_run_player", refuse)
    monkeypatch.setattr(voice, "_LOCAL_MODELS", {})
    for name in list(os.environ):
        if name.startswith(("GENESIS_STT_", "GENESIS_TTS_", "GENESIS_VOICE_")):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_real_stripe(monkeypatch):
    """Stripe ключове от shell-а на разработчика не включват истинско плащане
    в тест (2026-10-11): cloud/web/server.main чете STRIPE_* и
    GENESIS_PUBLIC_URL от средата. Тестовете с Stripe ги задават сами, а
    обръщенията към Stripe са винаги фалшив opener."""
    for name in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "GENESIS_PUBLIC_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _elide_off_like_the_default(monkeypatch):
    """B.12 (2026-10-11): config.py казва на оператора да пусне A/B с
    GENESIS_ELIDE_OLD_WRITES=1; с изнесена такава променлива
    test_bench_history (базата „off“) и test_budget (рундовете K) падаха при
    верен код — стойностите се четат при внасяне на config. Тестовете
    започват изключено (подразбирането е включено от 2026-10-12 — така
    историите в старите тестове остават байт по байт); който иска друго,
    го задава сам."""
    monkeypatch.delenv("GENESIS_ELIDE_OLD_WRITES", raising=False)
    monkeypatch.delenv("GENESIS_ELIDE_WRITE_ROUNDS", raising=False)
    from genesis_agent import budget, config
    for module in (budget, config):
        monkeypatch.setattr(module, "ELIDE_OLD_WRITES", 0)
        monkeypatch.setattr(module, "ELIDE_WRITE_ROUNDS", 6)


@pytest.fixture(autouse=True)
def _acceptance_off_like_before(monkeypatch):
    """Б.4 (2026-10-12): приемните тестове са включени по подразбиране за
    безплатен модел, а терминалът в тестовете стои на groq — всеки ход с .py
    би направил още една заявка към фалшивия модел и старите тестове щяха да
    видят чужд отговор. Започват изключени; тестовете за тях задават сами."""
    monkeypatch.setenv("GENESIS_ACCEPTANCE", "0")


class FakeDocker:
    """Записва `docker` командите вместо да ги пуска. `answers` — отговор по
    префикс на аргументите: {("network", "create"): (1, "", "pool exhausted")}."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.answers: dict[tuple[str, ...], tuple[int, str, str]] = {}

    def __call__(self, *args: str, timeout: float = 60):
        import subprocess
        self.calls.append(list(args))
        code, out, err = next((v for k, v in self.answers.items() if tuple(args[:len(k)]) == k),
                              (0, "", ""))
        return subprocess.CompletedProcess(["docker", *args], code, out, err)


@pytest.fixture(autouse=True)
def fake_docker(monkeypatch):
    """Деплой 2026-10-11: launch.run_task вече създава мрежа за всяка задача, а
    уеб сървърът при старт убива останали контейнери (`docker rm -f`). На
    машина с истински Docker (като тази) тестът би спрял задачите на
    оператора — затова всеки `docker` през launch._docker се записва тук."""
    from cloud.runner import launch
    fake = FakeDocker()
    monkeypatch.setattr(launch, "_docker", fake)
    return fake


@pytest.fixture(autouse=True)
def _package_dir_untouched():
    """Нито един тест не оставя файл в genesis_agent/ (виж _PACKAGE_DIR)."""
    yield
    new = _package_entries() - _PACKAGE_BEFORE
    # Пада само тестът, който ги е оставил — следващите не наследяват грешката.
    _PACKAGE_BEFORE.update(new)
    assert not new, f"тестът остави файлове в genesis_agent/: {sorted(new)}"
