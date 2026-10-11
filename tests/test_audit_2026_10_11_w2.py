"""Одит 2026-10-11, втори кръг: какво още минаваше покрай поправките за
сигурност от първия кръг. Всеки тест тук падаше на кода преди поправката."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from genesis_agent import chat_commands, hooks, mcp_client, sandbox
from genesis_agent.sandbox import RiskLevel

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


# ── Ключът за доверието зад cd и glob ───────────────────────────────────────

def test_the_trust_key_behind_cd_and_a_glob_asks(tmp_path, monkeypatch) -> None:
    """`cd ~/.gen*; cat hooks_trust.k*` минаваше като SAFE: аргументите се
    разгъваха спрямо работната папка, не спрямо папката след `cd`."""
    from genesis_agent import paths
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(paths, "GENESIS_HOME", home / ".genesis")
    hooks.trust_key()
    ws = tmp_path / "ws"
    ws.mkdir()
    for command in ("cd ~/.gen*; cat hooks_trust.k*", "cd ~/.genesis && cat hooks_trust.key",
                    "pushd ~/.genesis; cat hooks_trust.key"):
        assert sandbox.assess_command(command, cwd=ws).level == RiskLevel.CONFIRM, command
    assert sandbox.assess_command("cd sub; cat notes.txt", cwd=ws).level == RiskLevel.SAFE


# ── Четене и споменаване на управляващите файлове не пита ───────────────────

@pytest.mark.parametrize("command", [
    "cat ~/.genesis/settings.json",
    "ls .genesis/commands",
    "grep -rn trusted_hooks genesis_agent/",
    "grep -n hooks_trust.key genesis_agent/hooks.py",
    "cp draft.md .genesis/commands/deploy.md",
    "cat .genesis/hooks.json",
])
def test_reading_or_mentioning_genesis_files_stays_safe(command) -> None:
    """Първият кръг питаше за всяко споменаване (и го наричаше „промяна“):
    Genesis не можеше да работи по собственото си хранилище, нито да пише
    командите на проекта с RUN_CMD. hooks.json/mcp.json вече са защитени от
    подписа, не от думите в командата."""
    assert sandbox.assess_command(command).level == RiskLevel.SAFE, command


@pytest.mark.parametrize("command", [
    "cp x.md ~/.genesis/commands/review.md",
    "cp x.md $HOME/.genesis/agents/helper.md",
    "cp x.md ~/.genesis/comm''ands/review.md",
])
def test_writing_the_operators_own_commands_still_asks(command) -> None:
    assert sandbox.assess_command(command).level == RiskLevel.CONFIRM, command


# ── .azure-pipelines не е тайна ─────────────────────────────────────────────

def test_azure_pipelines_folder_is_not_a_secret(tmp_path) -> None:
    assert sandbox.sensitive_path_reason(tmp_path / ".azure-pipelines" / "ci.yml") is None
    assert sandbox.assess_command("cat .azure-pipelines/ci.yml").level == RiskLevel.SAFE
    assert sandbox.sensitive_path_reason(tmp_path / ".azure" / "msal_token_cache.json")
    assert sandbox.assess_command("cat ~/.azure/accessTokens.json").level == RiskLevel.CONFIRM


# ── /proc/<pid>/environ по сглобен път ──────────────────────────────────────

@pytest.mark.parametrize("command", [
    'p=/proc/$(pgrep -f mcp-server); cat $p/env""iron',
    "cat \"$p\"/environ",
    "cat ${dir}/env*",
])
def test_another_process_environment_through_a_variable_asks(command) -> None:
    """MCP сървърите на Genesis пазят токените си в средата и след exec са
    отново dumpable (същия uid) — `cat $p/env""iron` минаваше като SAFE."""
    assert sandbox.assess_command(command).level == RiskLevel.CONFIRM


@pytest.mark.parametrize("command", [
    'python3 -c "import os; print(os.environ)"', "ls $CONDA_PREFIX/envs",
    "cat $ASSETS/maps/level1.json", "ls ${dir}/environments",
])
def test_ordinary_variables_and_environ_words_stay_safe(command) -> None:
    assert sandbox.assess_command(command).level == RiskLevel.SAFE, command


# ── Сървърите от ~/.genesis/mcp.json и относителните пътища ─────────────────

def test_a_relative_path_for_a_home_server_is_reported(tmp_path, monkeypatch, clean_mcp) -> None:
    """Сървърите от ~/.genesis/mcp.json тръгват от празна временна папка:
    `--db-path app.db` отваряше празна база там, която изчезва на изхода —
    без нито дума (одит 2026-10-11)."""
    import genesis_skills as gs
    home = hooks._home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {"db": {
        "command": sys.executable, "args": [str(_FAKE_MCP), "--db-path", "app.db"]}}}),
        encoding="utf-8")
    hooks.trust(home / "mcp.json")
    monkeypatch.setattr(gs, "_WORKSPACE", tmp_path)
    mcp_client.workspace = tmp_path
    lines = mcp_client.start_all(tmp_path)
    assert any("app.db" in line and "пълен път" in line for line in lines), lines
    assert not any("@modelcontextprotocol" in line for line in lines)


@pytest.mark.parametrize("args", [
    ["-y", "@modelcontextprotocol/server-github"], ["mcp-server-fetch"], ["-m", "mcp_server_time"],
    ["--from", "git+https://github.com/x/y.git", "srv"], ["pkg==1.2.3"], ["--port=8080"],
    ["-m", "mcp_server.main"], ["--repo", "owner/repo"], ["--host", "api.example.com"],
    ["/abs/app.db"], ["~/app.db"],
])
def test_ordinary_server_arguments_are_not_relative_paths(args) -> None:
    assert mcp_client._relative_path_args(args) == []


def test_relative_data_paths_are_found() -> None:
    assert mcp_client._relative_path_args(
        ["--db-path", "app.db", "--repository", ".", "--db=data/x.sqlite", "server.py"]) == \
        ["app.db", ".", "data/x.sqlite", "server.py"]
