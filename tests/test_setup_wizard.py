"""genesis_agent.setup_wizard — `genesis setup`, the first thing a new user
runs, and zero coverage before this file. `run()` itself is a long chain of
input() prompts and is exercised by hand, not here; what these tests protect
is the logic underneath it that actually decides whether a key works —
`_test_key`'s HTTP-status branching and `_write_private`'s file permissions.
A wrong verdict here either tells someone their working key is broken, or
worse, tells them a dead key is fine.
"""
from __future__ import annotations

import stat

import pytest

from genesis_agent import setup_wizard as sw


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class TestTestKey:
    def test_200_is_ok(self, monkeypatch) -> None:
        monkeypatch.setattr(sw.requests, "post", lambda *a, **kw: _FakeResponse(200))
        ok, _why = sw._test_key("https://x", "key", "m")
        assert ok is True

    def test_401_is_not_ok(self, monkeypatch) -> None:
        monkeypatch.setattr(sw.requests, "post", lambda *a, **kw: _FakeResponse(401))
        ok, why = sw._test_key("https://x", "key", "m")
        assert ok is False
        assert "401" in why

    def test_403_is_not_ok_and_mentions_region(self, monkeypatch) -> None:
        monkeypatch.setattr(sw.requests, "post", lambda *a, **kw: _FakeResponse(403))
        ok, why = sw._test_key("https://x", "key", "m")
        assert ok is False
        assert "регион" in why

    def test_402_is_not_ok_no_credit(self, monkeypatch) -> None:
        monkeypatch.setattr(sw.requests, "post", lambda *a, **kw: _FakeResponse(402))
        ok, _why = sw._test_key("https://x", "key", "m")
        assert ok is False

    def test_429_is_ok_key_valid_but_rate_limited(self, monkeypatch) -> None:
        """A 429 means the key authenticated — the quota, not the key, is the
        problem. Marking this 'broken' would make a wizard reject good keys
        during any burst of setup traffic."""
        monkeypatch.setattr(sw.requests, "post", lambda *a, **kw: _FakeResponse(429))
        ok, _why = sw._test_key("https://x", "key", "m")
        assert ok is True

    def test_network_error_is_not_ok(self, monkeypatch) -> None:
        def _raise(*a, **kw):
            raise sw.requests.RequestException("no route to host")
        monkeypatch.setattr(sw.requests, "post", _raise)
        ok, why = sw._test_key("https://x", "key", "m")
        assert ok is False
        assert "мрежова" in why

    def test_404_with_bad_key_on_models_probe_is_not_ok(self, monkeypatch) -> None:
        """404 alone is ambiguous (dead key vs. retired probe model) — the
        /models fallback is what disambiguates it."""
        def fake_post(*a, **kw):
            return _FakeResponse(404)

        def fake_get(url, headers=None, timeout=None):
            return _FakeResponse(401)

        monkeypatch.setattr(sw.requests, "post", fake_post)
        monkeypatch.setattr(sw.requests, "get", fake_get)
        ok, why = sw._test_key("https://x", "key", "retired-model")
        assert ok is False
        assert "отхвърлен" in why

    def test_404_with_good_key_on_models_probe_is_ok(self, monkeypatch) -> None:
        """404 + a /models call that DOES authenticate → the key is fine, the
        probe model is just gone. Must not be reported as a bad key."""
        monkeypatch.setattr(sw.requests, "post", lambda *a, **kw: _FakeResponse(404))
        monkeypatch.setattr(sw.requests, "get", lambda url, headers=None, timeout=None: _FakeResponse(200))
        ok, why = sw._test_key("https://x", "key", "retired-model")
        assert ok is True
        assert "вече не съществува" in why

    def test_unexpected_status_is_not_ok_and_includes_code(self, monkeypatch) -> None:
        monkeypatch.setattr(sw.requests, "post", lambda *a, **kw: _FakeResponse(500, "boom"))
        ok, why = sw._test_key("https://x", "key", "m")
        assert ok is False
        assert "500" in why


class TestTestAnthropicKey:
    @staticmethod
    def _fake_response(status_code: int):
        httpx = pytest.importorskip("httpx")
        return httpx.Response(status_code, request=httpx.Request("POST", "https://x.test"))

    def test_authentication_error_is_not_ok(self, monkeypatch) -> None:
        anthropic = pytest.importorskip("anthropic")

        class _FakeClient:
            def __init__(self, **kw) -> None:
                self.messages = self

            def create(self, **kw):
                raise anthropic.AuthenticationError(
                    message="bad key", response=self._resp, body=None
                )

        _FakeClient._resp = self._fake_response(401)
        monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)
        ok, why = sw._test_anthropic_key("sk-bad", "claude-opus-5")
        assert ok is False
        assert "отхвърлен" in why

    def test_rate_limit_error_is_ok_key_works(self, monkeypatch) -> None:
        anthropic = pytest.importorskip("anthropic")

        class _FakeClient:
            def __init__(self, **kw) -> None:
                self.messages = self

            def create(self, **kw):
                raise anthropic.RateLimitError(message="slow down", response=self._resp, body=None)

        _FakeClient._resp = self._fake_response(429)
        monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)
        ok, _why = sw._test_anthropic_key("sk-good", "claude-opus-5")
        assert ok is True

    def test_missing_package_is_not_ok_with_install_hint(self, monkeypatch) -> None:
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "anthropic":
                raise ImportError("no module named anthropic")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        ok, why = sw._test_anthropic_key("sk-x", "claude-opus-5")
        assert ok is False
        assert "pip install anthropic" in why


class TestWritePrivate:
    def test_file_is_written_with_content(self, tmp_path) -> None:
        p = tmp_path / "secret.env"
        sw._write_private(p, "HELLO=world\n")
        assert p.read_text(encoding="utf-8") == "HELLO=world\n"

    def test_file_is_owner_only_readable(self, tmp_path) -> None:
        p = tmp_path / "secret.env"
        sw._write_private(p, "x")
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == 0o600

    def test_overwrites_existing_file_content(self, tmp_path) -> None:
        p = tmp_path / "secret.env"
        p.write_text("OLD=stuff\n" * 500, encoding="utf-8")  # longer than the new content
        sw._write_private(p, "NEW=short\n")
        assert p.read_text(encoding="utf-8") == "NEW=short\n"


class TestExisting:
    def test_real_env_var_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("SOME_KEY", "from-environ")
        monkeypatch.setattr(sw, "read_env_files", lambda var: "from-dotenv")
        assert sw._existing("SOME_KEY") == "from-environ"

    def test_falls_back_to_env_files_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("SOME_KEY", raising=False)
        monkeypatch.setattr(sw, "read_env_files", lambda var: "from-dotenv")
        assert sw._existing("SOME_KEY") == "from-dotenv"

    def test_empty_string_when_nowhere(self, monkeypatch) -> None:
        monkeypatch.delenv("SOME_KEY", raising=False)
        monkeypatch.setattr(sw, "read_env_files", lambda var: None)
        assert sw._existing("SOME_KEY") == ""
