"""`genesis keys` — ключовете от компютъра към Genesis на телефона."""
from __future__ import annotations

import os
import sys

import pytest

from genesis_agent import keys_transfer as kt

GROQ = "gsk_" + "A1b2" * 12
GROQ2 = "gsk_" + "Z9y8" * 12
ROUTER = "sk-or-v1-" + "0f" * 32
OLLAMA = "0123456789abcdef0123456789abcdef.AbCdEfGhIjKlMnOpQrSt"


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    path = tmp_path / ".genesis" / ".env"
    monkeypatch.setattr("genesis_agent.paths.ENV_FILE", path)
    monkeypatch.setattr("genesis_agent.paths.ENV_FILES", (str(path),))
    monkeypatch.setattr("genesis_agent.paths.GENESIS_HOME", path.parent)
    # Средата е копие без ключове: истинските ключове на машината не влизат в
    # теста, а записаните от save() не излизат от него към следващите тестове.
    monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items() if not kt.is_key_name(k)})
    return path


def test_only_model_provider_keys_travel() -> None:
    for name in ("GROQ_API_KEY", "GROQ_API_KEY_3", "OLLAMA_API_KEY_10", "HF_TOKEN",
                 "GENESIS_TELEGRAM_TOKEN"):
        assert kt.is_key_name(name), name
    # Чужди токени от средата на компютъра (измерено на машината на оператора).
    for name in ("CLAUDE_CODE_MESSAGING_TOKEN", "GENESIS_WAITLIST_TOKEN", "GITHUB_TOKEN",
                 "PATH", "GROQ_API_KEY_11", "GROQ_API_KEY_1"):
        assert not kt.is_key_name(name), name


def test_values_must_fit_one_env_line() -> None:
    assert kt.clean({"GROQ_API_KEY": GROQ, "GROQ_API_KEY_2": "has space inside",
                     "GROQ_API_KEY_3": "x\ny=1", "OLLAMA_API_KEY": "short"}) == {"GROQ_API_KEY": GROQ}


def test_free_text_file_like_the_operators() -> None:
    # Като апита.txt: ключове един под друг, без имена; вторият за същия
    # доставчик става _2 — както в .env на компютъра.
    text = f"{ROUTER}\n\n{GROQ}\n\n{GROQ2}\n\nollama: {OLLAMA}\nsomething {'q' * 30}\n"
    keys, unknown = kt.parse_text(text)
    assert keys == {"OPENROUTER_API_KEY": ROUTER, "GROQ_API_KEY": GROQ,
                    "GROQ_API_KEY_2": GROQ2, "OLLAMA_API_KEY": OLLAMA}
    assert unknown == ["qqqq…qqq"]


def test_named_lines_win_and_repeats_are_dropped() -> None:
    keys, _ = kt.parse_text(f'export GROQ_API_KEY="{GROQ}"\nGROQ_API_KEY_2={GROQ2}\n{GROQ}\n')
    assert keys == {"GROQ_API_KEY": GROQ, "GROQ_API_KEY_2": GROQ2}


def test_from_env_skips_foreign_tokens(env_file, monkeypatch) -> None:
    env_file.parent.mkdir(parents=True)
    env_file.write_text(f"GROQ_API_KEY={GROQ}\nGENESIS_QUALITY=max\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "x" * 40)
    monkeypatch.setenv("OLLAMA_API_KEY_3", OLLAMA)
    assert kt.from_env() == {"GROQ_API_KEY": GROQ, "OLLAMA_API_KEY_3": OLLAMA}


def test_link_round_trip_with_short_names() -> None:
    keys = {"GROQ_API_KEY": GROQ, "GROQ_API_KEY_2": GROQ2, "OLLAMA_API_KEY": OLLAMA,
            "OPENROUTER_API_KEY_3": ROUTER}
    link = kt.encode_link(keys)
    assert link.startswith("genesisremote://keys?gq=")
    assert "gq2=" in link and "or3=" in link and "GROQ" not in link
    assert kt.decode_link(link) == keys


def test_decode_ignores_other_links_and_unknown_codes() -> None:
    assert kt.decode_link(f"genesisremote://pair?gq={GROQ}") == {}
    assert kt.decode_link(f"https://evil.example/keys?gq={GROQ}") == {}
    assert kt.decode_link(f"genesisremote://keys?zz={GROQ}&gq={GROQ}") == {"GROQ_API_KEY": GROQ}


def test_save_merges_into_env_and_the_running_process(env_file, monkeypatch) -> None:
    env_file.parent.mkdir(parents=True)
    env_file.write_text("# мой коментар\nGROQ_API_KEY=old\nGENESIS_QUALITY=max\n", encoding="utf-8")
    fake_gta = type(sys)("genesis_terminal_agent")
    fake_gta.KEYS = {"GROQ_API_KEY": "old", "OLLAMA_API_KEY": ""}
    monkeypatch.setitem(sys.modules, "genesis_terminal_agent", fake_gta)

    saved = kt.save({"GROQ_API_KEY": GROQ, "OLLAMA_API_KEY": OLLAMA, "PATH": "/evil/bin"})

    assert saved == ["GROQ_API_KEY", "OLLAMA_API_KEY"]
    text = env_file.read_text(encoding="utf-8")
    assert "# мой коментар" in text and "GENESIS_QUALITY=max" in text
    assert f"GROQ_API_KEY={GROQ}" in text and "GROQ_API_KEY=old" not in text
    assert f"OLLAMA_API_KEY={OLLAMA}" in text and "PATH" not in text
    assert list(env_file.parent.glob(".env.backup-*"))
    assert os.environ["GROQ_API_KEY"] == GROQ
    assert fake_gta.KEYS == {"GROQ_API_KEY": GROQ, "OLLAMA_API_KEY": OLLAMA}
    if os.name == "posix":
        assert env_file.stat().st_mode & 0o777 == 0o600


def test_import_command_takes_the_link(env_file, capsys) -> None:
    assert kt.main(["import", kt.encode_link({"GROQ_API_KEY": GROQ})]) == 0
    assert f"GROQ_API_KEY={GROQ}" in env_file.read_text(encoding="utf-8")
    assert "GROQ_API_KEY" in capsys.readouterr().out


def test_qr_shows_the_page_then_deletes_it(env_file, monkeypatch, capsys) -> None:
    pytest.importorskip("qrcode")
    env_file.parent.mkdir(parents=True)
    env_file.write_text(f"GROQ_API_KEY={GROQ}\n", encoding="utf-8")
    opened: list[str] = []

    def fake_open(uri: str) -> bool:
        from pathlib import Path
        from urllib.parse import unquote, urlparse
        path = unquote(urlparse(uri).path).lstrip("/") if os.name == "nt" else unquote(urlparse(uri).path)
        page = Path(path).read_text(encoding="utf-8")
        assert "<svg" in page and "GROQ_API_KEY" in page
        opened.append(path)
        return True

    monkeypatch.setattr("webbrowser.open", fake_open)
    monkeypatch.setattr("builtins.input", lambda *_: "")
    assert kt.main(["qr", "--all"]) == 0
    assert opened and not os.path.exists(opened[0])
    out = capsys.readouterr().out
    assert GROQ not in out and kt.mask(GROQ) in out


def test_usage_and_bad_options(capsys) -> None:
    assert kt.main([]) == 2
    assert kt.main(["qr", "--nope"]) == 2
    assert kt.main(["nope"]) in (1, 2)
    assert "genesis keys" in capsys.readouterr().out
