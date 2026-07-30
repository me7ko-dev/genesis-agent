"""genesis_agent.discord_bot — the only other user-facing frontend besides the
terminal/GUI, previously untested. Priority: the owner-lock fail-closed
behavior (a bot that runs real tools on the user's machine must never answer
a stranger — see the design note above _OWNER_ID in discord_bot.py) and the
pure _chunk() splitter. discord.py is an optional dependency; this whole
file is skipped if it isn't installed."""
from __future__ import annotations

import asyncio

import pytest

discord = pytest.importorskip("discord")

from genesis_agent import discord_bot as db


class TestChunk:
    def test_short_text_is_a_single_chunk(self) -> None:
        assert db._chunk("hello") == ["hello"]

    def test_blank_text_becomes_placeholder(self) -> None:
        assert db._chunk("   ") == ["…"]

    def test_splits_on_line_boundaries_under_the_limit(self) -> None:
        text = ("a" * 1500 + "\n") * 3
        chunks = db._chunk(text, size=2000)
        assert all(len(c) <= 2000 for c in chunks)
        assert "".join(chunks) == text.strip() + "\n" or "".join(chunks).strip() == text.strip()

    def test_single_line_longer_than_limit_is_hard_split(self) -> None:
        text = "x" * 5000
        chunks = db._chunk(text, size=2000)
        assert [len(c) for c in chunks] == [2000, 2000, 1000]
        assert "".join(chunks) == text

    def test_exactly_at_limit_is_one_chunk(self) -> None:
        text = "a" * 2000
        assert db._chunk(text, size=2000) == [text]


class _FakeUser:
    def __init__(self, user_id: int, *, bot: bool = False) -> None:
        self.id = user_id
        self.bot = bot

    def __eq__(self, other) -> bool:
        return isinstance(other, _FakeUser) and self.id == other.id and self.bot == other.bot

    def __hash__(self) -> int:
        return hash((self.id, self.bot))


class _FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeChannel:
    def __init__(self, channel_id: int = 1) -> None:
        self.id = channel_id
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    def typing(self):
        return _FakeTyping()


class _FakeMessage:
    def __init__(self, *, author: _FakeUser, content: str, guild=None, channel=None, mentions=None) -> None:
        self.author = author
        self.content = content
        self.guild = guild
        self.channel = channel or _FakeChannel()
        self.mentions = mentions or []


OWNER_ID = 42
STRANGER_ID = 999


def _client(monkeypatch) -> db.GenesisClient:
    # discord.Client.user is a read-only property backed by internal ws
    # state; patch it at the class level for the duration of the test.
    monkeypatch.setattr(db.GenesisClient, "user", property(lambda self: _FakeUser(1)))
    return db.GenesisClient()


class TestOwnerLockFailClosed:
    """The exact contract from the design note above _OWNER_ID: without an
    owner configured, or from anyone but the owner, the bot must stay
    silent — not error, not partially respond, silent."""

    def test_no_owner_configured_ignores_everyone(self, monkeypatch) -> None:
        monkeypatch.setattr(db, "_OWNER_ID", None)
        client = _client(monkeypatch)
        msg = _FakeMessage(author=_FakeUser(OWNER_ID), content="hello", guild=None)
        asyncio.run(client.on_message(msg))
        assert msg.channel.sent == []

    def test_owner_configured_ignores_a_stranger(self, monkeypatch) -> None:
        monkeypatch.setattr(db, "_OWNER_ID", OWNER_ID)
        client = _client(monkeypatch)
        msg = _FakeMessage(author=_FakeUser(STRANGER_ID), content="hello", guild=None)
        asyncio.run(client.on_message(msg))
        assert msg.channel.sent == []

    def test_ignores_messages_from_bots_including_self(self, monkeypatch) -> None:
        monkeypatch.setattr(db, "_OWNER_ID", OWNER_ID)
        client = _client(monkeypatch)
        msg = _FakeMessage(author=_FakeUser(OWNER_ID, bot=True), content="hello", guild=None)
        asyncio.run(client.on_message(msg))
        assert msg.channel.sent == []

    def test_owner_dm_gets_a_reply(self, monkeypatch) -> None:
        monkeypatch.setattr(db, "_OWNER_ID", OWNER_ID)
        client = _client(monkeypatch)
        monkeypatch.setattr(client, "_brain_reply", lambda channel_id, text, enable_tools=False: ("hi!", 0))
        msg = _FakeMessage(author=_FakeUser(OWNER_ID), content="hello", guild=None)
        asyncio.run(client.on_message(msg))
        assert msg.channel.sent == ["hi!"]

    def test_owner_in_a_guild_channel_without_mention_or_prefix_is_not_addressed(self, monkeypatch) -> None:
        monkeypatch.setattr(db, "_OWNER_ID", OWNER_ID)
        client = _client(monkeypatch)
        msg = _FakeMessage(author=_FakeUser(OWNER_ID), content="just chatting", guild=object())
        asyncio.run(client.on_message(msg))
        assert msg.channel.sent == []

    def test_owner_in_a_guild_channel_with_prefix_is_addressed(self, monkeypatch) -> None:
        monkeypatch.setattr(db, "_OWNER_ID", OWNER_ID)
        client = _client(monkeypatch)
        monkeypatch.setattr(client, "_brain_reply", lambda channel_id, text, enable_tools=False: (text, 0))
        msg = _FakeMessage(author=_FakeUser(OWNER_ID), content="genesis ping", guild=object())
        asyncio.run(client.on_message(msg))
        assert msg.channel.sent == ["ping"]


class TestCommands:
    def test_help_lists_commands_without_touching_the_brain(self, monkeypatch) -> None:
        monkeypatch.setattr(db, "_OWNER_ID", OWNER_ID)
        client = _client(monkeypatch)

        def _boom(*a, **kw):
            raise AssertionError("!help must not call the brain")

        monkeypatch.setattr(client, "_brain_reply", _boom)
        msg = _FakeMessage(author=_FakeUser(OWNER_ID), content="!help", guild=None)
        asyncio.run(client.on_message(msg))
        assert len(msg.channel.sent) == 1
        assert "!mission" in msg.channel.sent[0]

    def test_clear_empties_only_that_channels_history(self, monkeypatch) -> None:
        monkeypatch.setattr(db, "_OWNER_ID", OWNER_ID)
        client = _client(monkeypatch)
        chan_a, chan_b = _FakeChannel(1), _FakeChannel(2)
        client._history[chan_a.id].append({"role": "user", "content": "old"})
        client._history[chan_b.id].append({"role": "user", "content": "keep me"})

        msg = _FakeMessage(author=_FakeUser(OWNER_ID), content="!clear", guild=None, channel=chan_a)
        asyncio.run(client.on_message(msg))

        assert chan_a.id not in client._history
        assert list(client._history[chan_b.id]) == [{"role": "user", "content": "keep me"}]

    def test_stop_sets_the_global_kill_switch(self, monkeypatch) -> None:
        from genesis_agent.config import stop_event
        monkeypatch.setattr(db, "_OWNER_ID", OWNER_ID)
        client = _client(monkeypatch)
        stop_event.clear()
        try:
            msg = _FakeMessage(author=_FakeUser(OWNER_ID), content="!stop", guild=None)
            asyncio.run(client.on_message(msg))
            assert stop_event.is_set()
        finally:
            stop_event.clear()

    def test_mission_runs_in_a_thread_and_reports_the_outcome(self, monkeypatch) -> None:
        monkeypatch.setattr(db, "_OWNER_ID", OWNER_ID)
        client = _client(monkeypatch)
        monkeypatch.setattr(client, "_run_mission", lambda goal: f"done: {goal}")
        msg = _FakeMessage(author=_FakeUser(OWNER_ID), content="!mission build a thing", guild=None)
        asyncio.run(client.on_message(msg))
        assert msg.channel.sent[0].startswith("🚀 Стартирам мисия")
        assert msg.channel.sent[1] == "done: build a thing"


class TestRunMission:
    def test_success_reports_rounds_and_skill_path(self, monkeypatch) -> None:
        from genesis_agent.autonomous_loop import LoopOutcome
        outcome = LoopOutcome(success=True, rounds=3, skill_path="a/b.md", last_stdout="",
                               last_stderr="", storage_note="", dna_audit={})
        monkeypatch.setattr("genesis_agent.autonomous_loop.run_autonomous_loop", lambda *a, **kw: outcome)
        client = db.GenesisClient()
        result = client._run_mission("do a thing")
        assert "✅" in result
        assert "3" in result
        assert "a/b.md" in result

    def test_failure_reports_last_stderr(self, monkeypatch) -> None:
        from genesis_agent.autonomous_loop import LoopOutcome
        outcome = LoopOutcome(success=False, rounds=6, skill_path=None, last_stdout="",
                               last_stderr="boom", storage_note="", dna_audit={})
        monkeypatch.setattr("genesis_agent.autonomous_loop.run_autonomous_loop", lambda *a, **kw: outcome)
        client = db.GenesisClient()
        result = client._run_mission("do a thing")
        assert "❌" in result
        assert "boom" in result

    def test_exception_is_caught_and_reported_not_raised(self, monkeypatch) -> None:
        def _boom(*a, **kw):
            raise RuntimeError("provider down")

        monkeypatch.setattr("genesis_agent.autonomous_loop.run_autonomous_loop", _boom)
        client = db.GenesisClient()
        result = client._run_mission("do a thing")
        assert "provider down" in result


class TestSleepInterruptible:
    def test_stop_event_wakes_it_up_before_the_full_duration(self) -> None:
        import threading
        import time

        stop_event = threading.Event()

        def _setter():
            time.sleep(0.05)
            stop_event.set()

        threading.Thread(target=_setter).start()
        started = time.monotonic()
        db.GenesisClient._sleep_interruptible(30, stop_event)
        # One 5s polling step, not the full 30s duration.
        assert time.monotonic() - started < 10
