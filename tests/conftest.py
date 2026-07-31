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


@pytest.fixture(autouse=True)
def _no_real_notifications(monkeypatch):
    """genesis_agent.autonomous_loop.run_autonomous_loop() calls notifier.notify()
    unconditionally on every mission, success or failure — and tests/test_autonomous_loop.py
    calls run_autonomous_loop() (not the private _impl) directly with real goals like
    "kill the background process" / "a goal that always fails". Caught live on
    2026-07-31: every `pytest` run was posting those fake mission results to the
    operator's real, configured Discord webhook. resolve_setting() is the single
    place notify()/send_message() look up GENESIS_DISCORD_WEBHOOK/GENESIS_TELEGRAM_*,
    so forcing it empty here blocks the network call regardless of which module
    calls notify() next, without having to chase every call site individually."""
    monkeypatch.setattr("genesis_agent.notifier.resolve_setting", lambda *a, **kw: "")
