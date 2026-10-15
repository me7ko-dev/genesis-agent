"""/listen и /voice в терминалния чат (2026-10-15): разпознатото минава по пътя
на написания ред и само след потвърждение; на глас — само крайният отговор.
Фалшиви запис, разпознаване и говорене."""
from __future__ import annotations

import types

import pytest

import genesis_terminal_agent as gta
from genesis_agent import voice


@pytest.fixture()
def chat(monkeypatch):
    from genesis_agent import brain, chat_input, model_check, workspace_memory
    monkeypatch.setattr(brain, "any_model_reachable", lambda: True, raising=False)
    monkeypatch.setattr(gta, "print_minimal_banner", lambda: None)
    monkeypatch.setattr(gta, "show_status_bar", lambda: None)
    monkeypatch.setattr(gta, "build_system_prompt", lambda: ("system", ""))
    monkeypatch.setattr(model_check, "needs_check", lambda: False)
    monkeypatch.setattr(workspace_memory, "auto_capture", lambda convo: {})
    monkeypatch.setattr(gta.sys, "argv", ["genesis"])
    monkeypatch.setattr(gta.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(gta, "VOICE_ON", False)
    printed: list[str] = []
    monkeypatch.setattr(gta.console, "print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    turns: list[str] = []

    def run_turn(messages, text, ui):
        turns.append(text)
        ui.assistant("Ще проверя.")              # междинен — не се казва
        ui.assistant("**Готово.** Всички тестове минават.")
        return messages

    monkeypatch.setattr(gta, "run_turn", run_turn)
    spoken: list[str] = []
    monkeypatch.setattr(voice, "speak", lambda text, **kw: spoken.append(text))
    monkeypatch.setattr(voice, "tts_ready", lambda cfg=None: (True, "edge-tts, bg-BG-KalinaNeural"))

    def drive(*lines: str) -> None:
        it = iter([*lines, "exit"])
        monkeypatch.setattr(chat_input, "read_message", lambda read: next(it))
        gta.main()

    return types.SimpleNamespace(drive=drive, turns=turns, spoken=spoken, printed=printed)


def test_listen_sends_the_confirmed_text_as_a_turn(chat, monkeypatch) -> None:
    heard = iter(["направи тест за voice", None])
    monkeypatch.setattr(voice, "listen_flow", lambda **kw: next(heard))
    chat.drive("/listen", "/mic")
    assert chat.turns == ["направи тест за voice"]
    assert any("🎙 ❯ направи тест за voice" in line for line in chat.printed)


def test_heard_text_takes_the_same_path_as_typed_text(chat, monkeypatch) -> None:
    """`!` от потвърдения текст е команда на оператора, както написана — не
    отива при модела като текст (и обратно: моделът не я получава)."""
    from genesis_agent import shell_mode
    monkeypatch.setattr(voice, "listen_flow", lambda **kw: "!git status")
    ran: list[str] = []
    monkeypatch.setattr(shell_mode, "run", lambda cmd, ws, out: ran.append(cmd) or "")
    chat.drive("/слушай")
    assert ran == ["git status"] and chat.turns == []


def test_a_cancelled_listen_sends_nothing(chat, monkeypatch) -> None:
    monkeypatch.setattr(voice, "listen_flow", lambda **kw: None)
    chat.drive("/listen")
    assert chat.turns == []


def test_listen_without_a_terminal_says_so(chat, monkeypatch) -> None:
    monkeypatch.setattr(gta.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(voice, "listen_flow", lambda **kw: pytest.fail("без терминал не записва"))
    chat.drive("/listen")
    assert any("иска терминал" in line for line in chat.printed) and chat.turns == []


def test_voice_on_speaks_only_the_final_answer(chat) -> None:
    chat.drive("здравей", "/voice on", "пусни тестовете", "/voice off", "още веднъж")
    assert chat.turns == ["здравей", "пусни тестовете", "още веднъж"]
    assert chat.spoken == ["**Готово.** Всички тестове минават."]


def test_voice_on_without_tts_stays_off(chat, monkeypatch) -> None:
    monkeypatch.setattr(voice, "tts_ready", lambda cfg=None: (False, "няма с какво да говоря"))
    chat.drive("/voice on", "здравей", "/voice")
    assert chat.spoken == [] and any("няма с какво да говоря" in line for line in chat.printed)
    assert any("изключен" in line for line in chat.printed)


def test_speak_answers_in_config_turns_voice_on_at_start(chat, monkeypatch) -> None:
    monkeypatch.setenv("GENESIS_VOICE_SPEAK", "1")
    chat.drive("здравей")
    assert chat.spoken == ["**Готово.** Всички тестове минават."]


def test_voice_and_listen_are_not_custom_command_names() -> None:
    from genesis_agent import chat_commands
    assert {"voice", "listen", "mic", "глас", "слушай"} <= chat_commands.RESERVED


def test_headless_print_mode_never_loads_the_voice_path() -> None:
    """`genesis -p` е без глас: headless не вика нищо от voice."""
    import inspect

    from genesis_agent import headless
    assert "voice" not in inspect.getsource(headless)
