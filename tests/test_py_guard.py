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
    # Изрично „deny“ (автономен режим): глобалната политика може да е оставена
    # от друг тест — тук мерим пазача, не нея.
    return sandbox.run_python(code, timeout=60, policy=policy or sandbox.SandboxPolicy(mode="deny"))


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
    # Работната папка е постоянна, но не е в sys.path: там е само новата
    # папка на пускането.
    first = sandbox.run_python("open('runpy.py', 'w').write('print(\"PLANTED\")')\nprint('ok')\n",
                               timeout=60, cwd=tmp_path)
    assert first.ok, first.stderr
    second = sandbox.run_python("print('innocent')\n", timeout=60, cwd=tmp_path)
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


# ── преглед за регресии 2026-10-09 ───────────────────────────────────────────

def _approve(_op, _verdict) -> bool:
    return True


_ASKING = sandbox.SandboxPolicy(mode="interactive", confirm_fn=_approve)


@pytest.mark.parametrize("code", [
    "from subprocess import run\nimport sys\nrun([sys.executable, '-c', 'print(7)'])\n",
    "import subprocess as sp, sys\nprint(sp.check_output([sys.executable, '-c', 'print(7)'], text=True))\n",
    ("from concurrent.futures import ProcessPoolExecutor\n"
     "def sq(x):\n    return x * x\n"
     "if __name__ == '__main__':\n"
     "    with ProcessPoolExecutor(1) as ex:\n        print(sum(ex.map(sq, [1, 2])) + 2)\n"),
])
def test_other_ways_to_start_a_process_are_asked_not_silently_refused(code) -> None:
    asked: list[str] = []

    def approve(_op, verdict) -> bool:
        asked.extend(verdict.reasons)
        return True
    res = _run(code, policy=sandbox.SandboxPolicy(mode="interactive", confirm_fn=approve))
    assert "стартиране на подпроцес" in asked
    assert res.ok and "7" in res.stdout, res.stderr


@pytest.mark.skipif(not os.path.exists("/bin/sh"), reason="POSIX")
def test_the_stdlib_may_ask_the_system_about_itself() -> None:
    import platform
    res = _run("import platform\nprint(repr(platform.processor()))\n")
    assert res.ok and res.stdout.strip() == repr(platform.processor()), res.stderr


def test_copying_out_of_the_python_install_is_a_read(tmp_path) -> None:
    res = sandbox.run_python("import json, shutil\nshutil.copy(json.__file__, 'json_copy.py')\n"
                             "print('copied')\n", timeout=60, cwd=tmp_path)
    assert res.ok and (tmp_path / "json_copy.py").is_file(), res.stderr


def test_a_workspace_inside_the_python_prefix_can_be_written(tmp_path, monkeypatch) -> None:
    import sys as _sys
    ws = Path(_sys.prefix) / "genesis-test-ws-not-real"
    # Без да пишем в истинския prefix: пускаме с позволена папка в tmp и
    # проверяваме само реда на правилата — позволеното печели пред библиотеките.
    work = tmp_path / "app"
    work.mkdir()
    res = sandbox.run_python("open('out.txt', 'w').write('ok')\nprint('ok')\n", timeout=60,
                             cwd=work, allow=[work, ws])
    assert res.ok and (work / "out.txt").read_text() == "ok", res.stderr


def test_relative_output_without_cwd_survives_the_run() -> None:
    first = _run("import os\nopen('state.json', 'w').write('{\"n\": 1}')\nprint(os.path.abspath('state.json'))\n")
    path = Path(first.stdout.strip())
    try:
        assert first.ok and path.is_file(), first.stderr
        second = _run("print(open('state.json').read())\n")
        assert '"n": 1' in second.stdout, second.stderr
    finally:
        path.unlink(missing_ok=True)


def test_the_script_does_not_shadow_a_project_main_module(tmp_path) -> None:
    (tmp_path / "main.py").write_text("def greet():\n    return 'hi from project'\n", encoding="utf-8")
    res = _run(f"import sys\nsys.path.append({str(tmp_path)!r})\nfrom main import greet\nprint(greet())\n")
    assert res.ok and "hi from project" in res.stdout, res.stderr


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="fwalk — Linux")
def test_fwalk_and_rmtree_see_the_whole_tree(tmp_path) -> None:
    (tmp_path / "d" / "e").mkdir(parents=True)
    (tmp_path / "d" / "e" / "f.txt").write_text("x", encoding="utf-8")
    res = sandbox.run_python("import os, shutil\nprint(sorted(r for r, _d, _f, _fd in os.fwalk('d')))\n"
                             "shutil.rmtree('d')\nprint(os.path.exists('d'))\n", timeout=60, cwd=tmp_path,
                             policy=_ASKING)
    assert res.ok and "'d/e'" in res.stdout and "False" in res.stdout, res.stderr



def test_windows_needs_reach_the_child(monkeypatch) -> None:
    # Windows CI: subprocess от одобрено умение — „neither %ComSpec% nor %SystemRoot% is set“.
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\system32\cmd.exe")
    env = sandbox._build_env(sandbox.SandboxPolicy())
    assert env["SYSTEMROOT"] == r"C:\Windows" and env["COMSPEC"].endswith("cmd.exe")


def test_the_guard_settings_are_not_left_in_the_environment() -> None:
    res = _run("import os\nprint(sorted(k for k in os.environ if k.startswith('GENESIS_GUARD')))\n")
    assert res.ok and res.stdout.strip() == "[]", res.stderr
