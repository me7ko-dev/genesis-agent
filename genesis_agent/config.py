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

# Optional: set GENESIS_OPERATOR=<your-name> for an audit trail (CLI --operator).
# GENESIS_STRICT_AUTHORITY=1 requires sovereign operator to start the autonomous loop.
# Red Zone manual approval contract: GENESIS_RED_ZONE_SECRET (host) + GENESIS_RED_ZONE_TOKEN (process) must match.

# Storage safety: total project tree size
STORAGE_THRESHOLD_BYTES: int = int(
    os.environ.get("GENESIS_STORAGE_THRESHOLD_GB", "100")
) * (1024**3)
