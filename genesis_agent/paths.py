"""
genesis_agent.paths — where Genesis Agent looks for configuration.

One place, so no module has to guess. Nothing here is tied to a particular
machine or user: every path is derived from the installed package, the user's
home directory, or an environment variable they set themselves.

Precedence when resolving a key (first hit wins):

    1. a real environment variable            (export HF_TOKEN=...)
    2. ./.env next to the project             (per-checkout override)
    3. ~/.genesis/.env                        (the normal place)
    4. config.yaml                            (non-secret defaults only)

Secrets belong in 2 or 3, never in 4 — `config.yaml` is committed, the .env
files are gitignored.
"""

from __future__ import annotations

import os
from pathlib import Path

# The installed package: .../genesis-agent/genesis_agent/ → .../genesis-agent/
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# User configuration lives here. Override with GENESIS_HOME if you keep
# config elsewhere (e.g. an encrypted volume).
GENESIS_HOME: Path = Path(os.environ.get("GENESIS_HOME", Path.home() / ".genesis"))

ENV_FILE: Path = GENESIS_HOME / ".env"
CONFIG_PATH: Path = PROJECT_ROOT / "config.yaml"

# Searched in order. The project-local .env wins over the home one, so a
# checkout can be pinned to different keys without touching global config.
ENV_FILES: tuple[str, ...] = (
    str(PROJECT_ROOT / ".env"),
    str(ENV_FILE),
)


def workspace_dir() -> Path:
    """
    Where the agent is allowed to work by default. Deliberately NOT the user's
    whole home directory — an agent that starts out pointed at everything is
    one bad command away from a bad day.
    """
    return Path(os.environ.get("GENESIS_WORKSPACE", PROJECT_ROOT))


def history_dir() -> Path:
    """Chat history. XDG-correct, so it does not clutter the repo."""
    default = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    ) / "genesis-agent" / "history"
    return Path(os.environ.get("GENESIS_HISTORY_DIR", default))


def read_env_files(key: str) -> str | None:
    """
    Look up `key` in the .env files. Returns None if absent.

    Real environment variables are NOT consulted here — callers check
    `os.environ` first so an explicit `export` always wins.
    """
    for envf in ENV_FILES:
        p = Path(envf)
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                if k.startswith("export "):
                    k = k[len("export "):].strip()
                if k == key:
                    return v.strip().strip('"').strip("'")
        except OSError:
            continue
    return None


def get_secret(key: str, default: str | None = None) -> str | None:
    """Full precedence chain for one secret."""
    val = os.environ.get(key)
    if val:
        return val
    val = read_env_files(key)
    if val:
        return val
    return default


def ensure_genesis_home() -> Path:
    """Create ~/.genesis with owner-only permissions (it holds API keys)."""
    GENESIS_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    return GENESIS_HOME
