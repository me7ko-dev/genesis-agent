"""scripts/cache_replay.py — без мрежа: формата на историята и двата начина."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("cache_replay", ROOT / "scripts" / "cache_replay.py")
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)


def test_every_tool_result_answers_the_call_before_it() -> None:
    msgs = cr._history("shrink-1", 5)
    for call, result in zip(msgs[2::2], msgs[3::2], strict=True):
        assert result["tool_call_id"] == call["tool_calls"][0]["id"]


def test_the_two_shapes_do_not_share_a_cache_prefix() -> None:
    a, b = cr._history("shrink-1234567", 1), cr._history("append-7654321", 1)
    assert a[0]["content"][:60] != b[0]["content"][:60]


def test_both_shapes_are_sent_and_counted(monkeypatch, capsys) -> None:
    seen: list[int] = []

    def fake_send(provider, model, key, messages):
        seen.append(sum(len(str(m.get("content") or "")) for m in messages))
        return {"prompt_tokens": 1000, "cached_read_tokens": 600}

    monkeypatch.setattr(cr, "_send", fake_send)
    monkeypatch.setattr(cr.time, "sleep", lambda s: None)
    assert cr.main(["--rounds", "12", "ollama_cloud=m"]) == 0
    out = capsys.readouterr().out
    assert "shrink: заявки 12, вход 12000, от кеша 7200 (60%)" in out
    assert "append: заявки 12" in out
    assert seen[11] < seen[23], "свиването праща по-малко от пълната история"


def test_a_bad_spec_is_refused() -> None:
    assert cr.main(["no-such=m"]) == 2
