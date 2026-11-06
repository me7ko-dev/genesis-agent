"""Дребните отворени от NEXT_STEPS (2026-11-06), без ключове.

1. `code_check.mismatch_line`: „Total: 1,000 expected 1000“ беше разминаване —
   „1,000“ се четеше само като десетична запетая (1.0), а в изхода на програма
   на английски това са хиляди. Еднаквите стойности не са разминаване.
2. `conversation_memory`: репликите се пазеха сурово — `sk_live_…`, `ghp_…`,
   паролата в `postgres://u:pw@` лежаха в conversation_memory.db (графът ги
   маскира от 11-04, тук — не). Никой не ги чете обратно, но файлът е на диска.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from genesis_agent import conversation_memory as cm
from genesis_agent.code_check import mismatch_line

# ─── 1. Хиляди със запетая или точка ─────────────────────────────────────────


@pytest.mark.parametrize("output", [
    "Total: 1,000 expected 1000\nAll tests passed!",
    "sum = 12,500 (expected 12500)",
    "expected 1000, got 1,000",
    "Общо: 1.000 очаквано 1000",                        # точка за хиляди (BG/DE)
    "expected 2,500,000, got 2500000",
    "result: 0,5 expected 0.5",                          # десетична запетая — както досега
])
def test_the_same_number_with_a_thousands_separator_is_not_a_mismatch(output) -> None:
    assert mismatch_line(output) == ""


@pytest.mark.parametrize("output", [
    "Total: 1,000 expected 2000\nAll tests passed!",
    "expected 1000, got 1,001",
    "expected 2,500,000, got 2500001",
    "result: 0,5 expected 0.6",
    "Total: 1,00 expected 100",                          # не са групи по 3 — 1.0 ≠ 100
])
def test_a_real_difference_is_still_a_mismatch(output) -> None:
    assert mismatch_line(output)


# ─── 2. Тайни в паметта на разговора ─────────────────────────────────────────

_SECRETS = ("sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
            "ghp_" + "16C7e42F292c6912E7710c838347Ae178B4a",
            "hunter2pass")


def _raw_db() -> str:
    with sqlite3.connect(cm.DB_PATH) as conn:
        return "\n".join(r[0] for r in conn.execute("SELECT content FROM conversations"))


def test_secrets_from_the_chat_are_not_stored_raw() -> None:
    cm.add_message("user", f"ключът ми е {_SECRETS[0]} и токенът {_SECRETS[1]}")
    cm.add_message("user", f"базата е postgres://admin:{_SECRETS[2]}@db.local:5432/app")
    stored = _raw_db()
    for secret in _SECRETS:
        assert secret not in stored
    assert "ключът ми е" in stored and "db.local:5432/app" in stored


def test_a_summary_of_old_raw_rows_does_not_carry_secrets() -> None:
    # Стара база отпреди поправката: суровите редове влизат в резюмето.
    Path(cm.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cm.DB_PATH) as conn:
        conn.executescript(cm._SCHEMA)
        conn.executemany("INSERT INTO conversations (role, content) VALUES (?, ?)",
                         [("user", f"искане №{i} с {_SECRETS[0]}") for i in range(60)])
    cm.summarize_old_context()
    stored = _raw_db()
    assert "[Context summary]" in stored
    summary = next(line for line in stored.splitlines() if line.startswith("[Context summary]"))
    assert _SECRETS[0] not in summary
    assert "искане №0" in summary
