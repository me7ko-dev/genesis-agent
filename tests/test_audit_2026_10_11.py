"""Одит 2026-10-11 (сигурност): заобикаляния, доказани с изпълнение.

Всеки тест тук падаше на кода преди поправката — виж коментарите в самите
модули за това какво точно беше измерено."""
from __future__ import annotations

import hashlib
import json
import sys
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



# ── API ключовете на родителя през /proc/<pid>/environ ──────────────────────

@pytest.mark.parametrize("command", [
    "tr '\\0' '\\n' < /proc/$PPID/environ | grep API_KEY",
    "cat /proc/self/environ",
    "strings /proc/1234/mem",
])
def test_reading_another_process_environment_asks(command) -> None:
    assert sandbox.assess_command(command).level == RiskLevel.CONFIRM


_HARNESS = r"""
import os, sys
from pathlib import Path
from genesis_agent import sandbox
sandbox.set_policy(sandbox.SandboxPolicy(mode="deny"))
ws = Path(sys.argv[1])
me = os.getpid()
code = ("import sys\n"
        "try:\n"
        "    data = open('/proc/%d/env' + 'iron', 'rb').read()\n"
        "    print('LEAK' if b'sk-fake-proc' in data else 'NOKEY')\n"
        "except OSError as e:\n"
        "    print('DENIED', type(e).__name__)\n") % me
res = sandbox._run([sys.executable, "-c", code], cwd=ws, policy=sandbox.get_policy(), timeout=30)
print("RAW:", res.stdout.strip())
guarded = sandbox.run_python("import os\nprint(open(f'/proc/{os.getppid()}/environ').read())", cwd=ws)
print("GUARD:", "sk-fake-proc" in guarded.stdout)
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc е само на Linux")
def test_a_sandboxed_child_cannot_read_the_parents_environment(tmp_path) -> None:
    """Одит 2026-10-11, възпроизведено: `tr '\\0' '\\n' < /proc/$PPID/environ`
    беше SAFE и печаташе ключа, който sandbox-ът беше махнал от средата на
    детето; py_guard пускаше `open(f'/proc/{os.getppid()}/environ')`."""
    import os
    import subprocess
    harness = tmp_path / "harness.py"
    harness.write_text(_HARNESS, encoding="utf-8")
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ, OPENAI_API_KEY="sk-fake-proc", PYTHONPATH=str(root),
               GENESIS_HOME=str(tmp_path / "gh"), HOME=str(tmp_path),
               GENESIS_SANDBOX_DIR=str(tmp_path / "sbx"))
    out = subprocess.run([sys.executable, str(harness), str(tmp_path)], capture_output=True,
                         text=True, env=env, timeout=120, cwd=str(tmp_path), check=False)
    assert "RAW:" in out.stdout, out.stderr
    assert "GUARD: False" in out.stdout
    # root има CAP_SYS_PTRACE и чете всеки /proc въпреки PR_SET_DUMPABLE.
    if os.geteuid() != 0:
        assert "LEAK" not in out.stdout


# ── MCP сървърите на оператора не тръгват от папката на проекта ─────────────

_FAKE_MCP = Path(__file__).parent / "fake_mcp_server.py"


@pytest.fixture
def clean_mcp():
    from genesis_agent import mcp_client
    yield mcp_client
    mcp_client.stop_all()
    mcp_client.workspace = None


def test_a_home_mcp_server_does_not_import_a_module_planted_in_the_workspace(
        tmp_path, monkeypatch, clean_mcp) -> None:
    """Одит 2026-10-11, възпроизведено: `python3 -m mcp_server_time` от
    ~/.genesis/mcp.json тръгваше с cwd = клонирания проект, `-m` внася от cwd
    и подхвърленият mcp_server_time.py се пускаше при всеки старт на чата —
    без /mcp trust. Същото с npx и локален node_modules."""
    import genesis_skills as gs
    from genesis_agent import paths
    mcp_client = clean_mcp
    ws = tmp_path / "cloned_repo"
    ws.mkdir()
    marker = tmp_path / "PWNED"
    (ws / "planted_mcp_mod.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n",
                                           encoding="utf-8")
    home = Path(paths.GENESIS_HOME)
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {
        "planted": {"command": sys.executable, "args": ["-m", "planted_mcp_mod"]},
        "fake": {"command": sys.executable, "args": [str(_FAKE_MCP)]},
    }}), encoding="utf-8")
    hooks.trust(home / "mcp.json")  # операторът го е потвърдил
    monkeypatch.setattr(gs, "_WORKSPACE", ws)
    mcp_client.workspace = ws
    lines = mcp_client.start_all(ws)
    assert not marker.exists()
    assert any("fake: 2 инструмента" in line for line in lines), lines


# ── Тайна по glob, разделени кавички и символна връзка в RUN_CMD ────────────

@pytest.fixture
def secret_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("-----BEGIN KEY----- FAKEKEY", encoding="utf-8")
    (home / ".ssh" / "id_ed25519").write_text("FAKEKEY", encoding="utf-8")
    (home / ".aws").mkdir()
    (home / ".aws" / "credentials").write_text("FAKEKEY", encoding="utf-8")
    (home / ".genesis").mkdir()
    for name in ("remote.json", ".env", "mcp_tokens.json"):
        (home / ".genesis" / name).write_text("FAKEKEY", encoding="utf-8")
    ws = home / "proj"
    ws.mkdir()
    (ws / "plain.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home, ws


@pytest.mark.parametrize("command", [
    "cat ~/.s?h/id_r?a",
    "cat ~/.ss[h]/id_rs[a]",
    "cat ~/.a?s/cred*",
    "cat ~/.genesis/remote.jso?",
    "cat ~/.genesis/.en?",
    "cat ~/.genesis/mcp_tokens.jso[n]",
    "cat ~/.s''sh/id_ed2551''9",
])
def test_a_secret_behind_a_glob_or_split_quotes_asks(secret_home, command) -> None:
    """Одит 2026-10-11, възпроизведено: образецът гледаше само буквалния
    текст, и `cat ~/.s?h/id_r?a` печаташе ключа в режим „deny“."""
    _, ws = secret_home
    assert sandbox.assess_command(command, cwd=ws).level == RiskLevel.CONFIRM


def test_a_symlink_to_a_key_in_the_workspace_asks_in_run_cmd(secret_home, monkeypatch) -> None:
    home, ws = secret_home
    link = ws / "notes.txt"
    try:
        link.symlink_to(home / ".ssh" / "id_rsa")
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    monkeypatch.setattr(sandbox, "_POLICY", sandbox.SandboxPolicy(mode="deny"))
    assert sandbox.assess_command("cat notes.txt", cwd=ws).level == RiskLevel.CONFIRM
    res = sandbox.run_shell("cat notes.txt", cwd=ws)
    assert res.blocked and "FAKEKEY" not in res.stdout


@pytest.mark.parametrize("command", ["cat plain.txt", "cat *.txt", "ls -la", "grep -r hello ."])
def test_ordinary_files_in_the_workspace_stay_safe(secret_home, command) -> None:
    _, ws = secret_home
    assert sandbox.assess_command(command, cwd=ws).level == RiskLevel.SAFE


# ── MCP инструмент „само чете“ не стига до тайните без въпрос ───────────────

def test_a_read_only_mcp_tool_asks_before_a_secret_path(tmp_path, monkeypatch, clean_mcp) -> None:
    """Одит 2026-10-11, възпроизведено: read_file на filesystem сървър
    (readOnlyHint) връщаше ~/.ssh/id_rsa в режим „deny“ и в plan mode,
    докато READ_FILE го отказваше."""
    import genesis_skills as gs
    from genesis_agent import paths
    mcp_client = clean_mcp
    key = tmp_path / "home" / ".ssh" / "id_rsa"
    key.parent.mkdir(parents=True)
    key.write_text("-----BEGIN KEY----- FAKEKEY", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "readme.txt").write_text("hello from the project", encoding="utf-8")
    home = Path(paths.GENESIS_HOME)
    home.mkdir(parents=True, exist_ok=True)
    inner = home / "remote_notes.txt"
    inner.write_text("INTERNAL-STATE", encoding="utf-8")
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {
        "fs": {"command": sys.executable, "args": [str(_FAKE_MCP), "fs"], "autoApprove": ["read_file"]},
    }}), encoding="utf-8")
    hooks.trust(home / "mcp.json")  # операторът го е потвърдил
    monkeypatch.setattr(gs, "_WORKSPACE", ws)
    mcp_client.workspace = ws
    assert any("fs: 1 инструмента" in line for line in mcp_client.start_all(ws))
    monkeypatch.setattr(sandbox, "_POLICY", sandbox.SandboxPolicy(mode="deny"))
    out = mcp_client.call("fs.read_file", {"path": str(key)})
    assert "FAKEKEY" not in out and "SANDBOX DENIED" in out
    out = mcp_client.call("fs.read_file", {"path": str(inner)})
    assert "INTERNAL-STATE" not in out and "SANDBOX DENIED" in out
    assert "hello from the project" in mcp_client.call("fs.read_file", {"path": str(ws / "readme.txt")})
