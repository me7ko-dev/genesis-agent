"""genesis_agent.embeddings — сравнимост на векторите и пътят до умение.

Модулът нямаше нито един тест, а от него зависи дали перифразирана заявка
намира вече съществуващо умение (skill_loader.search_skills) и дали ново
умение изобщо се записва (semantic_duplicate). Мрежата (Ollama) се спира на
`embed`, за да не решава наличието на локален сървър дали тестът тече.

DB_PATH се пренасочва към tmp: модулът си го свързва при import от
genesis_agent.config, така че закърпването на config после няма ефект — виж
tests/conftest.py.
"""
from __future__ import annotations

import pytest

from genesis_agent import embeddings as emb


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(emb, "DB_PATH", tmp_path / "embeddings.db")
    yield


def _vec(n: int, seed: float = 1.0) -> list[float]:
    return [seed + i * 0.001 for i in range(n)]


class TestCosineComparability:
    """`zip` реже до по-късия вектор мълчаливо. Таблицата пази `dim` за всеки
    ред поотделно, тоест вектори от различни модели могат да съжителстват —
    и тогава близостта се смяташе върху отрязания вектор."""

    def test_vectors_of_different_length_are_not_comparable(self) -> None:
        assert emb._cosine(_vec(768), _vec(384)) == 0.0

    def test_the_truncated_comparison_was_not_merely_a_weaker_score(self) -> None:
        """Числото, което излизаше, е достатъчно високо да мине прага за
        семантично попадение (0.55 в skill_loader) по чист шанс."""
        import random
        random.seed(7)
        a = [random.random() for _ in range(768)]
        b = [random.random() for _ in range(384)]
        truncated = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a[:384]) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        assert truncated / (na * nb) > 0.55
        assert emb._cosine(a, b) == 0.0

    def test_identical_vectors_score_one(self) -> None:
        assert emb._cosine(_vec(16), _vec(16)) == pytest.approx(1.0)

    def test_a_zero_vector_scores_zero_not_a_division_error(self) -> None:
        assert emb._cosine([0.0] * 8, _vec(8)) == 0.0
        assert emb._cosine(_vec(8), [0.0] * 8) == 0.0

    def test_empty_vectors_score_zero(self) -> None:
        assert emb._cosine([], []) == 0.0


class TestPackRoundTrip:
    def test_a_vector_survives_the_blob_round_trip(self) -> None:
        vec = [0.5, -0.25, 0.125]
        assert emb._unpack(emb._pack(vec), len(vec)) == pytest.approx(vec)


class TestSemanticSearch:
    def test_records_from_another_model_are_skipped_not_ranked(self, monkeypatch) -> None:
        """Стар индекс не бива да изглежда като слаби съвпадения."""
        monkeypatch.setattr(emb, "embed", lambda text, timeout=10: _vec(8))
        emb.index_skill("same_dim", "x")
        monkeypatch.setattr(emb, "embed", lambda text, timeout=10: _vec(4))
        emb.index_skill("other_dim", "y")

        monkeypatch.setattr(emb, "embed", lambda text, timeout=10: _vec(8))
        names = [n for n, _ in emb.semantic_search("query", top_k=5)]
        assert names == ["same_dim"]

    def test_search_returns_nothing_when_embeddings_are_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr(emb, "embed", lambda text, timeout=10: None)
        assert emb.semantic_search("anything") == []

    def test_results_come_back_ranked_best_first(self, monkeypatch) -> None:
        monkeypatch.setattr(emb, "embed", lambda text, timeout=10: [1.0, 0.0])
        emb.index_skill("exact", "a")
        monkeypatch.setattr(emb, "embed", lambda text, timeout=10: [0.0, 1.0])
        emb.index_skill("orthogonal", "b")

        monkeypatch.setattr(emb, "embed", lambda text, timeout=10: [1.0, 0.0])
        ranked = emb.semantic_search("q", top_k=2)
        assert [n for n, _ in ranked] == ["exact", "orthogonal"]
        assert ranked[0][1] > ranked[1][1]

    def test_top_k_is_respected(self, monkeypatch) -> None:
        monkeypatch.setattr(emb, "embed", lambda text, timeout=10: _vec(4))
        for i in range(5):
            emb.index_skill(f"s{i}", "t")
        assert len(emb.semantic_search("q", top_k=2)) == 2


class TestIndexSkill:
    def test_indexing_fails_cleanly_when_no_embedding_comes_back(self, monkeypatch) -> None:
        monkeypatch.setattr(emb, "embed", lambda text, timeout=10: None)
        assert emb.index_skill("nope", "text") is False

    def test_reindexing_the_same_name_replaces_rather_than_duplicates(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(emb, "embed", lambda text, timeout=10: _vec(4))
        emb.index_skill("dup", "first")
        emb.index_skill("dup", "second")
        assert emb._count_vectors() == 1


class TestSemanticDuplicate:
    def test_a_close_enough_match_is_reported_as_a_duplicate(self, monkeypatch) -> None:
        monkeypatch.setattr(emb, "embed", lambda text, timeout=10: [1.0, 0.0])
        emb.index_skill("already_here", "reverse a string")
        assert emb.semantic_duplicate("invert character order") == "already_here"

    def test_a_distant_match_is_not_a_duplicate(self, monkeypatch) -> None:
        monkeypatch.setattr(emb, "embed", lambda text, timeout=10: [1.0, 0.0])
        emb.index_skill("unrelated", "a")
        monkeypatch.setattr(emb, "embed", lambda text, timeout=10: [0.0, 1.0])
        assert emb.semantic_duplicate("something else") is None

    def test_a_stale_dimension_cannot_block_a_new_skill(self, monkeypatch) -> None:
        """The expensive half of the bug: a false 0.92+ match stops a genuinely
        new skill from ever being written, with "one like this already exists".

        The vectors are chosen to actually reach that threshold under the old
        truncating comparison — a query whose first 384 values match the stored
        record and whose remaining values are near zero scores 1.0 once the tail
        is cut away, because the cut also removes it from the query's norm. A
        gently increasing tail only reaches ~0.60 and would let this test pass
        against the unfixed code, proving nothing.
        """
        monkeypatch.setattr(emb, "embed", lambda text, timeout=10: [1.0] * 384)
        emb.index_skill("old_model_entry", "whatever")
        monkeypatch.setattr(emb, "embed",
                            lambda text, timeout=10: [1.0] * 384 + [0.01] * 384)
        assert emb.semantic_duplicate("a genuinely new skill") is None
