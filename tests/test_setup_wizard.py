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
import sys

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

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="NTFS has no POSIX mode bits — os.chmod(0o600) is not meaningfully "
               "enforceable there, it is a real platform difference, not a bug.",
    )
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


class TestPruneBackups:
    """Every `genesis setup` run writes a dated backup, and every backup is a
    full copy of every API key in the file. Nothing pruned them, so a dozen
    setup runs left a dozen complete copies of the user's secrets sitting in
    ~/.genesis indefinitely (2026-08-12). Keeping the recent few preserves the
    reason backups exist — a mistake stays recoverable — without the sprawl.
    """

    def _make(self, tmp_path, *stamps) -> None:
        for s in stamps:
            (tmp_path / f".env.backup-{s}").write_text("KEY=secret\n", encoding="utf-8")

    def test_keeps_only_the_newest_n(self, tmp_path) -> None:
        self._make(tmp_path, *[f"2026081{i}-120000" for i in range(9)])
        sw._prune_backups(tmp_path, keep=3)
        left = sorted(p.name for p in tmp_path.glob(".env.backup-*"))
        assert left == [".env.backup-20260816-120000",
                        ".env.backup-20260817-120000",
                        ".env.backup-20260818-120000"]

    def test_under_the_limit_nothing_is_removed(self, tmp_path) -> None:
        self._make(tmp_path, "20260810-120000", "20260811-120000")
        sw._prune_backups(tmp_path, keep=5)
        assert len(list(tmp_path.glob(".env.backup-*"))) == 2

    def test_the_live_env_file_is_never_touched(self, tmp_path) -> None:
        (tmp_path / ".env").write_text("LIVE=keys\n", encoding="utf-8")
        self._make(tmp_path, *[f"2026081{i}-120000" for i in range(9)])
        sw._prune_backups(tmp_path, keep=1)
        assert (tmp_path / ".env").read_text(encoding="utf-8") == "LIVE=keys\n"

    def test_no_backups_at_all_is_a_no_op(self, tmp_path) -> None:
        sw._prune_backups(tmp_path, keep=3)  # must not raise

    def test_missing_directory_is_a_no_op(self, tmp_path) -> None:
        sw._prune_backups(tmp_path / "does-not-exist", keep=3)  # must not raise

    def test_ordering_is_by_name_not_mtime(self, tmp_path) -> None:
        """Names are timestamped, so lexical order IS chronological — copying
        the directory between machines does not preserve mtime, and sorting by
        it would then delete the wrong ones."""
        import os
        import time
        self._make(tmp_path, "20260801-090000", "20260820-090000")
        # Make the OLDER-named file look newest by mtime.
        os.utime(tmp_path / ".env.backup-20260801-090000", (time.time(), time.time()))
        sw._prune_backups(tmp_path, keep=1)
        left = [p.name for p in tmp_path.glob(".env.backup-*")]
        assert left == [".env.backup-20260820-090000"]


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


# ── run(): целият съветник по сценарий, без мрежа и без истинския .env ──────
_FREE = [("A_KEY", "A", "https://a", "https://a/v1", "m"),
         ("B_KEY", "B", "https://b", "https://b/v1", "m"),
         ("C_KEY", "C", "https://c", "https://c/v1", "m"),
         ("D_KEY", "D", "https://d", "https://d/v1", "m"),
         ("E_KEY", "E", "https://e", "https://e/v1", "m")]
_PAID = [("P_KEY", "P", "https://p", "native://anthropic", "m"),
         ("Q_KEY", "Q", "https://q", "https://q/v1", "m")]


def _wizard(monkeypatch, tmp_path, existing: dict, answers: list[str], good: set[str]):
    env = tmp_path / ".env"
    asked: list[str] = []
    script = iter(answers)

    def _prompt(text: str) -> str:
        asked.append(text)
        return next(script)

    tested: list[str] = []

    def _test(*args):
        key = args[1] if len(args) == 3 else args[0]
        tested.append(key)
        return key in good, "ok" if key in good else "лош"

    monkeypatch.setattr(sw, "PROVIDERS", _FREE)
    monkeypatch.setattr(sw, "PAID_PROVIDERS", _PAID)
    monkeypatch.setattr(sw, "ENV_FILE", env)
    monkeypatch.setattr(sw, "ensure_genesis_home", lambda: None)
    monkeypatch.setattr(sw, "_existing", lambda var: existing.get(var, ""))
    monkeypatch.setattr(sw, "_prompt", _prompt)
    monkeypatch.setattr(sw, "_test_key", _test)
    monkeypatch.setattr(sw, "_test_anthropic_key", _test)
    code = sw.run()
    written = {}
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                k, _, v = line.partition("=")
                written[k] = v
    return code, written, asked, tested


def test_wizard_free_keys_every_branch(monkeypatch, tmp_path, capsys) -> None:
    existing = {"A_KEY": "a-good", "B_KEY": "b-good", "C_KEY": "c-bad", "D_KEY": "d-bad"}
    answers = ["",           # A: работещ, Enter → запазен
               "b-new",      # B: работещ, заменен с нов работещ
               "",           # C: счупен, Enter → оставен
               "d-new", "y",  # D: счупен → нов, пак лош, „запиши въпреки това“
               "e-new",      # E: нов работещ
               ""]           # платени: [y/N] → не
    code, written, asked, tested = _wizard(monkeypatch, tmp_path, existing, answers,
                                           {"a-good", "b-good", "b-new", "e-new"})
    assert code == 0
    assert written == {"A_KEY": "a-good", "B_KEY": "b-new", "C_KEY": "c-bad",
                       "D_KEY": "d-new", "E_KEY": "e-new"}
    assert tested == ["a-good", "b-good", "b-new", "c-bad", "d-bad", "d-new", "e-new"]
    assert asked[-1] == "  Да настроя ли платен ключ? [y/N] "
    out = capsys.readouterr().out
    assert "3 работещи доставчика." in out and "този доставчик няма да работи" in out


def test_wizard_a_bad_new_key_is_dropped_on_no(monkeypatch, tmp_path) -> None:
    code, written, _, _ = _wizard(monkeypatch, tmp_path, {},
                                  ["a-new", "n", "", "", "", "e-new", "", ""], {"e-new"})
    assert code == 0 and written == {"E_KEY": "e-new"}


def test_wizard_paid_defaults_to_yes_when_one_exists(monkeypatch, tmp_path) -> None:
    answers = ["", "", "", "", "e-new",
               "",           # платени: [Y/n] → да
               "",           # P: има, Enter → запазен
               "q-new", "n"]  # Q: нов, лош, не
    code, written, asked, tested = _wizard(monkeypatch, tmp_path, {"P_KEY": "p-old"}, answers,
                                           {"e-new"})
    assert code == 0 and written == {"E_KEY": "e-new", "P_KEY": "p-old"}
    assert "  Да настроя ли платен ключ? [Y/n] " in asked
    assert tested == ["e-new", "p-old", "q-new"]


def test_wizard_paid_refused_still_carries_the_existing_key(monkeypatch, tmp_path) -> None:
    code, written, _, _ = _wizard(monkeypatch, tmp_path, {"P_KEY": "p-old"},
                                  ["", "", "", "", "", "n"], set())
    assert code == 0 and written == {"P_KEY": "p-old"}


def test_wizard_paid_new_key_saved_when_good(monkeypatch, tmp_path) -> None:
    code, written, _, _ = _wizard(monkeypatch, tmp_path, {},
                                  ["", "", "", "", "", "y", "p-new", ""], {"p-new"})
    assert code == 0 and written == {"P_KEY": "p-new"}


def test_wizard_nothing_set_writes_nothing(monkeypatch, tmp_path) -> None:
    code, written, _, _ = _wizard(monkeypatch, tmp_path, {}, [""] * 6, set())
    assert code == 1 and written == {} and not (tmp_path / ".env").exists()


def test_wizard_keeps_unmanaged_settings_and_backs_up(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env").write_text("# стар\nexport OTHER=1\nA_KEY=old\nEMPTY=\n", encoding="utf-8")
    code, written, _, _ = _wizard(monkeypatch, tmp_path, {},
                                  ["a-new", "", "", "", "", ""], {"a-new"})
    assert code == 0 and written == {"A_KEY": "a-new", "OTHER": "1"}
    backups = list(tmp_path.glob(".env.backup-*"))
    assert len(backups) == 1 and "A_KEY=old" in backups[0].read_text(encoding="utf-8")
