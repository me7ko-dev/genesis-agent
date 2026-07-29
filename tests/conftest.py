"""Shared fixtures. Every test that touches persisted state (JSON/JSONL/SQLite
files under DATA_DIR) must redirect the module-level path constant to a tmp
file first — these modules bind their path at import time from
genesis_agent.config, so patching config.DATA_DIR afterwards has no effect on
an already-imported module; patch the module's own attribute instead."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
