"""Пазачът на Python кода от модела (py_guard, 2026-10-09): тайните са
затворени и по сглобен път — assess_code вижда само буквалните пътища, а
`Path.home().parents[1] / ".ssh"` или `"~/.s" + "sh"` минаваха."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from genesis_agent import paths, sandbox


@pytest.fixture
def secret(tmp_path) -> Path:
    key = tmp_path / "victim" / ".ssh" / "id_rsa"
    key.parent.mkdir(parents=True)
    key.write_text("-----BEGIN KEY----- TOP-SECRET", encoding="utf-8")
    return key


def _run(code: str, policy=None):
    return sandbox.run_python(code, timeout=60, policy=policy)


def test_a_secret_on_an_assembled_path_is_refused(secret) -> None:
    base = str(secret.parent.parent)
    res = _run(f"import os\np = os.path.join({base!r}, '.s' + 'sh', 'id' + '_rsa')\n"
               "print(open(p).read())\n")
    assert not res.blocked                                     # assess_code не го видя
    assert "TOP-SECRET" not in res.stdout and "отказан" in res.stderr


def test_the_genesis_home_is_closed(tmp_path) -> None:
    home = Path(paths.GENESIS_HOME)
    (home / "notes.txt").write_text("вътрешно", encoding="utf-8")
    res = _run(f"import os\nprint(os.listdir({str(home)!r}))\n")
    assert "notes.txt" not in res.stdout and "отказан" in res.stderr


@pytest.mark.skipif(os.name == "nt", reason="cat — POSIX")
def test_a_command_at_a_secret_is_refused(secret) -> None:
    base = str(secret.parent.parent)
    res = _run(f"import subprocess\nsubprocess.run(['cat', {base!r} + '/.s' + 'sh/id' + '_rsa'])\n",
               policy=sandbox.SandboxPolicy(mode="allow"))     # подпроцесът иска одобрение
    assert "TOP-SECRET" not in res.stdout and "отказана" in res.stderr


def test_ordinary_code_still_runs(tmp_path) -> None:
    data = tmp_path / "data.csv"
    data.write_text("a,b\n1,2\n", encoding="utf-8")
    res = _run("from __future__ import annotations\nimport csv, json, email\n"
               f"print(len(list(csv.reader(open({str(data)!r})))))\n")
    assert res.ok and res.stdout.strip() == "2", res.stderr


def test_access_the_operator_approved_runs_without_the_guard(secret) -> None:
    # Буквален път до тайна → въпрос „достъп до чувствителни файлове“ → да.
    asked: list[list[str]] = []

    def approve(_op, verdict) -> bool:
        asked.append(list(verdict.reasons))
        return True
    policy = sandbox.SandboxPolicy(mode="interactive", confirm_fn=approve)
    res = _run(f"print(open({str(secret)!r}).read())\n", policy=policy)
    assert "TOP-SECRET" in res.stdout and sandbox._SECRET_REASON in asked[0]


# ── одит на пазача 2026-10-09 ────────────────────────────────────────────────

def _base(secret: Path) -> str:
    return str(secret.parent.parent)


@pytest.mark.parametrize("starter", [
    ("import multiprocessing as mp\n"
     "def leak():\n    print(open(P).read())\n"
     "if __name__ == '__main__':\n"
     "    p = mp.get_context('spawn').Process(target=leak); p.start(); p.join()\n"),
    "import subprocess\nprint(subprocess.getoutput('cat ' + P))\n",
])
def test_no_process_starts_without_approval(secret, starter) -> None:
    code = f"import os\nP = os.path.join({_base(secret)!r}, '.s' + 'sh', 'id' + '_rsa')\n" + starter
    res = _run(code)
    assert "TOP-SECRET" not in res.stdout + res.stderr
    assert "процес" in res.stderr


@pytest.mark.skipif(not hasattr(os, "posix_spawn"), reason="posix_spawn — POSIX")
def test_posix_spawn_is_refused_too(secret) -> None:
    code = (f"import os, sys\nP = os.path.join({_base(secret)!r}, '.s' + 'sh', 'id' + '_rsa')\n"
            "os.posix_spawn(sys.executable, [sys.executable, '-c', 'print(open(%r).read())' % P],"
            " dict(os.environ))\n")
    res = _run(code)
    assert "TOP-SECRET" not in res.stdout and "процес" in res.stderr


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="dir_fd — Linux")
def test_reading_through_dir_fd_is_refused(secret) -> None:
    code = (f"import os\nh = os.open({_base(secret)!r}, os.O_RDONLY)\n"
            "s = os.open('.s' + 'sh', os.O_RDONLY, dir_fd=h)\n"
            "k = os.open('id' + '_rsa', os.O_RDONLY, dir_fd=s)\nprint(os.read(k, 100))\n")
    res = _run(code)
    assert "TOP-SECRET" not in res.stdout and "dir_fd" in res.stderr


def test_listing_the_genesis_home_by_fd_is_refused() -> None:
    home = Path(paths.GENESIS_HOME)
    (home / "notes.txt").write_text("вътрешно", encoding="utf-8")
    code = f"import os\nfd = os.open({str(home)!r}, os.O_RDONLY)\nprint(os.listdir(fd))\n"
    res = _run(code)
    assert "notes.txt" not in res.stdout and "отказан" in res.stderr


def test_sqlite_on_genesis_data_is_refused() -> None:
    db = Path(paths.GENESIS_HOME) / "memory.db"
    code = (f"import sqlite3\nc = sqlite3.connect({str(db)!r})\n"
            "c.execute('create table t(x)'); c.commit(); print('wrote')\n")
    res = _run(code)
    assert "wrote" not in res.stdout and "отказан" in res.stderr


def test_a_comment_with_env_does_not_switch_the_guard_off(secret) -> None:
    asked: list[list[str]] = []

    def approve(_op, verdict) -> bool:
        asked.append(list(verdict.reasons))
        return True
    code = ("# settings, as python-dotenv does for a .env file\nimport os, subprocess\n"
            "subprocess.run(['true'])\n"
            f"print(open(os.path.join({_base(secret)!r}, '.s' + 'sh', 'id' + '_rsa')).read())\n")
    res = _run(code, policy=sandbox.SandboxPolicy(mode="interactive", confirm_fn=approve))
    assert "TOP-SECRET" not in res.stdout and "отказан" in res.stderr


def test_allow_mode_keeps_the_guard(secret) -> None:
    res = _run(f"print(open({str(secret)!r}).read())\n", policy=sandbox.SandboxPolicy(mode="allow"))
    assert "TOP-SECRET" not in res.stdout


def test_writing_into_the_user_site_is_refused(tmp_path) -> None:
    userbase = tmp_path / "userbase"
    code = ("import site, os\nd = site.getusersitepackages(); os.makedirs(d, exist_ok=True)\n"
            "open(os.path.join(d, 'x.pth'), 'w').write('import os')\nprint('planted')\n")
    res = sandbox.run_python(code, timeout=60, env_extra={"PYTHONUSERBASE": str(userbase)})
    assert "planted" not in res.stdout
    assert not list(userbase.rglob("*.pth"))


def test_a_file_left_by_one_run_is_not_in_the_next(tmp_path) -> None:
    first = _run("open('runpy.py', 'w').write('print(\"PLANTED\")')\nprint('ok')\n")
    assert first.ok, first.stderr
    second = _run("print('innocent')\n")
    assert second.stdout.strip() == "innocent"


def test_ordinary_project_files_with_secret_like_names_are_readable(tmp_path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_credentials.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("KEY=\n", encoding="utf-8")
    code = (f"import os\nb = {str(tmp_path)!r}\n"
            "print(open(os.path.join(b, 'tests', 'test_cred' + 'entials.py')).read().strip())\n"
            "print(open(os.path.join(b, '.e' + 'nv.example')).read().strip())\n")
    res = sandbox.run_python(code, timeout=60, cwd=tmp_path)
    assert res.ok and "X = 1" in res.stdout and "KEY=" in res.stdout, res.stderr


def test_a_workspace_inside_the_genesis_home_is_allowed() -> None:
    ws = Path(paths.GENESIS_HOME) / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "data.csv").write_text("a,b\n", encoding="utf-8")
    res = sandbox.run_python(f"print(open({str(ws / 'data.csv')!r}).read())", timeout=60,
                             allow=[ws])
    assert res.ok and "a,b" in res.stdout, res.stderr


def test_wrapping_an_already_open_descriptor_works(tmp_path) -> None:
    # Windows CI 2026-10-09: open(fd) се отказваше (няма /proc) — и така всяко
    # tempfile.mkstemp + os.fdopen, и subprocess отвътре.
    code = (f"import os, tempfile\nfd, p = tempfile.mkstemp(dir={str(tmp_path)!r})\n"
            "with os.fdopen(fd, 'w') as f:\n    f.write('ok')\nprint(open(p).read())\n")
    res = sandbox.run_python(code, timeout=60)
    assert res.ok and res.stdout.strip() == "ok", res.stderr
