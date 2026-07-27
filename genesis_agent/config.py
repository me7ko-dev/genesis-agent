"""Paths and limits for Genesis Agent."""

from __future__ import annotations

import os
import threading
from pathlib import Path

stop_event = threading.Event()


# Repository root: .../genesis-0/
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

SKILLS_DIR: Path = Path(os.environ.get("GENESIS_SKILLS_DIR", PROJECT_ROOT / "skills"))
SANDBOX_DIR: Path = Path(os.environ.get("GENESIS_SANDBOX_DIR", PROJECT_ROOT / ".sandbox_run"))
LOGS_DIR: Path = PROJECT_ROOT / "logs"

# ENGINE / 100% STANDALONE MODE
# MODEL_PATH беше хардкоднат Windows път; сега е env-конфигурируем и по
# подразбиране търси в ~/.lmstudio/models (Linux/LM Studio разположение).
ENGINE_EXE: Path = Path(os.environ.get("GENESIS_ENGINE_EXE", PROJECT_ROOT / "engine" / "llama-server"))
MODEL_PATH: Path = Path(os.environ.get(
    "GENESIS_MODEL_PATH",
    Path.home() / ".lmstudio" / "models" / "nvidia-agentic-coder-4b.gguf",
))
ENGINE_PORT = 12345

# OpenAI-compatible local LLM (LM Studio / Ollama)
LLM_BASE_URL: str = os.environ.get("GENESIS_LLM_BASE_URL", f"http://127.0.0.1:{ENGINE_PORT}/v1")
LLM_API_KEY: str = os.environ.get("GENESIS_LLM_API_KEY", "genesis")
LLM_MODEL: str = os.environ.get("GENESIS_LLM_MODEL", "nvidia-agentic")

# За локални малки модели (DeepSeek 8B, Qwen 7B и т.н.) намали ретрите!
# Малък модел = бърз провал, не 300 рунда по 130 секунди
MAX_LLM_RETRIES: int = int(os.environ.get("GENESIS_MAX_RETRIES", "8"))
EXEC_TIMEOUT_SEC: int = int(os.environ.get("GENESIS_EXEC_TIMEOUT", "120"))

# Детекция на режим: 'local' (малък модел) или 'cloud' (GPT/Groq/NVIDIA)
GENESIS_MODE: str = os.environ.get("GENESIS_MODE", "local")  # 'local' | 'cloud'

# Optional: set GENESIS_OPERATOR=<your-name> for an audit trail (CLI --operator).
# GENESIS_STRICT_AUTHORITY=1 requires sovereign operator to start the autonomous loop.
# Red Zone manual approval contract: GENESIS_RED_ZONE_SECRET (host) + GENESIS_RED_ZONE_TOKEN (process) must match.

# Storage safety: total project tree size
STORAGE_THRESHOLD_BYTES: int = int(
    os.environ.get("GENESIS_STORAGE_THRESHOLD_GB", "100")
) * (1024**3)
