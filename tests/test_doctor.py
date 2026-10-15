"""`genesis doctor` и `/doctor` (2026-10-15): всяка проверка с ✓/✗ и ред за
поправка, без да виси и без да печата тайни. Всичко е с фалшиви проби —
никаква мрежа, никакъв истински `claude`, нищо от ~/.genesis на оператора."""
from __future__ import annotations

import json
import sys
import time
from collections import namedtuple
from pathlib import Path

import pytest

from genesis_agent import chat_commands, cli, doctor, paths
from genesis_agent.doctor import Check

FAKE_MCP = Path(__file__).parent / "fake_mcp_server.py"


def _only(monkeypatch, *plan):
    monkeypatch.setattr(doctor, "probes", lambda online=False: list(plan))


# ── отчетът ─────────────────────────────────────────────────────────────────

def test_the_report_marks_each_check_and_gives_one_fix_line() -> None:
    text = doctor.format_report([Check("git", "ok", "git 2.43", "не се показва"),
                                 Check("ruff", "fail", "няма го в PATH", "pip install ruff"),
                                 Check("Модели", "skip", "не е проверено", "genesis doctor --models")])
    lines = text.splitlines()
    assert lines[0] == "✓ git: git 2.43" and "не се показва" not in text
    assert lines[1] == "✗ ruff: няма го в PATH" and lines[2] == "    → pip install ruff"
    assert lines[3].startswith("· Модели") and "--models" in lines[4]
    assert "1 проблем (✗)" in text


def test_a_hanging_probe_is_a_failure_not_a_hang(monkeypatch) -> None:
    _only(monkeypatch, ("бавна", lambda ws: time.sleep(5) or []),
          ("бърза", lambda ws: [Check("бърза", "ok", "да")]))
    started = time.monotonic()
    checks = doctor.run_checks(Path.cwd(), deadline=0.5)
    assert time.monotonic() - started < 2
    assert [(c.name, c.status) for c in checks] == [("бавна", "fail"), ("бърза", "ok")]
    assert "не отговори" in checks[0].detail


def test_a_crashing_probe_does_not_hide_the_others(monkeypatch) -> None:
    def boom(ws):
        raise RuntimeError("счупено")
    _only(monkeypatch, ("A", boom), ("B", lambda ws: [Check("B", "ok", "да")]))
    checks = doctor.run_checks(Path.cwd(), deadline=5)
    assert checks[0].status == "fail" and "счупено" in checks[0].detail and checks[1].status == "ok"


def test_a_command_that_hangs_is_cut_by_its_timeout() -> None:
    started = time.monotonic()
    code, why = doctor._run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
    assert code is None and "не отговори" in why and time.monotonic() - started < 10


def test_fix_commands_quote_a_path_with_spaces_for_cmd_exe() -> None:
    assert doctor._cmd(r"C:\Program Files\Python312\python.exe", "-m", "pip", "install", "ruff") == \
        r'"C:\Program Files\Python312\python.exe" -m pip install ruff'
    assert doctor._cmd("/usr/bin/python3", "-m", "pytest") == "/usr/bin/python3 -m pytest"


# ── тайните ─────────────────────────────────────────────────────────────────

def test_key_values_never_reach_the_report(monkeypatch) -> None:
    groq = "gsk_" + "A1b2C3d4" * 4
    plain = "plainvalue-без-образец-123"         # стойност без познат формат — пак тайна по име
    monkeypatch.setenv("GROQ_API_KEY", groq)
    monkeypatch.setenv("MY_SERVICE_TOKEN", plain)
    leak = f"сървърът каза: Authorization {groq} / token={plain}"
    _only(monkeypatch, ("API ключове", doctor.check_keys),
          ("изтичане", lambda ws: [Check("изтичане", "fail", leak, f"пусни с {plain}")]))
    checks = doctor.run_checks(Path.cwd(), deadline=10)
    text = doctor.format_report(checks) + json.dumps([c.__dict__ for c in checks], ensure_ascii=False)
    assert groq not in text and plain not in text
    assert "GROQ_API_KEY" in checks[0].detail and checks[0].status == "ok"


def test_without_any_key_the_fix_is_genesis_setup(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_key_names", list)
    from genesis_agent import claude_cli
    monkeypatch.setattr(claude_cli, "available", lambda: False)
    (check,) = doctor.check_keys(Path.cwd())
    assert check.status == "fail" and "genesis setup" in check.fix


# ── отделните проверки ──────────────────────────────────────────────────────

def test_missing_programs_say_how_to_get_them(monkeypatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    (git,) = doctor.check_git(Path.cwd())
    node, npm = doctor.check_node(Path.cwd())
    assert git.status == "fail" and "git-scm.com" in git.fix
    assert node.status == npm.status == "fail" and "nodejs.org" in npm.fix


def test_pytest_is_checked_with_the_projects_python(monkeypatch, tmp_path) -> None:
    py = r"C:\Program Files\Proj Venv\python.exe"
    monkeypatch.setattr(paths, "project_python", lambda root=None: py)
    seen: list[list[str]] = []

    def fake_run(argv, timeout=8.0):
        seen.append(argv)
        return 1, "C:\\...\\python.exe: No module named pytest"
    monkeypatch.setattr(doctor, "_run", fake_run)
    (check,) = doctor.check_pytest(tmp_path)
    assert seen == [[py, "-m", "pytest", "--version"]]
    assert check.status == "fail" and "няма pytest" in check.detail
    assert check.fix.startswith(f'"{py}" -m pip install pytest')


def test_pytest_reachable_is_ok(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(paths, "project_python", lambda root=None: sys.executable)
    (check,) = doctor.check_pytest(tmp_path)
    assert check.status == "ok" and "pytest" in check.detail


def test_models_without_network_are_not_checked(monkeypatch) -> None:
    import requests

    from genesis_agent import model_check

    def no_network(*a, **kw):
        raise AssertionError("/doctor без --models не пипа мрежата")
    monkeypatch.setattr(requests, "post", no_network)
    (check,) = doctor.check_models(Path.cwd())
    assert check.status == "skip" and "не е проверено" in check.detail and "--models" in check.fix
    model_check.CHECK_PATH.write_text(json.dumps({
        "checked_at": "2026-10-01T00:00:00+00:00",
        "results": [{"provider": "groq", "model": "a", "status": "ok"},
                    {"provider": "nvidia", "model": "b", "status": "dead"}]}), encoding="utf-8")
    (check,) = doctor.check_models(Path.cwd())
    assert check.status == "skip" and "1/2" in check.detail


def test_models_with_the_flag_reuse_the_models_check(monkeypatch) -> None:
    from genesis_agent import model_check
    monkeypatch.setattr(model_check, "run_check", lambda models=None: [
        {"provider": "groq", "model": "a", "status": "ok"},
        {"provider": "nvidia", "model": "b", "status": "dead"}])
    (check,) = doctor.check_models(Path.cwd(), online=True)
    assert check.status == "ok" and "1/2 отговарят" in check.detail and "nvidia/b" in check.detail
    monkeypatch.setattr(model_check, "run_check", lambda models=None: [
        {"provider": "groq", "model": "a", "status": "key"}])
    (check,) = doctor.check_models(Path.cwd(), online=True)
    assert check.status == "fail" and "genesis models --check" in check.fix


def _mcp_json(path: Path, **servers: list[str]) -> None:
    path.write_text(json.dumps({"mcpServers": {
        name: {"command": sys.executable, "args": [str(FAKE_MCP), *args]}
        for name, args in servers.items()}}), encoding="utf-8")


def test_mcp_servers_are_started_briefly_and_stopped(monkeypatch, tmp_path) -> None:
    from genesis_agent import mcp_client
    monkeypatch.setattr(doctor, "_MCP_TIMEOUT", 3.0)
    _mcp_json(Path(paths.GENESIS_HOME) / "mcp.json", good=[], mute=["chatty"])
    started = time.monotonic()
    checks = {c.name: c for c in doctor.check_mcp(tmp_path)}
    assert time.monotonic() - started < 15
    assert checks["MCP good"].status == "ok" and "2 инструмента" in checks["MCP good"].detail
    assert checks["MCP mute"].status == "fail" and "mcp.json" in checks["MCP mute"].fix
    assert not mcp_client._servers and not mcp_client._started   # чатът не е пипнат


def test_an_untrusted_project_mcp_json_is_a_problem(tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    _mcp_json(tmp_path / ".mcp.json", proj=[])
    checks = doctor.check_mcp(tmp_path)
    assert checks[0].status == "fail" and "/mcp trust" in checks[0].fix
    assert not any(c.name == "MCP proj" for c in checks)        # недовереното не се пуска


def test_the_chat_reports_its_running_servers_instead_of_starting_new_ones(monkeypatch, tmp_path) -> None:
    from genesis_agent import mcp_client
    _mcp_json(Path(paths.GENESIS_HOME) / "mcp.json", good=[])
    mcp_client.start_all(tmp_path)
    try:
        def no_start(*a, **kw):
            raise AssertionError("вече вървящ сървър не се пуска втори път")
        monkeypatch.setattr(mcp_client.Server, "start", no_start)
        (check,) = doctor.check_mcp(tmp_path)
        assert check.status == "ok" and "върви" in check.detail
    finally:
        mcp_client.stop_all()


def test_untrusted_project_hooks_are_a_problem(tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".genesis").mkdir()
    (tmp_path / ".genesis" / "hooks.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    checks = doctor.check_hooks(tmp_path)
    assert checks[0].status == "fail" and "/hooks trust" in checks[0].fix
    (tmp_path / ".genesis" / "hooks.json").unlink()
    assert doctor.check_hooks(tmp_path)[0].status == "skip"


def test_low_disk_space_is_a_problem(monkeypatch) -> None:
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: usage(10 << 30, 10 << 30, 200 << 20))
    (check,) = doctor.check_disk(Path.cwd())
    assert check.status == "fail" and "1 GB" in check.fix
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: usage(100 << 30, 50 << 30, 50 << 30))
    assert doctor.check_disk(Path.cwd())[0].status == "ok"


@pytest.mark.parametrize(("output", "status", "fix"), [
    ("CHROMIUM_OK", "ok", ""),
    ("CHROMIUM_MISSING", "fail", "-m playwright install chromium"),
    ("ModuleNotFoundError: No module named 'playwright'", "fail", "-m pip install playwright"),
])
def test_playwright_and_chromium(monkeypatch, tmp_path, output, status, fix) -> None:
    monkeypatch.setattr(paths, "project_python", lambda root=None: sys.executable)
    monkeypatch.setattr(doctor, "_run", lambda argv, timeout=8.0: (0 if "OK" in output else 1, output))
    (check,) = doctor.check_playwright(tmp_path)
    assert check.status == status and fix in check.fix


@pytest.mark.parametrize(("method", "status", "word"), [
    ("claude.ai", "ok", "абонамент"),
    ("none", "fail", "/login"),
    (None, "fail", "/login"),
])
def test_claude_cli_login(monkeypatch, method, status, word) -> None:
    from genesis_agent import claude_cli
    monkeypatch.setattr(claude_cli, "available", lambda: True)
    monkeypatch.setattr(claude_cli, "auth_method", lambda timeout=20: method)
    (check,) = doctor.check_claude(Path.cwd())
    assert check.status == status and word in (check.detail + check.fix)


def test_claude_cli_not_installed_is_not_a_problem(monkeypatch) -> None:
    from genesis_agent import claude_cli
    monkeypatch.setattr(claude_cli, "available", lambda: False)
    monkeypatch.setattr(claude_cli, "auth_method", lambda timeout=20: pytest.fail("не се пуска"))
    assert doctor.check_claude(Path.cwd())[0].status == "skip"


def test_every_real_probe_answers_in_time(monkeypatch, tmp_path) -> None:
    """Истинските проби (без мрежа и без истински `claude`) — всяка дава ред."""
    from genesis_agent import claude_cli
    monkeypatch.setattr(claude_cli, "cli_path", lambda: None)
    checks = doctor.run_checks(tmp_path)
    names = {c.name for c in checks}
    assert {"Python", "git", "ruff", "pytest", "API ключове", "Модели", "Диск",
            "Playwright/Chromium", "claude CLI"} <= names
    assert all(c.status in doctor.MARK for c in checks)
    slow = [f"{c.name}: {c.detail}" for c in checks if "не отговори" in c.detail]
    assert not slow, slow   # коя проба — иначе на CI се вижда само „assert not True“


# ── откъде се стига ─────────────────────────────────────────────────────────

def test_genesis_doctor_on_the_command_line(monkeypatch, capsys, tmp_path) -> None:
    _only(monkeypatch, ("git", lambda ws: [Check("git", "ok", "2.43")]),
          ("ruff", lambda ws: [Check("ruff", "fail", "няма", "pip install ruff")]))
    assert cli.main(["doctor", "--cwd", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "✓ git: 2.43" in out and "✗ ruff: няма" in out and "→ pip install ruff" in out
    assert cli.main(["doctor", "--json", "--cwd", str(tmp_path)]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and data["checks"][1] == {"name": "ruff", "status": "fail",
                                                         "detail": "няма", "fix": "pip install ruff"}
    assert cli.main(["doctor", "--bogus"]) == 2
    assert cli.main(["doctor", "--cwd"]) == 2


def test_doctor_in_the_chat(monkeypatch, tmp_path) -> None:
    calls: list[bool] = []

    def fake_run(ws, *, online=False, deadline=None):
        calls.append(online)
        return [Check("git", "ok", "2.43")]
    monkeypatch.setattr(doctor, "run_checks", fake_run)
    said: list[str] = []
    res = chat_commands.handle("/doctor", messages=None, workspace=tmp_path,
                               out=said.append, ask=lambda q: "")
    assert res is not None and res.prompt is None and "✓ git: 2.43" in "\n".join(said)
    chat_commands.handle("/doctor --models", messages=None, workspace=tmp_path,
                         out=said.append, ask=lambda q: "")
    assert calls == [False, True]


def test_doctor_from_the_phone(monkeypatch, tmp_path) -> None:
    pytest.importorskip("cryptography")
    from genesis_agent import remote_server as rs
    monkeypatch.setattr(doctor, "run_checks",
                        lambda ws, *, online=False, deadline=None: [Check("git", "ok", "2.43")])
    infos: list[str] = []

    class UI:
        def info(self, text: str) -> None:
            infos.append(text)

        def warn(self, text: str) -> None:
            infos.append(text)
    _, prompt = rs.phone_command("/doctor", UI(), rs.RemoteSession(lambda text, ui: None),
                                 messages=[], workspace=tmp_path)
    assert prompt is None and any("✓ git" in t for t in infos)
    assert "/doctor" in rs.PHONE_HELP
