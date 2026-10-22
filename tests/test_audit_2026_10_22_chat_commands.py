"""Одит 2026-10-22, командите в чата: непознатите /думи, `!команда` без 2 s
чакане, /export с кавички и папка, Ctrl-C в менюто не затваря чата."""
from __future__ import annotations

import os
import sys
import time
import types
from collections import deque

import pytest

import genesis_terminal_agent as gta
from genesis_agent import deliver, remote_server, shell_mode

INT, EOF = "<Ctrl-C>", "<Ctrl-D>"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX обвивка и групи процеси")


@pytest.fixture()
def chat(monkeypatch, tmp_path):
    """Истинският gta.main(); входът (подканата и всеки въпрос на команда) идва
    от списък, ходът към модела само се записва."""
    from genesis_agent import brain, chat_input, model_check, workspace_memory
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "README.md").write_text("# proj\n", encoding="utf-8")
    monkeypatch.chdir(ws)
    monkeypatch.setattr(gta, "WORKSPACE", ws)
    monkeypatch.setattr(brain, "any_model_reachable", lambda: True, raising=False)
    monkeypatch.setattr(gta, "print_minimal_banner", lambda: None)
    monkeypatch.setattr(gta, "show_status_bar", lambda: None)
    monkeypatch.setattr(gta, "build_system_prompt", lambda: ("system", ""))
    monkeypatch.setattr(gta, "provider_ready", lambda pk: (True, ""))
    monkeypatch.setattr(model_check, "needs_check", lambda: False)
    monkeypatch.setattr(workspace_memory, "auto_capture", lambda convo: {})
    monkeypatch.setattr(gta.sys, "argv", ["genesis"])
    monkeypatch.setattr(gta, "VOICE_ON", False)
    monkeypatch.setattr(gta, "current_provider", gta.current_provider)
    monkeypatch.setattr(gta, "current_model_id", gta.current_model_id)
    printed: list[str] = []
    monkeypatch.setattr(gta.console, "print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    script: list[str] = []

    def take(_prompt: str = "") -> str:
        value = script.pop(0)
        if value == INT:
            raise KeyboardInterrupt
        if value == EOF:
            raise EOFError
        return value

    monkeypatch.setattr(chat_input, "read_message", lambda read: take())
    monkeypatch.setattr(gta.console, "input", take)
    turns: list[str] = []

    def run_turn(messages, text, ui, **kw):
        turns.append(text)
        return messages

    monkeypatch.setattr(gta, "run_turn", run_turn)

    def drive(*lines: str):
        printed.clear()
        turns.clear()
        script[:] = [*lines, EOF]
        gta.main()
        return types.SimpleNamespace(turns=list(turns), printed="\n".join(printed), left=list(script))

    return types.SimpleNamespace(drive=drive, ws=ws, tmp=tmp_path)


# ── 1. непозната /дума, /exit, вградена команда с аргументи ─────────────────

class _PhoneUI:
    def __init__(self) -> None:
        self.said: list[str] = []

    def info(self, text: str) -> None:
        self.said.append(text)

    def warn(self, text: str) -> None:
        self.said.append(text)


class _PhoneSession:
    def confirm(self, *a) -> bool:
        return False

    def emit(self, *a, **k) -> None:
        pass


def test_unknown_slash_word_exit_and_builtin_with_args_never_reach_the_model(chat, monkeypatch):
    monkeypatch.setattr(gta, "FALLBACK_CHAIN", [{"provider": "groq", "model": "openai/gpt-oss-120b"},
                                                {"provider": "groq", "model": "llama-3.3-70b"}])
    monkeypatch.setattr(gta, "current_model_id", "друг")
    r = chat.drive("/clera", "/hlep", "/model няма-такъв", "/help me", "/clear now",
                   "/usr/lib/x is broken", "/model llama", "/exit", "след изхода")
    assert r.turns == ["/usr/lib/x is broken"]           # пътят е за модела
    assert "Непозната команда /clera" in r.printed and "/clear?" in r.printed
    assert "Непозната команда /hlep" in r.printed
    assert gta.current_model_id == "llama-3.3-70b"   # /model <име> избира
    assert r.left == ["след изхода", EOF]                  # /exit затваря чата
    assert chat.drive("/quit", "след изхода").left == ["след изхода", EOF]

    ws = chat.tmp
    for text in ("/clera", "/exit", "/hlep"):
        ui = _PhoneUI()
        _, prompt = remote_server.phone_command(
            text, ui, _PhoneSession(), messages=deque([{"role": "system", "content": "s"}]),
            workspace=ws)
        assert prompt is None, text
    _, prompt = remote_server.phone_command(
        "/usr/lib/x is broken", _PhoneUI(), _PhoneSession(),
        messages=deque([{"role": "system", "content": "s"}]), workspace=ws)
    assert prompt == "/usr/lib/x is broken"


# ── 2. `!команда` чакаше 2 s повече: зомби водачът на групата ───────────────

@posix_only
def test_bang_external_program_and_timeout_do_not_wait_two_extra_seconds(tmp_path):
    # Три пъти: първото понякога печели надбягването poll()/EOF и минава бързо.
    started = time.monotonic()
    for _ in range(3):
        shell_mode.run("ls", tmp_path, lambda _: None)
    assert time.monotonic() - started < 1.5
    # exec: водачът е самата команда. Сираче на обвивката се прибира от PID 1
    # — в контейнер (process_api) това е ~1.5 s и не зависи от Genesis.
    started = time.monotonic()
    ctx = shell_mode.run("exec sleep 30", tmp_path, lambda _: None, timeout=1)
    assert time.monotonic() - started < 2.5 and "спряна" in ctx


# ── 3. /export с кавички и с папка за цел ──────────────────────────────────

def test_export_strips_quotes_and_puts_a_folder_target_inside_the_folder(chat):
    desk = chat.tmp / "Desktop"
    desk.mkdir()
    chat.drive('/export "my out.zip"', f"/export '{desk}/my file.zip'", f"/export {desk}{os.sep}")
    assert sorted(os.listdir(chat.ws)) == ["README.md", "my out.zip"]   # нищо странично
    assert (desk / "my file.zip").is_file()
    inside = sorted(p.name for p in desk.glob("proj-*.zip"))
    assert len(inside) == 1 and not (chat.tmp / "Desktop.zip").exists()
    d = deliver.export(chat.ws, str(desk), run_tests=False)   # без разделител накрая
    assert d.zip_path.parent == desk.resolve() and d.zip_path.name.startswith("proj-")


# ── 4. Ctrl-C на подканата и Ctrl-C/Ctrl-D в менюто ─────────────────────────

def test_ctrl_c_cancels_the_line_or_the_menu_and_only_a_second_one_exits(chat):
    r = chat.drive("първо", INT, "второ")
    assert r.turns == ["първо", "второ"] and "Ctrl-C пак" in r.printed
    for key in (INT, EOF):
        r = chat.drive("/model", key, "след менюто")
        assert r.turns == ["след менюто"], key
    r = chat.drive(INT, INT, "никога")
    assert r.turns == [] and r.left == ["никога", EOF]
