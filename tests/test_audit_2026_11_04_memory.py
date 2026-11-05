"""Regressions for the memory bugs an audit reproduced on 2026-11-04
(шести кръг, 1/3): knowledge_graph, conversation_memory, embeddings.
Every model and every Ollama answer is a fake; paths are tmp (conftest)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import genesis_agent.brain as brain_module
from genesis_agent import conversation_memory as cm
from genesis_agent import embeddings as emb
from genesis_agent import knowledge_graph as kg

ROOT = Path(__file__).resolve().parent.parent

# Изглеждат като истински ключове (цифри, главни букви, точната дължина), за
# да ги хване всяко правило; не са ничии.
_GH = "ghp_" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0dF3gH6"
_STRIPE = "sk_live_" + "51HxQ2eLkdIwHu7ix0a9b8c7Z"
_JUNK = b"not a database at all " * 200


def _model_says(monkeypatch, raw: str | dict) -> None:
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)

    class _Fake:
        def __init__(self, *a, **kw) -> None:
            pass

        def complete(self, messages, **kw):
            return type("Reply", (), {"raw_text": text})

    monkeypatch.setattr(brain_module, "Brain", _Fake)


# ─── knowledge_graph ─────────────────────────────────────────────────────────

def test_a_relation_extracted_again_is_kept_once(monkeypatch) -> None:
    facts = {"entities": [], "states": [],
             "relations": [{"source": "API", "relation": "USES", "target": "Redis"},
                           {"source": "Auth", "relation": "DEPENDS_ON", "target": "JWT"}]}
    _model_says(monkeypatch, facts)
    for _ in range(6):
        kg.compact_and_graph_memory("user: x", workspace="/proj/a")
    _model_says(monkeypatch, {"relations": [{"source": "api", "relation": "uses", "target": "redis"}]})
    kg.compact_and_graph_memory("user: x", workspace="/proj/a")
    kg.compact_and_graph_memory("user: x", workspace="/proj/b")  # друг проект — свой ред

    lines = kg.graph_briefing(workspace="/proj/a").splitlines()
    assert lines == ["- Auth DEPENDS_ON JWT", "- api uses redis"], lines
    assert len(kg.graph_briefing(workspace="/proj/b").splitlines()) == 1
    assert len(json.loads(kg.GRAPH_PATH.read_text(encoding="utf-8"))["relations"]) == 3


def test_secrets_from_the_chat_reach_neither_the_graph_nor_the_prompt(monkeypatch) -> None:
    _model_says(monkeypatch, {
        "entities": [{"name": f"GITHUB_TOKEN={_GH}", "type": "secret"}],
        "relations": [{"source": "deploy", "relation": "USES", "target": f"GITHUB_TOKEN={_GH}"}],
        "states": [{"entity": "Stripe ключ", "status": f"зададен: {_STRIPE}"},
                   {"entity": "DB", "status": "postgres://admin:hunter2pass@db.local/app"}]})
    kg.compact_and_graph_memory(f"user: ключът за stripe е {_STRIPE}", workspace="/proj/a")
    on_disk = kg.GRAPH_PATH.read_text(encoding="utf-8")
    # Запис отпреди поправката: вече е във файла и не бива да стигне до промпта.
    graph = json.loads(on_disk)
    graph["states"]["стар"] = {"name": "стар", "status": f"токен {_GH}", "updated_at": "2026-01-01"}
    kg.GRAPH_PATH.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")

    brief = kg.graph_briefing(workspace="/proj/a")
    assert "Stripe ключ" in brief and "deploy USES" in brief and "стар: токен" in brief
    for secret in (_GH, _STRIPE, "hunter2pass"):
        assert secret not in brief, brief
        assert secret not in on_disk, on_disk


def test_a_runaway_field_does_not_bloat_the_system_prompt(monkeypatch) -> None:
    long = "миграцията на базата се провали, защото " * 60     # 2400 знака
    _model_says(monkeypatch, {
        "entities": [{"name": "E" * 5000, "type": "x" * 1000}],
        "relations": [{"source": f"S{i}", "relation": "BLOCKS", "target": long} for i in range(20)],
        "states": [{"entity": f"Модул {i}", "status": long} for i in range(20)]})
    kg.compact_and_graph_memory("user: x", workspace="/proj/a")
    brief = kg.graph_briefing(workspace="/proj/a")
    # Беше 57 887 знака (~15K токена) на всеки рунд; обещанието е ~150-250 токена.
    assert len(brief) < 6000, len(brief)
    assert "- Модул 0: миграцията на базата се провали" in brief
    assert kg.GRAPH_PATH.stat().st_size < 30_000


@pytest.mark.parametrize("wrap", [
    "Ето графа:\n```json\n{}\n```",
    "```JSON\n{}\n```",
    "```json\n{}\n```\nНадявам се да помогне.",
    "Ето JSON:\n{}",
], ids=["prose-before-fence", "upper-JSON-fence", "prose-after-fence", "prose-before-object"])
def test_json_wrapped_in_prose_still_grows_the_graph(monkeypatch, wrap: str) -> None:
    obj = json.dumps({"entities": [{"name": "Redis", "type": "db"}], "relations": [], "states": []})
    _model_says(monkeypatch, wrap.replace("{}", obj))
    assert "redis" in kg.compact_and_graph_memory("user: x")["entities"]


# ─── повредени бази ──────────────────────────────────────────────────────────

def test_a_corrupt_conversation_db_is_set_aside_and_the_chat_is_kept() -> None:
    Path(cm.DB_PATH).write_bytes(_JUNK)
    cm.add_message("user", "първа реплика")
    cm.add_message("assistant", "отговор")
    assert [m["content"] for m in cm.get_history()] == ["първа реплика", "отговор"]
    assert Path(cm.DB_PATH + ".corrupt").read_bytes() == _JUNK


def test_a_corrupt_embeddings_db_is_set_aside_and_search_works_again(monkeypatch) -> None:
    Path(emb.DB_PATH).write_bytes(_JUNK)
    monkeypatch.setattr(emb, "embed", lambda text, timeout=60: [1.0, 0.0, 0.5])
    assert emb.index_skill("reverse_string", "reverse a string") is True
    assert [n for n, _ in emb.semantic_search("invert text")] == ["reverse_string"]
    assert Path(str(emb.DB_PATH) + ".corrupt").read_bytes() == _JUNK


def test_a_busy_new_embeddings_db_is_waited_for_not_failed(monkeypatch) -> None:
    # Другият процес пише в базата, която още не е WAL (нова или стара):
    # прагмата не чака busy_timeout и index_skill гърмеше с „database is locked“.
    import sqlite3
    import threading
    monkeypatch.setattr(emb, "embed", lambda text, timeout=60: [1.0, 0.0])
    other = sqlite3.connect(emb.DB_PATH, check_same_thread=False, isolation_level=None)
    other.execute("CREATE TABLE t (a);")
    other.execute("BEGIN IMMEDIATE;")
    other.execute("INSERT INTO t VALUES (1);")
    timer = threading.Timer(0.3, other.execute, ("COMMIT;",))
    timer.start()
    try:
        assert emb.index_skill("reverse_string", "reverse a string") is True
    finally:
        timer.join()
        other.close()


_EMB_RACER = """
import json, sys, time
from pathlib import Path
from genesis_agent import embeddings as emb
emb.DB_PATH = Path(sys.argv[1])
emb.embed = lambda text, timeout=60: [1.0] * 1024
Path(sys.argv[3]).touch()
go = Path(sys.argv[2])
while not go.exists():
    time.sleep(0.001)
errors = []
for k in range(5):
    try:
        if not emb.index_skill("%s_%d" % (Path(sys.argv[3]).name, k), "text"):
            errors.append("False")
    except Exception as e:
        errors.append(repr(e))
print(json.dumps(errors))
"""


def test_processes_creating_the_embeddings_index_at_once_lose_nothing(tmp_path) -> None:
    racer = tmp_path / "racer.py"
    racer.write_text(_EMB_RACER, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
               GENESIS_DATA_DIR=str(tmp_path / "data"), PYTHONPATH=str(ROOT),
               PYTHONIOENCODING="utf-8")
    env.pop("GENESIS_MEMORY_DIR", None)
    errors: list[str] = []
    for rnd in range(3):                     # всеки кръг е върху нова база
        db, go = tmp_path / f"emb{rnd}.db", tmp_path / f"go{rnd}"
        ready = [tmp_path / f"r{rnd}_{n}" for n in range(8)]
        procs = [subprocess.Popen([sys.executable, str(racer), str(db), str(go), str(r)],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for r in ready]
        deadline = time.monotonic() + 60
        while not all(r.exists() for r in ready) and time.monotonic() < deadline:
            time.sleep(0.01)
        go.touch()
        for p in procs:
            out, err = p.communicate(timeout=120)
            assert p.returncode == 0, err
            errors += json.loads(out.strip().splitlines()[-1])
    assert errors == []


def test_a_lone_surrogate_from_the_model_does_not_break_the_save(monkeypatch) -> None:
    """Отвореното от одита (2026-11-05): `"\\ud800"` в JSON-а на модела (отрязано
    емоджи) правеше UnicodeEncodeError при записа — ValueError, не OSError, тоест
    compact_and_graph_memory хвърляше и оставяше `.tmp` до графа."""
    _model_says(monkeypatch, '{"relations": [{"source": "API\\ud83d", "relation": "USES", '
                             '"target": "Redis"}]}')
    kg.compact_and_graph_memory("user: x", workspace="/proj/a")
    assert "API? USES Redis" in kg.graph_briefing(workspace="/proj/a")
    assert not list(kg.GRAPH_PATH.parent.glob(f"{kg.GRAPH_PATH.name}.*.tmp"))
