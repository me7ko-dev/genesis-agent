"""genesis_agent.notifier — where mission results leave the machine.

Untested until now, and it carried its own .env reader with the same two
defects discord_bot.py had (both fixed 2026-08-12, both now delegating to
paths.read_env_files): prefix matching instead of exact key matching, and no
inline-comment stripping. Here the first one means notifications addressed to
somebody else's webhook — not a misconfiguration, a delivery to the wrong
place — and the second means a URL with a comment glued to it.
"""
from __future__ import annotations

import pytest

from genesis_agent import notifier


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Nothing in this file may actually POST anywhere."""
    def _boom(*a, **kw):
        raise AssertionError("notifier must not open a real connection in tests")
    monkeypatch.setattr(notifier.urllib.request, "urlopen", _boom)


class TestFromEnvFilesMatchesExactly:
    def test_a_longer_similarly_named_key_is_not_matched(self, tmp_path, monkeypatch) -> None:
        envf = tmp_path / ".env"
        envf.write_text(
            "GENESIS_DISCORD_WEBHOOK_2=https://discord.com/api/webhooks/WRONG\n"
            "GENESIS_DISCORD_WEBHOOK=https://discord.com/api/webhooks/RIGHT\n"
        )
        monkeypatch.setattr("genesis_agent.paths.ENV_FILES", (str(envf),))
        assert notifier._from_env_files("GENESIS_DISCORD_WEBHOOK").endswith("RIGHT")

    def test_missing_key_is_not_satisfied_by_a_numbered_one(self, tmp_path, monkeypatch) -> None:
        envf = tmp_path / ".env"
        envf.write_text("GENESIS_TELEGRAM_TOKEN_OLD=stale-token\n")
        monkeypatch.setattr("genesis_agent.paths.ENV_FILES", (str(envf),))
        assert notifier._from_env_files("GENESIS_TELEGRAM_TOKEN") == ""

    def test_inline_comment_is_stripped_from_the_value(self, tmp_path, monkeypatch) -> None:
        envf = tmp_path / ".env"
        envf.write_text("GENESIS_DISCORD_WEBHOOK=https://discord.com/api/webhooks/x  # my hook\n")
        monkeypatch.setattr("genesis_agent.paths.ENV_FILES", (str(envf),))
        assert notifier._from_env_files("GENESIS_DISCORD_WEBHOOK") == \
            "https://discord.com/api/webhooks/x"

    def test_missing_file_is_empty_not_an_error(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.paths.ENV_FILES", (str(tmp_path / "nope.env"),))
        assert notifier._from_env_files("ANYTHING") == ""


# resolve_setting()'s own precedence (real env > .env > config.yaml) is
# deliberately NOT tested here: conftest.py stubs it to "" for every test in
# the suite, because pytest runs were once posting fake mission results to the
# operator's real Discord webhook. Poking a hole in that safety net to test
# four lines of fallback would be a bad trade — and paths.get_secret, which
# _from_env_files now delegates to, already has that precedence covered in
# test_paths.py.


class TestSendMessageSkipsUnconfiguredChannels:
    """An unconfigured channel reports False and attempts no request at all —
    the autouse fixture above turns any real attempt into a failure. This also
    pins down what conftest's global stub relies on: with resolve_setting
    empty, nothing must reach the network."""

    def test_no_credentials_means_no_requests(self) -> None:
        results = notifier.send_message("hello")
        assert results == {"telegram": False, "discord": False}

    def test_only_the_requested_channel_is_reported(self) -> None:
        assert notifier.send_message("hello", channels=["discord"]) == {"discord": False}

    def test_notify_is_false_when_nothing_is_configured(self) -> None:
        assert notifier.notify("hello") is False

    def test_notify_never_raises_when_a_channel_blows_up(self, monkeypatch) -> None:
        def _explode(*a, **kw):
            raise RuntimeError("network on fire")
        monkeypatch.setattr(notifier, "send_message", _explode)
        assert notifier.notify("anything") is False
