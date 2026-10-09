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
    # Буквален път до тайна → CONFIRM → одобрен: операторът видя какво се чете.
    policy = sandbox.SandboxPolicy(mode="allow")
    res = _run(f"print(open({str(secret)!r}).read())\n", policy=policy)
    assert "TOP-SECRET" in res.stdout
