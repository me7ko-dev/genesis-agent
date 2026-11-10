"""Одит 2026-10-31 (шести кръг): паметта на разговора
(genesis_agent/conversation_memory.py) — какво остава в резюмето при
компресия (и при всяка следваща) и какво става, когато базата ѝ е повредена.
Всеки тест падаше срещу кода преди поправката (възпроизведено със скриптовете
на одита, audit6/convmem-a/)."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

import genesis_agent.conversation_memory as cm


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    # conftest.py вече пренасочва DB_PATH; тук — към собствена папка, за да се
    # види точно кои файлове (.corrupt, .lock) се появяват до базата.
    data = tmp_path / "convmem"
    data.mkdir()
    monkeypatch.setattr(cm, "DB_PATH", str(data / "conversation_memory.db"))


def _chat(n: int) -> None:
    """Редуващ се разговор през публичното add_message — точно повикването,
    което genesis_terminal_agent._remember прави на всеки ход."""
    for i in range(n):
        if i % 2 == 0:
            cm.add_message("user", f"искане №{i}: направи стъпка {i} от миграцията")
        else:
            cm.add_message("assistant", f"Готово, стъпка {i - 1} е направена, ето кода ...")


# ── 1. резюмето пазеше отговорите на модела, а не репликите на човека ──────────


def test_the_latest_human_request_survives_compression_of_a_real_chat() -> None:
    """51 реда → компресия на най-старите 21 (11 искания на човека). Преди:
    резюмето беше „user: искане №0 | … | assistant: Готово, стъпка 6..18“ —
    последните 7 бяха отговори на модела, а 10 от 11-те искания изчезнаха."""
    _chat(51)
    history = cm.get_history(last_n=1000)
    summary = history[0]["content"]
    assert history[0]["role"] == "system" and "[Context summary]" in summary
    assert "искане №0:" in summary, summary
    assert "искане №20:" in summary, summary
    # 11 искания препълват резюмето — отговорите на модела нямат място в него.
    assert "assistant:" not in summary, summary


def test_when_the_humans_lines_fit_the_models_latest_replies_fill_the_rest() -> None:
    """3 искания + 10 отговора: и трите искания трябва да останат, а отговорите
    само допълват мястото — с най-новите (докъде стигнахме)."""
    msgs = [{"role": "user", "content": f"искане {i}"} for i in range(3)]
    msgs += [{"role": "assistant", "content": f"отговор {i}"} for i in range(10)]
    out = cm._simple_summarize(msgs)
    for i in range(3):
        assert f"user: искане {i}" in out, out
    assert "assistant: отговор 9" in out, out
    assert "assistant: отговор 0" not in out, out
    entries = out.split(" | ")[1:]
    assert len([e for e in entries if e != "…"]) <= cm._SUMMARY_MAX_ITEMS, out


def test_exactly_a_full_summary_of_human_lines_leaves_no_room_for_replies() -> None:
    """Границата: толкова искания, колкото събира резюмето — всичките остават,
    а за отговорите няма място (и резюмето остава ограничено)."""
    msgs = [{"role": "user", "content": f"искане {i}"} for i in range(cm._SUMMARY_MAX_ITEMS)]
    msgs += [{"role": "assistant", "content": f"отговор {i}"} for i in range(10)]
    out = cm._simple_summarize(msgs)
    for i in range(cm._SUMMARY_MAX_ITEMS):
        assert f"user: искане {i}" in out, out
    assert "assistant:" not in out and "…" not in out, out


# ── 2. повредена conversation_memory.db спираше записа завинаги и тихо ─────────


def test_a_corrupt_database_is_set_aside_and_recording_continues(caplog) -> None:
    """Голо sqlite3.connect: „file is not a database“ на всяко add_message, а
    genesis_terminal_agent._remember го гълта на ниво debug — всяка реплика от
    тази и от всяка следваща сесия се губеше без дума към оператора."""
    junk = b"not a database\x00" * 300
    db = Path(cm.DB_PATH)
    db.write_bytes(junk)
    with caplog.at_level(logging.WARNING):
        cm.add_message("user", "след повредата")
    assert cm.get_history() == [{"role": "user", "content": "след повредата"}]
    assert Path(str(db) + ".corrupt").read_bytes() == junk
    assert any("conversation_memory.db" in r.getMessage() and r.levelno >= logging.WARNING
               for r in caplog.records), [r.getMessage() for r in caplog.records]


# ── 3. всяка следваща компресия изхвърляше предишното резюме ────────────────────


def _summary_rows() -> list[str]:
    return [m["content"] for m in cm.get_history(last_n=1000)
            if m["role"] == "system" and m["content"].startswith("[Context summary]")]


def test_a_second_compression_carries_the_first_request_forward() -> None:
    """71 реплики → две компресии. Преди: резюмето започваше от „искане №22“,
    заглавието казваше „21 messages from roles: assistant, system, user“, а
    първото резюме (role=system, най-старият ред) падаше в изрязаната среда —
    паметта покриваше само последните ~50 реплики."""
    _chat(71)
    summaries = _summary_rows()
    assert len(summaries) == 1, summaries
    summary = summaries[0]
    assert "искане №0:" in summary, summary
    assert "искане №40:" in summary, summary
    # Резюмето стои за 21 + 20 реплики; самото то не е „system“ реплика.
    assert summary.startswith("[Context summary] 41 messages from roles: assistant, user | "), summary


def test_the_header_plus_the_kept_rows_always_account_for_every_message() -> None:
    """Истинският ход в терминала: една реплика на човека и 2-3 на модела
    (инструменти). Преди: първата компресия пазеше „искане №0“, втората и
    третата го губеха, а бройката в заглавието падаше обратно на ~21."""
    added = 0
    for turn in range(40):
        cm.add_message("user", f"искане №{turn}: оправи модул {turn}")
        added += 1
        for k in range(2 + turn % 2):
            cm.add_message("assistant", f"ход {turn}.{k}: пускам тестовете ...")
            added += 1
    history = cm.get_history(last_n=1000)
    summary = history[0]["content"]
    assert history[0]["role"] == "system" and len(_summary_rows()) == 1, summary
    count = int(summary.split()[2])
    assert count + len(history) - 1 == added, (count, len(history), added)
    assert "искане №0:" in summary, summary


def test_a_carried_line_with_a_pipe_in_it_stays_whole() -> None:
    """Реплика с „ | “ в текста си не бива да се разпада на две при пренасяне
    в следващото резюме."""
    first = cm._simple_summarize([
        {"role": "user", "content": "мигрирай billing | после auth"},
        {"role": "assistant", "content": "ок"},
    ])
    out = cm._simple_summarize(
        [{"role": "system", "content": first}]
        + [{"role": "user", "content": f"искане {i}"} for i in range(10)]
    )
    assert out.startswith("[Context summary] 12 messages from roles: assistant, user | "
                          "user: мигрирай billing ¦ после auth | … | "), out
    assert out.endswith("user: искане 9"), out


def test_the_gap_marker_survives_when_nothing_new_pushes_it_out() -> None:
    """Предишното резюме е „първата | … | последните“. Ако новият блок няма
    реплики на човека, пренесените пак се събират — но „…“ трябва да остане,
    иначе „стъпка 0 | стъпка 13“ изглежда като два поредни хода."""
    first = cm._simple_summarize([{"role": "user", "content": f"стъпка {i}"} for i in range(20)])
    assert "user: стъпка 0 | … | user: стъпка 13" in first, first
    out = cm._simple_summarize(
        [{"role": "system", "content": first}]
        + [{"role": "assistant", "content": f"отговор {i}"} for i in range(3)]
    )
    assert out.startswith("[Context summary] 23 messages from roles: assistant, user | "
                          "user: стъпка 0 | … | user: стъпка 13"), out
    assert "стъпка 19" in out, out
    entries = out.split(" | ")[1:]
    assert len([e for e in entries if e != "…"]) <= cm._SUMMARY_MAX_ITEMS, out


def test_a_real_system_message_is_still_counted_and_summarised() -> None:
    """Само ред, който Е резюме, се разгъва; обикновена system реплика остава
    реплика — с ролята си в заглавието."""
    out = cm._simple_summarize([
        {"role": "system", "content": "ти си асистент за миграции"},
        {"role": "user", "content": "почни"},
    ])
    assert out.startswith("[Context summary] 2 messages from roles: system, user | "), out
    assert "system: ти си асистент за миграции" in out, out
