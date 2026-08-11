"""genesis_agent.paths — the secret-resolution precedence chain (real env >
project .env > ~/.genesis/.env > config.yaml). Getting this wrong either
leaks a key into the wrong place or makes a configured key invisible."""
from __future__ import annotations

from genesis_agent import paths


def test_strip_inline_comment_after_whitespace() -> None:
    assert paths._strip_inline_comment("value  # a note") == "value"


def test_strip_inline_comment_keeps_hash_inside_value() -> None:
    """A `#` with no preceding whitespace is part of the value, not a comment
    — otherwise a real secret containing '#' gets silently truncated."""
    assert paths._strip_inline_comment("sk-abc#def") == "sk-abc#def"


def test_strip_inline_comment_no_comment() -> None:
    assert paths._strip_inline_comment("plain-value") == "plain-value"


def test_read_env_files_finds_key(tmp_path, monkeypatch) -> None:
    envf = tmp_path / ".env"
    envf.write_text("FOO=bar\nBAZ=qux  # comment\n")
    monkeypatch.setattr(paths, "ENV_FILES", (str(envf),))
    assert paths.read_env_files("FOO") == "bar"
    assert paths.read_env_files("BAZ") == "qux"


def test_read_env_files_handles_export_prefix_and_quotes(tmp_path, monkeypatch) -> None:
    envf = tmp_path / ".env"
    envf.write_text('export TOKEN="secret-value"\n')
    monkeypatch.setattr(paths, "ENV_FILES", (str(envf),))
    assert paths.read_env_files("TOKEN") == "secret-value"


def test_read_env_files_missing_key_returns_none(tmp_path, monkeypatch) -> None:
    envf = tmp_path / ".env"
    envf.write_text("FOO=bar\n")
    monkeypatch.setattr(paths, "ENV_FILES", (str(envf),))
    assert paths.read_env_files("MISSING") is None


def test_read_env_files_skips_comments_and_blank_lines(tmp_path, monkeypatch) -> None:
    envf = tmp_path / ".env"
    envf.write_text("# a full-line comment\n\nFOO=bar\n")
    monkeypatch.setattr(paths, "ENV_FILES", (str(envf),))
    assert paths.read_env_files("FOO") == "bar"


def test_get_secret_real_env_wins_over_env_files(tmp_path, monkeypatch) -> None:
    envf = tmp_path / ".env"
    envf.write_text("FOO=from_file\n")
    monkeypatch.setattr(paths, "ENV_FILES", (str(envf),))
    monkeypatch.setenv("FOO", "from_real_env")
    assert paths.get_secret("FOO") == "from_real_env"


def test_get_secret_falls_back_to_env_file(tmp_path, monkeypatch) -> None:
    envf = tmp_path / ".env"
    envf.write_text("FOO=from_file\n")
    monkeypatch.setattr(paths, "ENV_FILES", (str(envf),))
    monkeypatch.delenv("FOO", raising=False)
    assert paths.get_secret("FOO") == "from_file"


def test_get_secret_returns_default_when_nowhere(monkeypatch) -> None:
    monkeypatch.setattr(paths, "ENV_FILES", ())
    monkeypatch.delenv("NOPE", raising=False)
    assert paths.get_secret("NOPE", default="fallback") == "fallback"


def test_project_local_env_wins_over_home_env(tmp_path, monkeypatch) -> None:
    project_env = tmp_path / "project.env"
    home_env = tmp_path / "home.env"
    project_env.write_text("FOO=project\n")
    home_env.write_text("FOO=home\n")
    monkeypatch.setattr(paths, "ENV_FILES", (str(project_env), str(home_env)))
    monkeypatch.delenv("FOO", raising=False)
    assert paths.get_secret("FOO") == "project"


class TestKeyNamesMatchExactly:
    """Names must match in full, never by prefix (regression guard, 2026-08-12).

    discord_bot.py used to carry its own copy of this lookup that compared
    with `line.startswith(key)`. That is not hypothetical sloppiness: this
    project documents a numbered-key convention (`<KEY>_2`..`_10`, see
    brain.py), so an operator following it has exactly the kind of .env where
    a prefix match picks the wrong line — silently handing the Discord
    owner-lock a different user ID, or the bot a different token. That copy
    now delegates here; these pin the behavior it depends on.
    """

    def test_a_longer_similarly_named_key_is_not_matched(self, tmp_path, monkeypatch) -> None:
        envf = tmp_path / ".env"
        envf.write_text("GENESIS_DISCORD_OWNER_ID_2=999\nGENESIS_DISCORD_OWNER_ID=42\n")
        monkeypatch.setattr(paths, "ENV_FILES", (str(envf),))
        monkeypatch.delenv("GENESIS_DISCORD_OWNER_ID", raising=False)
        assert paths.read_env_files("GENESIS_DISCORD_OWNER_ID") == "42"

    def test_missing_bare_key_is_not_satisfied_by_a_numbered_one(self, tmp_path, monkeypatch) -> None:
        envf = tmp_path / ".env"
        envf.write_text("TOKEN_2=numbered-only\n")
        monkeypatch.setattr(paths, "ENV_FILES", (str(envf),))
        monkeypatch.delenv("TOKEN", raising=False)
        assert paths.read_env_files("TOKEN") is None

    def test_a_shorter_key_is_not_matched_by_a_longer_query(self, tmp_path, monkeypatch) -> None:
        envf = tmp_path / ".env"
        envf.write_text("API=short\n")
        monkeypatch.setattr(paths, "ENV_FILES", (str(envf),))
        monkeypatch.delenv("API_KEY", raising=False)
        assert paths.read_env_files("API_KEY") is None
