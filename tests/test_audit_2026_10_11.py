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
