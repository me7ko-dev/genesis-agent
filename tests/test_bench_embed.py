"""scripts/bench_embed.py — сравнението на embedding модели за български.

Ollama се подменя с фалшив `embed`, за да се провери само сметката: кой модел
колко попадения има и че заявка без правилното умение в индекса се пропуска.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bench_embed

from genesis_agent import embeddings as emb

INDEX = {
    "alpha": {"name": "alpha", "description": "a"},
    "beta": {"name": "beta", "description": "b"},
}
QUERIES = [("търси алфа", "alpha"), ("търси бета", "beta")]


def _fake_embed(right: bool):
    def embed(text, timeout=60, *, model=None):
        if text.startswith("alpha"):
            return [1.0, 0.0]
        if text.startswith("beta"):
            return [0.0, 1.0]
        # Правилният модел слага заявката до нейното умение, грешният — обратно.
        hit_alpha = ("алфа" in text) == right
        return [1.0, 0.1] if hit_alpha else [0.1, 1.0]
    return embed


def test_a_model_that_finds_the_skill_scores_every_hit(monkeypatch) -> None:
    monkeypatch.setattr(emb, "embed", _fake_embed(right=True))
    r = bench_embed.bench_model("m", INDEX, QUERIES)
    assert (r["hit1"], r["hit3"], r["over"]) == (2, 2, 2)
    assert r["misses"] == []


def test_a_model_that_confuses_them_is_counted_as_missing(monkeypatch) -> None:
    monkeypatch.setattr(emb, "embed", _fake_embed(right=False))
    r = bench_embed.bench_model("m", INDEX, QUERIES)
    assert r["hit1"] == 0
    assert r["hit3"] == 2  # with only two skills, both are in the top 3
    assert len(r["misses"]) == 2


def test_a_failed_embedding_call_is_reported_not_scored_as_zero(monkeypatch) -> None:
    monkeypatch.setattr(emb, "embed", lambda *a, **k: None)
    assert bench_embed.bench_model("m", INDEX, QUERIES) is None


def test_every_expected_skill_ships_in_the_bundled_index() -> None:
    """Иначе заявката тихо се пропуска и сравнението е по-малко от 20."""
    import json
    bundled = Path(emb.__file__).parent / "skills" / "skills.json"
    names = {s["name"] for s in json.loads(bundled.read_text(encoding="utf-8"))["skills"]}
    assert {want for _, want in bench_embed.QUERIES} <= names


def test_the_model_can_be_switched_by_environment(monkeypatch) -> None:
    monkeypatch.setenv("GENESIS_EMBED_MODEL", "bge-m3")
    try:
        assert importlib.reload(emb).MODEL == "bge-m3"
    finally:
        monkeypatch.delenv("GENESIS_EMBED_MODEL")
        importlib.reload(emb)
    assert emb.MODEL == "nomic-embed-text"
