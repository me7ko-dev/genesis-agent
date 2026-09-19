"""Paths and limits for Genesis Agent."""

from __future__ import annotations

import os
import threading
from pathlib import Path

stop_event = threading.Event()


# The directory containing the genesis_agent package. For a git checkout
# this is the repo root — writable, fine to use directly. For a `pip install`
# it is site-packages, which a mission has no business writing into (often
# not even permitted, and wiped on the next reinstall/upgrade).
PACKAGE_DIR: Path = Path(__file__).resolve().parent
PROJECT_ROOT: Path = PACKAGE_DIR.parent
_INSTALLED = PROJECT_ROOT.name in ("site-packages", "dist-packages")


def _default_skills_dir() -> Path:
    shipped = PACKAGE_DIR / "skills"
    if not _INSTALLED:
        return shipped
    # Installed copy: skills a mission writes go to ~/.genesis/skills instead.
    # Seeded once from the shipped starter set, so `genesis skills` still
    # shows them immediately on a fresh install — this only copies, it never
    # writes back into site-packages.
    from genesis_agent.paths import GENESIS_HOME
    user_dir = GENESIS_HOME / "skills"
    if not user_dir.exists():
        if shipped.exists():
            import shutil
            shutil.copytree(shipped, user_dir)
        else:
            user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


SKILLS_DIR: Path = Path(os.environ.get("GENESIS_SKILLS_DIR") or _default_skills_dir())
SANDBOX_DIR: Path = Path(os.environ.get("GENESIS_SANDBOX_DIR",
    str((Path.home() / ".genesis" / "sandbox_run") if _INSTALLED else PROJECT_ROOT / ".sandbox_run")))
LOGS_DIR: Path = (Path.home() / ".genesis" / "logs") if _INSTALLED else PROJECT_ROOT / "logs"

# Where the SQLite state lives: workspace memory, episodic memory, conversation
# history, the embeddings cache. These modules historically wrote next to
# their own __file__ — fine in a checkout (that's a writable repo directory),
# but for an installed copy __file__ is under site-packages, and a database
# nobody can write survives exactly until the next `pip install --upgrade`
# deletes it anyway. Same path as always in checkout mode: zero behavior
# change for anyone running from a git clone.
DATA_DIR: Path = (Path.home() / ".genesis" / "data") if _INSTALLED else (PROJECT_ROOT / "genesis_agent")
if _INSTALLED:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

# Removed here: ENGINE_EXE / MODEL_PATH / ENGINE_PORT / LLM_BASE_URL /
# LLM_API_KEY / LLM_MODEL / GENESIS_MODE. They described a llama-server and
# LM Studio setup that no longer exists — nothing in the codebase read any of
# them, and they pointed at a specific .gguf on a specific machine. The local
# model is configured with GENESIS_LOCAL_MODEL and talked to over Ollama's
# OpenAI-compatible endpoint; see genesis_agent/brain.py.

# За локални малки модели (DeepSeek 8B, Qwen 7B и т.н.) намали ретрите!
# Малък модел = бърз провал, не 300 рунда по 130 секунди
MAX_LLM_RETRIES: int = int(os.environ.get("GENESIS_MAX_RETRIES", "8"))
EXEC_TIMEOUT_SEC: int = int(os.environ.get("GENESIS_EXEC_TIMEOUT", "120"))

# ─── Бюджет на контекста: щедро към свежото, безмилостно към старото ───────
#
# Двата лимита не се борят — разделят си работата по възраст на резултата:
#
# TOOL_RESULT_MAX_CHARS е предпазителят на ВХОДА. Пази историята (и файла на
# сесията) от абсурд — `cat` на бинарен файл, лог от 5 MB — но нарочно е
# щедър: точно този резултат е онова, върху което моделът работи В МОМЕНТА,
# и да го отрежеш на две е да го накараш да гадае или да пусне командата пак,
# което струва повече от символите, които си спестил.
#
# STALE_TOOL_RESULT_MAX_CHARS е истинската икономия и се прилага при ИЗХОДА
# (genesis_agent.budget.budget_history, извикан от Brain.complete). Един tool
# резултат се праща наново на ВСЕКИ следващ рунд, докато не изпадне от
# прозореца — 10 рунда по-късно същите 40 000 символа са платени десет пъти,
# а вече не вършат работа: моделът е реагирал на тях преди осем хода. Затова
# всичко освен последните FRESH_TOOL_RESULTS резултата се свива агресивно.
# Нетно: моделът вижда ПОВЕЧЕ от това, което му трябва сега, и много по-малко
# от това, което вече е изиграло ролята си.
#
# 0 изключва съответния таван (старото поведение).
TOOL_RESULT_MAX_CHARS: int = int(os.environ.get("GENESIS_TOOL_RESULT_MAX_CHARS", "40000"))
STALE_TOOL_RESULT_MAX_CHARS: int = int(
    os.environ.get("GENESIS_STALE_TOOL_RESULT_MAX_CHARS", "2000"))
FRESH_TOOL_RESULTS: int = int(os.environ.get("GENESIS_FRESH_TOOL_RESULTS", "2"))

# Колко tool рунда има правото да направи агентът за ЕДНО съобщение, преди
# цикълът да спре и да върне контрола. Това е предпазителят срещу зацикляне,
# НЕ бюджет за работа — а на 8 беше точно бюджет: реална многостъпкова задача
# (диагностицирай → поправи → пусни тестовете → поправи пак → потвърди) тихо
# се удряше в тавана по средата и потребителят получаваше половин работа с
# обяснение — точно поведението, което целият tool-loop беше добавен да спре
# (виж коментара при цикъла в genesis_terminal_agent.py). Цената на рунд вече
# е ограничена отделно, през TOOL_RESULT_MAX_CHARS.
TOOL_ROUND_CAP: int = int(os.environ.get("GENESIS_TOOL_ROUNDS", "25"))

# Optional: set GENESIS_OPERATOR=<your-name> for an audit trail (CLI --operator).
# GENESIS_STRICT_AUTHORITY=1 requires sovereign operator to start the autonomous loop.
# Red Zone manual approval contract: GENESIS_RED_ZONE_SECRET (host) + GENESIS_RED_ZONE_TOKEN (process) must match.

# Storage safety: total project tree size
STORAGE_THRESHOLD_BYTES: int = int(
    os.environ.get("GENESIS_STORAGE_THRESHOLD_GB", "100")
) * (1024**3)
