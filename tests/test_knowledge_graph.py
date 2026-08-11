"""genesis_agent.knowledge_graph — GraphRAG-style entity/relation/state
extraction. GRAPH_PATH is monkeypatched to a tmp file. Brain.complete is
faked (no network/API key needed) since compact_and_graph_memory's own logic
— dedup, relation cap, fail-open — is what's under test, not the LLM call."""
from __future__ import annotations

import json

import genesis_agent.brain as brain_module
from genesis_agent import knowledge_graph as kg


class _FakeReply:
    def __init__(self, raw_text: str) -> None:
        self.raw_text = raw_text


class _FakeBrain:
    last_prompt: str | None = None

    def __init__(self, *a, **kw) -> None:
        pass

    def complete(self, messages):
        _FakeBrain.last_prompt = messages[0]["content"]
        return _FakeReply(_FakeBrain.reply_text)


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(kg, "GRAPH_PATH", tmp_path / "knowledge_graph.json")


def _stub_brain(monkeypatch, reply_json: dict) -> None:
    _FakeBrain.reply_text = json.dumps(reply_json)
    monkeypatch.setattr(brain_module, "Brain", _FakeBrain)


def test_dedup_key_normalizes_case_and_spacing() -> None:
    assert kg._dedup_key("AuthToken") == kg._dedup_key("auth token")
    assert kg._dedup_key("Auth-Token!") == kg._dedup_key("authtoken")


def test_empty_session_logs_returns_graph_unchanged(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    result = kg.compact_and_graph_memory("")
    assert result == kg._empty_graph()


def test_fail_open_when_brain_raises(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)

    class _Boom:
        def __init__(self, *a, **kw): pass
        def complete(self, messages): raise RuntimeError("no provider available")

    monkeypatch.setattr(brain_module, "Brain", _Boom)
    result = kg.compact_and_graph_memory("some transcript")
    assert result == kg._empty_graph()


def test_fail_open_on_malformed_json(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    _FakeBrain.reply_text = "not valid json at all"
    monkeypatch.setattr(brain_module, "Brain", _FakeBrain)
    result = kg.compact_and_graph_memory("transcript")
    assert result == kg._empty_graph()


def test_extracts_entities_relations_states(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    _stub_brain(monkeypatch, {
        "entities": [{"name": "AuthToken", "type": "component"}],
        "relations": [{"source": "AuthToken", "relation": "USES", "target": "JWT"}],
        "states": [{"entity": "AuthToken", "status": "in progress"}],
    })
    result = kg.compact_and_graph_memory("we're building AuthToken with JWT")
    assert kg._dedup_key("AuthToken") in result["entities"]
    assert result["relations"][-1] == {
        "source": "AuthToken", "relation": "USES", "target": "JWT", "at": result["relations"][-1]["at"],
    }
    assert result["states"][kg._dedup_key("AuthToken")]["status"] == "in progress"


def test_repeated_entity_mentioned_differently_dedupes_to_one(tmp_path, monkeypatch) -> None:
    """The exact live-caught case (2026-07-29): 'AuthToken' (code symbol) vs
    'auth token' (prose) landing as two separate entities instead of one."""
    _isolate(monkeypatch, tmp_path)
    _stub_brain(monkeypatch, {"entities": [{"name": "AuthToken", "type": "component"}],
                              "relations": [], "states": []})
    kg.compact_and_graph_memory("first mention")

    _stub_brain(monkeypatch, {"entities": [{"name": "auth token", "type": "component"}],
                              "relations": [], "states": []})
    result = kg.compact_and_graph_memory("second mention, different phrasing")

    assert len(result["entities"]) == 1


def test_entity_first_seen_preserved_across_updates(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    _stub_brain(monkeypatch, {"entities": [{"name": "X", "type": "t"}], "relations": [], "states": []})
    first = kg.compact_and_graph_memory("a")
    first_seen = first["entities"][kg._dedup_key("X")]["first_seen"]

    _stub_brain(monkeypatch, {"entities": [{"name": "X", "type": "t"}], "relations": [], "states": []})
    second = kg.compact_and_graph_memory("b")
    assert second["entities"][kg._dedup_key("X")]["first_seen"] == first_seen


def test_relations_capped_at_max_stored(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    for i in range(kg._MAX_RELATIONS_STORED + 10):
        _stub_brain(monkeypatch, {
            "entities": [], "states": [],
            "relations": [{"source": f"s{i}", "relation": "USES", "target": f"t{i}"}],
        })
        result = kg.compact_and_graph_memory(f"round {i}")
    assert len(result["relations"]) == kg._MAX_RELATIONS_STORED
    assert result["relations"][-1]["source"] == f"s{kg._MAX_RELATIONS_STORED + 9}"


def test_incomplete_relation_entries_are_skipped(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    _stub_brain(monkeypatch, {
        "entities": [], "states": [],
        "relations": [{"source": "a", "relation": "", "target": "b"},
                      {"source": "", "relation": "USES", "target": "b"}],
    })
    result = kg.compact_and_graph_memory("x")
    assert result["relations"] == []


def test_graph_briefing_stays_compact_and_formats_relations(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    _stub_brain(monkeypatch, {
        "entities": [], "states": [{"entity": "Deploy", "status": "blocked on review"}],
        "relations": [{"source": "A", "relation": "BLOCKS", "target": "B"}],
    })
    kg.compact_and_graph_memory("x")
    briefing = kg.graph_briefing()
    assert "A BLOCKS B" in briefing
    assert "Deploy: blocked on review" in briefing


def test_graph_briefing_empty_when_no_graph(tmp_path, monkeypatch) -> None:
    _isolate(monkeypatch, tmp_path)
    assert kg.graph_briefing() == ""


def test_parse_llm_json_strips_markdown_fence() -> None:
    raw = '```json\n{"entities": [], "relations": [], "states": []}\n```'
    assert kg._parse_llm_json(raw) == {"entities": [], "relations": [], "states": []}


def test_parse_llm_json_plain_object() -> None:
    assert kg._parse_llm_json('{"a": 1}') == {"a": 1}


def test_load_recovers_from_corrupt_json_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "knowledge_graph.json"
    path.write_text("{not valid json")
    monkeypatch.setattr(kg, "GRAPH_PATH", path)
    assert kg._load() == kg._empty_graph()


class TestNeverRaisesOnBadModelOutput:
    """The module docstring promises "a malformed LLM response or a missing
    file never raises — worst case, the graph just doesn't grow this round".
    Valid JSON of the WRONG SHAPE slipped past that (fixed 2026-08-12): the
    parse succeeded so the except never fired, and `extracted.get(...)` then
    raised AttributeError from outside the try. A model asked for an object
    answering with a top-level list is a routine occurrence, not an exotic one.
    """

    def _fake_reply(self, monkeypatch, raw: str) -> None:
        import genesis_agent.brain as bm

        class _Fake:
            def __init__(self, *a, **kw) -> None:
                pass

            def complete(self, messages, **kw):
                return type("O", (object,), {"raw_text": raw, "code": "",
                                              "usage": None, "tool_calls": None})
        monkeypatch.setattr(bm, "Brain", _Fake)

    def test_a_top_level_list_does_not_raise(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(kg, "GRAPH_PATH", tmp_path / "g.json")
        self._fake_reply(monkeypatch, '[{"name": "AuthToken", "type": "x"}]')
        graph = kg.compact_and_graph_memory("user: hello")
        assert graph == {"entities": {}, "relations": [], "states": {}}

    def test_a_bare_string_does_not_raise(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(kg, "GRAPH_PATH", tmp_path / "g.json")
        self._fake_reply(monkeypatch, '"just a string"')
        assert kg.compact_and_graph_memory("user: hello")["entities"] == {}

    def test_an_error_reply_does_not_raise(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(kg, "GRAPH_PATH", tmp_path / "g.json")
        self._fake_reply(monkeypatch, "Error: цялата верига е изчерпана")
        assert kg.compact_and_graph_memory("user: hello")["entities"] == {}

    def test_a_correct_object_still_grows_the_graph(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(kg, "GRAPH_PATH", tmp_path / "g.json")
        self._fake_reply(monkeypatch, '{"entities":[{"name":"AuthToken","type":"symbol"}],'
                                      '"relations":[],"states":[]}')
        graph = kg.compact_and_graph_memory("user: hello")
        assert "authtoken" in graph["entities"]

    def test_an_unwritable_graph_path_does_not_raise(self, tmp_path, monkeypatch) -> None:
        """_save() does a plain write_text; a full disk or a bad path must not
        turn into an exception out of a function documented as fail-open."""
        monkeypatch.setattr(kg, "GRAPH_PATH", tmp_path / "g.json")
        self._fake_reply(monkeypatch, '{"entities":[{"name":"X","type":"y"}],'
                                      '"relations":[],"states":[]}')

        def _boom(_graph):
            raise OSError("no space left on device")
        monkeypatch.setattr(kg, "_save", _boom)

        graph = kg.compact_and_graph_memory("user: hello")
        assert "x" in graph["entities"]  # extraction survived, only the write failed
