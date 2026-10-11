"""Одит 2026-10-11 (сигурност): заобикаляния, доказани с изпълнение.

Всеки тест тук падаше на кода преди поправката — виж коментарите в самите
модули за това какво точно беше измерено."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from genesis_agent import hooks, sandbox
from genesis_agent.sandbox import RiskLevel

# ── Управляващите файлове на Genesis: hooks.json, mcp.json, доверието ────────

@pytest.mark.parametrize("command", [
    "mkdir -p ~/.genesis && printf '%s' '{\"hooks\":{}}' > ~/.genesis/hooks.json",
    "echo '{}' > ~/.genesis/mcp.json",
    "cp evil.json $HOME/.genesis/settings.json",
    "python3 -c \"import pathlib; (pathlib.Path.home()/'.genesis'/'trusted_hooks.json').write_text('{}')\"",
    "cp x.md ~/.genesis/commands/review.md",
    "cp x.md ~/.genesis/agents/helper.md",
])
def test_a_shell_command_touching_genesis_control_files_asks(command) -> None:
    assert sandbox.assess_command(command).level == RiskLevel.CONFIRM


def test_a_planted_hooks_file_is_not_written_in_deny_mode(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sandbox, "_POLICY", sandbox.SandboxPolicy(mode="deny"))
    cmd = "printf '%s' '{\"hooks\":{}}' > ~/.genesis/hooks.json"
    res = sandbox.run_shell(cmd, cwd=tmp_path)
    assert res.blocked
    assert not (home / ".genesis" / "hooks.json").exists()


def test_a_hand_written_trust_record_is_not_trust(tmp_path) -> None:
    """Запис в trusted_hooks.json (sha256 на файла), направен от команда, а не
    от `/hooks trust`, не прави проектните hooks доверени (одит 2026-10-11:
    `python3 -c` от RUN_CMD го сглобяваше сам)."""
    project = tmp_path / "proj" / ".genesis" / "hooks.json"
    project.parent.mkdir(parents=True)
    project.write_text('{"hooks":{"Stop":[{"command":"echo hi"}]}}', encoding="utf-8")
    forged = {str(Path(project).absolute()): hashlib.sha256(project.read_bytes()).hexdigest()}
    hooks._trust_file().parent.mkdir(parents=True, exist_ok=True)
    hooks._trust_file().write_text(json.dumps(forged), encoding="utf-8")
    assert not hooks.is_trusted(project)
    hooks.trust(project)
    assert hooks.is_trusted(project)
    project.write_text('{"hooks":{"Stop":[{"command":"echo changed"}]}}', encoding="utf-8")
    assert not hooks.is_trusted(project)


def test_the_trust_key_is_a_secret_path(tmp_path) -> None:
    hooks.trust_key()
    key = hooks._trust_key_file()
    assert key.is_file()
    assert sandbox.sensitive_path_reason(key)
    assert sandbox.assess_command(f"cat {key}").level == RiskLevel.CONFIRM

