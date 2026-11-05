"""Одит 2026-10-31 (шести кръг): genesis_agent/embeddings.py — повреден
embeddings.db и мълчаливите HTTP грешки на Ollama. Всеки тест падаше срещу
кода преди поправката (възпроизведено със скриптовете на одита, audit6/) —
освен пазача за зает файл, който пази самата поправка да не мести жива база.

Мрежата е спряна: `embed` се подменя, а там, където се проверява самият
`embed`, се подменя `requests.post` — никакъв истински Ollama и никакви ключове.
"""
from __future__ import annotations

import gc
import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from genesis_agent import embeddings as emb


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(emb, "DB_PATH", tmp_path / "embeddings.db")


def _skills(tmp_path: Path, monkeypatch, names: list[str]) -> None:
    index = tmp_path / "skills.json"
    index.write_text(json.dumps({"skills": [{"name": n, "description": n} for n in names]}),
                     encoding="utf-8")
    monkeypatch.setattr(emb, "SKILLS_INDEX", index)


def _fake_embed(monkeypatch, dim: int = 8) -> list[str]:
    calls: list[str] = []

    def fake(text, timeout=60):
        calls.append(text)
        return [1.0 + i * 0.001 for i in range(dim)]
    monkeypatch.setattr(emb, "embed", fake)
    return calls


def _names(hits) -> list[str]:
    return sorted(n for n, _ in hits)


def _kept_warning(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "embeddings.db.corrupt" in r.getMessage()]


def _healthy_index(tmp_path: Path, monkeypatch, names: list[str], dim: int = 8) -> None:
    """Здрав индекс, записан от самия модул, и всички връзки затворени —
    WAL-ът е прехвърлен в главния файл, за да стигне повредата до четенето."""
    _skills(tmp_path, monkeypatch, names)
    _fake_embed(monkeypatch, dim)
    assert _names(emb.semantic_search("q", top_k=10)) == sorted(names)
    gc.collect()
    assert not Path(str(emb.DB_PATH) + "-wal").exists()


# ── embeddings-a1: нечетим embeddings.db изключваше търсенето по смисъл завинаги ──


def test_a_junk_embeddings_db_is_set_aside_and_search_finds_the_skill_again(
        tmp_path, monkeypatch, caplog) -> None:
    """Синхронизация/чужд файл: „file is not a database“ на всяко търсене и
    всеки запис на умение, без нито едно предупреждение. Базата е само кеш —
    слага се настрани и се пълни наново."""
    _skills(tmp_path, monkeypatch, ["reverse_string"])
    _fake_embed(monkeypatch)
    junk = b"not sqlite" * 100
    emb.DB_PATH.write_bytes(junk)

    with caplog.at_level(logging.WARNING):
        hits = emb.semantic_search("flip characters backwards")
    assert _names(hits) == ["reverse_string"]
    assert (tmp_path / "embeddings.db.corrupt").read_bytes() == junk
    assert len(_kept_warning(caplog)) == 1, caplog.text
    # И следващото търсене работи от новата база, без нова повреда.
    assert _names(emb.semantic_search("again")) == ["reverse_string"]
    assert not (tmp_path / "embeddings.db.corrupt.2").exists()


def test_a_truncated_embeddings_db_is_set_aside_and_rebuilt(tmp_path, monkeypatch, caplog) -> None:
    """Наполовина копиран файл: „database disk image is malformed“ завинаги."""
    _healthy_index(tmp_path, monkeypatch, ["a_skill", "b_skill", "c_skill"], dim=1024)
    size = emb.DB_PATH.stat().st_size
    with open(emb.DB_PATH, "r+b") as f:
        f.truncate(size // 3)

    with caplog.at_level(logging.WARNING):
        hits = emb.semantic_search("q", top_k=10)
    assert _names(hits) == ["a_skill", "b_skill", "c_skill"]
    assert (tmp_path / "embeddings.db.corrupt").stat().st_size == size // 3
    assert len(_kept_warning(caplog)) == 1, caplog.text


def test_a_page_damaged_after_opening_is_also_recovered(tmp_path, monkeypatch, caplog) -> None:
    """Повреда вътре в страница: отварянето и схемата минават, а гърми чак
    SELECT-ът на векторите. Само смяна на връзката (sqlite_store.connect) не
    стига — затова и тялото на търсенето се пробва наново."""
    _healthy_index(tmp_path, monkeypatch, ["a_skill", "b_skill"])
    raw = sqlite3.connect(emb.DB_PATH)
    try:
        root = raw.execute("SELECT rootpage FROM sqlite_master WHERE name = 'embeddings'").fetchone()[0]
        page = raw.execute("PRAGMA page_size").fetchone()[0]
    finally:
        raw.close()
    with open(emb.DB_PATH, "r+b") as f:
        f.seek((root - 1) * page)
        f.write(b"\xff" * page)

    with caplog.at_level(logging.WARNING):
        hits = emb.semantic_search("q", top_k=10)
    assert _names(hits) == ["a_skill", "b_skill"]
    assert (tmp_path / "embeddings.db.corrupt").exists()
    assert len(_kept_warning(caplog)) == 1, caplog.text


def test_saving_a_skill_into_a_junk_embeddings_db_indexes_it(tmp_path, monkeypatch) -> None:
    """skills_manager.save_skill → index_skill: грешката се гълташе на debug и
    нито едно ново умение не влизаше в индекса."""
    _fake_embed(monkeypatch)
    emb.DB_PATH.write_bytes(b"not sqlite" * 100)
    assert emb.index_skill("new_skill", "new skill. does things") is True
    assert [n for n, _ in emb._all_vectors()] == ["new_skill"]
    assert (tmp_path / "embeddings.db.corrupt").exists()


def test_a_healthy_database_is_never_set_aside(tmp_path, monkeypatch) -> None:
    """Пазачът на поправката: процес, който е чел стария (вече сменен от друг
    процес) файл, вижда „malformed“ — но файлът на диска сега е здрав и не
    бива да отиде настрани заедно с току-що изчисления индекс."""
    _healthy_index(tmp_path, monkeypatch, ["a_skill", "b_skill"])
    calls = _fake_embed(monkeypatch)
    real = emb._all_vectors
    failed: list[bool] = []

    def once(model=None):
        if not failed:
            failed.append(True)
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real(model)
    monkeypatch.setattr(emb, "_all_vectors", once)

    assert _names(emb.semantic_search("q", top_k=10)) == ["a_skill", "b_skill"]
    assert failed == [True]
    assert not (tmp_path / "embeddings.db.corrupt").exists()
    assert calls == ["q"]  # нищо не е преизчислено


def test_a_busy_database_is_not_mistaken_for_a_damaged_one(tmp_path, monkeypatch) -> None:
    """„database is locked“ е зает файл, не развален — грешката си остава."""
    _healthy_index(tmp_path, monkeypatch, ["a_skill"])

    def busy(model=None):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(emb, "_all_vectors", busy)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        emb.semantic_search("q")
    assert not (tmp_path / "embeddings.db.corrupt").exists()


def _smash(pgno: int) -> None:
    raw = sqlite3.connect(emb.DB_PATH)
    try:
        page = raw.execute("PRAGMA page_size").fetchone()[0]
    finally:
        raw.close()
    with open(emb.DB_PATH, "r+b") as f:
        f.seek((pgno - 1) * page)
        f.write(b"\xff" * page)


def test_garbage_in_an_overflow_page_is_also_recovered(tmp_path, monkeypatch, caplog) -> None:
    """Ревю 2026-11-05, мерено: при 1024 измерения векторът прелива в своя
    страница и model/text_hash са там. Боклук в нея не е „malformed“, а
    „Could not decode to UTF-8 column 'model'“ — търсенето гърмеше завинаги,
    макар integrity_check да вижда повредата."""
    _healthy_index(tmp_path, monkeypatch, ["a_skill", "b_skill", "c_skill"], dim=1024)
    raw = sqlite3.connect(emb.DB_PATH)
    try:
        last = raw.execute("PRAGMA page_count").fetchone()[0]
        roots = {r for (r,) in raw.execute("SELECT rootpage FROM sqlite_master")}
    finally:
        raw.close()
    assert last not in roots | {1}
    _smash(last)

    with caplog.at_level(logging.WARNING):
        hits = emb.semantic_search("q", top_k=10)
    assert _names(hits) == ["a_skill", "b_skill", "c_skill"]
    assert (tmp_path / "embeddings.db.corrupt").exists()
    assert len(_kept_warning(caplog)) == 1, caplog.text


def _damaged_freelist(tmp_path: Path, monkeypatch) -> list[str]:
    """Повреда, която стига само записът: страницата-ствол на свободния списък
    (SELECT-ите не я четат, нов ред с препълване — да)."""
    names = [f"s{i}" for i in range(8)]
    _healthy_index(tmp_path, monkeypatch, names, dim=1024)
    _skills(tmp_path, monkeypatch, names[:4])  # _drop_ghosts трие → свободни страници
    assert _names(emb.semantic_search("q", top_k=10)) == names[:4]
    trunk = int.from_bytes(emb.DB_PATH.read_bytes()[32:36], "big")
    assert trunk > 1
    _smash(trunk)
    return names[:4]


def test_damage_seen_only_by_a_write_is_recovered_in_index_skill(tmp_path, monkeypatch) -> None:
    _damaged_freelist(tmp_path, monkeypatch)
    assert emb.index_skill("new_one", "new one. does things") is True
    assert "new_one" in [n for n, _ in emb._all_vectors()]
    assert (tmp_path / "embeddings.db.corrupt").exists()


def test_damage_seen_only_by_a_write_still_returns_the_whole_index(tmp_path, monkeypatch) -> None:
    """Възстановяване посред _index_missing не бива да оставя в новата база
    само умението, което се е записвало в момента на повредата."""
    kept = _damaged_freelist(tmp_path, monkeypatch)
    _skills(tmp_path, monkeypatch, [*kept, "new_one"])
    assert _names(emb.semantic_search("q", top_k=10)) == sorted([*kept, "new_one"])
    assert (tmp_path / "embeddings.db.corrupt").exists()


# ── embeddings-a5: HTTP грешка от Ollama не оставяше следа дори в debug.log ──


@pytest.mark.parametrize(("status", "body"), [
    (404, '{"error":"model \\"bge-m3\\" not found, try pulling it first"}'),
    (500, '{"error":"llama runner process has terminated"}'),
])
def test_an_ollama_http_error_is_recorded_at_debug_level(monkeypatch, caplog, status, body) -> None:
    """404 (моделът не е изтеглен — например останал nomic отпреди
    2026-09-24) или 500 връщаха None без нито ред, а грешка при свързване се
    записваше. С GENESIS_DEBUG=1 операторът не виждаше защо търсенето по
    смисъл е спряло."""
    def post(url, **kwargs):
        return SimpleNamespace(status_code=status, text=body, json=dict)
    monkeypatch.setattr(emb.requests, "post", post)

    with caplog.at_level(logging.DEBUG, logger="genesis.embeddings"):
        assert emb.embed("text") is None
    records = [r.getMessage() for r in caplog.records if r.name == "genesis.embeddings"]
    assert len(records) == 1, records
    assert str(status) in records[0] and body[:20] in records[0]
