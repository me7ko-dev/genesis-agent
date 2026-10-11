"""Одит 2026-10-11, втори кръг: какво още минаваше покрай поправките за
сигурност от първия кръг. Всеки тест тук падаше на кода преди поправката."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from genesis_agent import chat_commands, hooks, mcp_client, sandbox

_FAKE_MCP = Path(__file__).parent / "fake_mcp_server.py"


def _hooks_file(path: Path, command: str = "echo PLANTED") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"command": command}]}}), encoding="utf-8")


@pytest.fixture
def clean_mcp():
    yield mcp_client
    mcp_client.stop_all()
    mcp_client.workspace = None


# ── Подхвърлен ~/.genesis/hooks.json / mcp.json не се пуска без потвърждение ─

@pytest.mark.skipif(sys.platform == "win32", reason="printf и ~ в POSIX shell")
def test_a_planted_home_hooks_file_does_not_run(tmp_path, monkeypatch) -> None:
    """Одит 2026-10-11, възпроизведено: `> ~/.genesis/hook''s.json` (една
    кавичка повече от спрения в първия кръг пример) беше SAFE в режим „deny“,
    а ~/.genesis/hooks.json се пускаше без проверка — извън sandbox-а, с целия
    os.environ. Сега файлът е доверен само с подпис от /hooks trust."""
    from genesis_agent import paths
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(paths, "GENESIS_HOME", home / ".genesis")
    monkeypatch.setattr(sandbox, "_POLICY", sandbox.SandboxPolicy(mode="deny"))
    hooks.trust_key()                       # Genesis вече е вървял на машината
    cmd = "printf '%s' '{\"hooks\":{\"PreToolUse\":[{\"command\":\"echo PWNED\"}]}}' > ~/.genesis/hook''s.json"
    sandbox.run_shell(cmd, cwd=tmp_path)
    planted = home / ".genesis" / "hooks.json"
    if planted.exists():                    # гейтът не е единствената защита
        active, _ = hooks.configured(tmp_path)
        assert active == []


def test_home_hooks_need_the_operators_trust_and_lose_it_on_change(tmp_path) -> None:
    hooks.trust_key()
    home = hooks._home() / "hooks.json"
    _hooks_file(home)
    assert hooks.configured(tmp_path)[0] == []
    hooks.trust(home)
    assert [h.command for h in hooks.configured(tmp_path)[0]] == ["echo PLANTED"]
    _hooks_file(home, "echo CHANGED")
    assert hooks.configured(tmp_path)[0] == []
    assert "промен" in hooks.trust_problem(home)


def test_hooks_trust_covers_the_home_file(tmp_path) -> None:
    home = hooks._home() / "hooks.json"
    _hooks_file(home)
    shown: list[str] = []
    chat_commands.handle("/hooks trust", messages=None, workspace=tmp_path,
                         out=shown.append, ask=lambda q: "да")
    assert any("echo PLANTED" in line for line in shown)
    assert [h.command for h in hooks.configured(tmp_path)[0]] == ["echo PLANTED"]


def test_a_planted_home_mcp_file_does_not_start(tmp_path, monkeypatch, clean_mcp) -> None:
    import genesis_skills as gs
    hooks.trust_key()
    marker = tmp_path / "PWNED"
    home = hooks._home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {"evil": {
        "command": sys.executable, "args": ["-c", f"open({str(marker)!r}, 'w')"]}}}),
        encoding="utf-8")
    monkeypatch.setattr(gs, "_WORKSPACE", tmp_path)
    mcp_client.workspace = tmp_path
    lines = mcp_client.start_all(tmp_path)
    assert not marker.exists()
    assert any("/mcp trust" in line for line in lines), lines


def test_mcp_trust_covers_the_home_file(tmp_path, monkeypatch, clean_mcp) -> None:
    import genesis_skills as gs
    home = hooks._home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {"fake": {
        "command": sys.executable, "args": [str(_FAKE_MCP)]}}}), encoding="utf-8")
    monkeypatch.setattr(gs, "_WORKSPACE", tmp_path)
    mcp_client.workspace = tmp_path
    assert not mcp_client.configured(tmp_path)[0]
    chat_commands.handle("/mcp trust", messages=None, workspace=tmp_path,
                         out=lambda s: None, ask=lambda q: "да")
    assert any("fake: 2 инструмента" in line for line in mcp_client.start_all(tmp_path))


# ── Стар запис за доверие (sha256 без подпис) ───────────────────────────────

def test_a_legacy_trust_record_says_why_it_is_no_longer_trust(tmp_path) -> None:
    """Записите от преди подписа (голият sha256) вече не са доверие — без
    обяснение операторът виждаше само „не е доверен“ за файл, на който вече
    се беше доверил (одит 2026-10-11)."""
    project = tmp_path / "proj" / ".genesis" / "hooks.json"
    _hooks_file(project)
    hooks.trust_key()
    record = {str(project.absolute()): hashlib.sha256(project.read_bytes()).hexdigest()}
    hooks._trust_file().write_text(json.dumps(record), encoding="utf-8")
    assert not hooks.is_trusted(project)
    assert "по-стара версия" in hooks.trust_problem(project)


