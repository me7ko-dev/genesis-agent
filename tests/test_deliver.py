"""genesis_agent.deliver — `genesis export`: the project as a zip + report."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from genesis_agent import deliver


@pytest.fixture(autouse=True)
def _memory(tmp_path, monkeypatch):
    from genesis_agent import workspace_memory as wm
    monkeypatch.setattr(wm, "DB_PATH", tmp_path / "wm.db")


def _project(root: Path, *, failing: bool = False) -> Path:
    p = root / "egn"
    (p / "tests").mkdir(parents=True)
    (p / "egn.py").write_text("def ok():\n    return True\n", encoding="utf-8")
    (p / "conftest.py").write_text("", encoding="utf-8")
    (p / "tests" / "test_egn.py").write_text(
        "from egn import ok\n\n\ndef test_ok():\n    assert ok() is " + ("False" if failing else "True") + "\n",
        encoding="utf-8")
    return p


def _report(zip_path: Path, name: str) -> str:
    with zipfile.ZipFile(zip_path) as z:
        return z.read(f"{name}/{deliver.REPORT_NAME}").decode("utf-8")


def test_export_zips_the_project_with_a_report(tmp_path) -> None:
    p = _project(tmp_path)
    d = deliver.export(p, tmp_path / "out.zip")
    assert d.zip_path == (tmp_path / "out.zip").resolve()
    with zipfile.ZipFile(d.zip_path) as z:
        names = set(z.namelist())
    assert {"egn/egn.py", "egn/tests/test_egn.py", f"egn/{deliver.REPORT_NAME}"} <= names
    assert d.tests_passed is True
    report = _report(d.zip_path, "egn")
    assert "✅ минават" in report
    assert "1 passed" in report


def test_failing_tests_are_reported_as_failing(tmp_path) -> None:
    d = deliver.export(_project(tmp_path, failing=True), tmp_path / "out.zip")
    assert d.tests_passed is False
    assert "❌ падат" in _report(d.zip_path, "egn")


def test_no_tests_flag_does_not_run_them(tmp_path) -> None:
    d = deliver.export(_project(tmp_path, failing=True), tmp_path / "out.zip", run_tests=False)
    assert d.tests_passed is None
    assert "Не са пускани" in _report(d.zip_path, "egn")


def test_secrets_stay_out_and_are_named(tmp_path) -> None:
    p = _project(tmp_path)
    (p / ".env").write_text("API_KEY=sk-should-not-ship\n", encoding="utf-8")
    (p / "server.pem").write_text("-----BEGIN-----\n", encoding="utf-8")
    (p / ".env.example").write_text("API_KEY=\n", encoding="utf-8")
    d = deliver.export(p, tmp_path / "out.zip", run_tests=False)
    with zipfile.ZipFile(d.zip_path) as z:
        names = set(z.namelist())
        blob = b"".join(z.read(n) for n in names)
    assert "egn/.env" not in names and "egn/server.pem" not in names
    assert b"sk-should-not-ship" not in blob
    assert "egn/.env.example" in names
    assert set(d.withheld) == {".env", "server.pem"}
    assert "`.env`" in _report(d.zip_path, "egn")


def test_build_junk_is_left_out(tmp_path) -> None:
    p = _project(tmp_path)
    for junk in (".git/HEAD", "node_modules/x/index.js", ".venv/bin/python", "__pycache__/egn.cpython.pyc"):
        (p / junk).parent.mkdir(parents=True, exist_ok=True)
        (p / junk).write_text("x", encoding="utf-8")
    d = deliver.export(p, tmp_path / "out.zip", run_tests=False)
    assert all(not f.startswith((".git", "node_modules", ".venv", "__pycache__")) for f in d.files)


def test_a_zip_inside_the_project_does_not_include_itself(tmp_path) -> None:
    p = _project(tmp_path)
    d = deliver.export(p, p / "dist.zip", run_tests=False)
    assert "dist.zip" not in d.files
    d2 = deliver.export(p, p / "dist.zip", run_tests=False)   # a second export over it
    assert "dist.zip" not in d2.files


def test_readme_how_to_run_and_assumptions_are_quoted(tmp_path) -> None:
    p = _project(tmp_path)
    (p / "README.md").write_text(
        "# EGN\n\nУвод.\n\n## Как се пуска\n\n`python egn.py 7501010010`\n\n### Windows\n\n`py egn.py`\n\n"
        "## Допускания\n\n- само ЕГН след 1900\n\n## Лиценз\n\nMIT\n", encoding="utf-8")
    report = deliver.export(p, tmp_path / "out.zip", run_tests=False).report
    assert "python egn.py 7501010010" in report
    assert "py egn.py" in report             # подзаглавието е част от раздела
    assert "само ЕГН след 1900" in report
    assert "MIT" not in report


def test_without_readme_the_run_steps_come_from_the_files(tmp_path) -> None:
    p = _project(tmp_path)
    (p / "requirements.txt").write_text("# deps\nopenpyxl>=3.1\n", encoding="utf-8")
    (p / "main.py").write_text("print(1)\n", encoding="utf-8")
    report = deliver.export(p, tmp_path / "out.zip", run_tests=False).report
    assert "pip install -r requirements.txt" in report
    assert "python main.py" in report
    assert "`openpyxl>=3.1`" in report


def test_decisions_and_open_threads_of_this_folder_are_in_the_report(tmp_path) -> None:
    from genesis_agent import workspace_memory as wm
    p = _project(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    wm.set_workspace(p)
    wm.add_decision("Датите са ДД.ММ.ГГГГ", "така са във фактурите")
    wm.add_thread("CSV експорт", "добави колона ДДС")
    wm.set_workspace(other)
    wm.add_decision("решение от друга папка")
    report = deliver.export(p, tmp_path / "out.zip", run_tests=False).report
    assert "Датите са ДД.ММ.ГГГГ — така са във фактурите" in report
    assert "CSV експорт" in report and "добави колона ДДС" in report
    assert "друга папка" not in report
    assert wm.current_workspace() == wm._norm(other)       # не сменя workspace-а


def test_test_command_in_the_report_has_no_local_interpreter_path(tmp_path) -> None:
    report = deliver.export(_project(tmp_path), tmp_path / "out.zip").report
    assert sys.executable not in report
    assert "`python -m pytest" in report


@pytest.mark.parametrize(("cmd", "shown"), [
    ("/home/u/.venv/bin/python -m pytest -q", "python -m pytest -q"),
    ('"C:\\Program Files\\Python312\\python.exe" -m pytest', "python -m pytest"),
    ("npm test", "npm test"),
])
def test_portable(cmd, shown) -> None:
    assert deliver._portable(cmd) == shown


def test_missing_or_empty_folder_is_a_sentence(tmp_path) -> None:
    with pytest.raises(ValueError, match="Няма такава папка"):
        deliver.export(tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="няма файлове"):
        deliver.export(tmp_path / "empty")


def test_cli_export(tmp_path, capsys) -> None:
    from genesis_agent.cli import main
    p = _project(tmp_path)
    rc = main(["export", str(p), "-o", str(tmp_path / "x.zip")])
    assert rc == 0
    assert "тестовете минават" in capsys.readouterr().out
    assert (tmp_path / "x.zip").is_file()
    assert main(["export", str(p), "--bogus"]) == 2


def test_common_secret_files_stay_out(tmp_path) -> None:
    p = _project(tmp_path)
    secrets = [".envrc", ".streamlit/secrets.toml", "certs/server.key", "token.json",
               "client_secret_1234.json", "config/master.key", "service-account.json", ".htpasswd"]
    for rel in secrets:
        (p / rel).parent.mkdir(parents=True, exist_ok=True)
        (p / rel).write_text("x", encoding="utf-8")
    (p / "deploy.txt").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nAAA\n", encoding="utf-8")
    (p / "settings.toml").write_text("debug = true\n", encoding="utf-8")
    d = deliver.export(p, tmp_path / "out.zip", run_tests=False)
    assert set(secrets) | {"deploy.txt"} <= set(d.withheld)
    assert "settings.toml" in d.files
