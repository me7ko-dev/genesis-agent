import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# found, not imported: a script without a __main__ guard is still a valid answer
SCRIPT = importlib.util.find_spec("todo").origin


def strings(obj):
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        obj = list(obj.values())
    if isinstance(obj, list):
        return [s for v in obj for s in strings(v)]
    return []


@pytest.fixture
def run(tmp_path):
    def _run(*args, env=None):
        e = {k: v for k, v in os.environ.items() if not k.startswith("TODO_")}
        e["PYTHONUTF8"] = "1"
        e.update(env or {})
        return subprocess.run([sys.executable, SCRIPT, *args], cwd=tmp_path, env=e,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, check=False)
    return _run


def lines(r):
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def test_add_list_done(run, tmp_path):
    a = run("add", "Купи хляб")
    assert a.returncode == 0, a.stderr
    first = int(a.stdout.strip())
    second = int(run("add", "Плати тока", "--priority", "1").stdout.strip())
    third = int(run("add", "Звънни на мама", "--priority", "3").stdout.strip())
    assert len({first, second, third}) == 3
    assert (tmp_path / "tasks.json").is_file()
    assert lines(run("list")) == [
        f"[ ] {second} (P1) Плати тока",
        f"[ ] {first} (P2) Купи хляб",
        f"[ ] {third} (P3) Звънни на мама",
    ]
    assert run("done", str(second)).returncode == 0
    assert lines(run("list")) == [f"[ ] {first} (P2) Купи хляб", f"[ ] {third} (P3) Звънни на мама"]
    assert lines(run("list", "--all")) == [
        f"[x] {second} (P1) Плати тока",
        f"[ ] {first} (P2) Купи хляб",
        f"[ ] {third} (P3) Звънни на мама",
    ]


def test_same_priority_by_id(run):
    ids = [int(run("add", t).stdout.strip()) for t in ("А", "Б", "В")]
    assert lines(run("list")) == [f"[ ] {i} (P2) {t}" for i, t in zip(ids, "АБВ")]


def test_done_unknown_id(run):
    run("add", "x")
    r = run("done", "999")
    assert r.returncode == 1
    assert r.stderr.strip()


def test_precedence_file(run, tmp_path):
    (tmp_path / "todo.toml").write_text('file = "from_toml.json"\n', encoding="utf-8")
    run("add", "от toml")
    assert (tmp_path / "from_toml.json").is_file()
    run("add", "от env", env={"TODO_FILE": str(tmp_path / "from_env.json")})
    assert (tmp_path / "from_env.json").is_file()
    run("--file", "from_flag.json", "add", "от флаг", env={"TODO_FILE": str(tmp_path / "from_env.json")})
    assert (tmp_path / "from_flag.json").is_file()
    texts = {name: [t for t in strings(json.loads((tmp_path / name).read_text("utf-8"))) if t.startswith("от ")]
             for name in ("from_toml.json", "from_env.json", "from_flag.json")}
    assert texts == {"from_toml.json": ["от toml"], "from_env.json": ["от env"],
                     "from_flag.json": ["от флаг"]}
    assert not (tmp_path / "tasks.json").exists()


def test_precedence_priority(run, tmp_path):
    (tmp_path / "todo.toml").write_text("default_priority = 3\n", encoding="utf-8")
    a = int(run("add", "toml").stdout.strip())
    b = int(run("add", "env", env={"TODO_PRIORITY": "1"}).stdout.strip())
    c = int(run("--default-priority", "2", "add", "flag", env={"TODO_PRIORITY": "1"}).stdout.strip())
    d = int(run("add", "explicit", "--priority", "1").stdout.strip())
    out = lines(run("list"))
    assert f"[ ] {a} (P3) toml" in out
    assert f"[ ] {b} (P1) env" in out
    assert f"[ ] {c} (P2) flag" in out
    assert f"[ ] {d} (P1) explicit" in out


@pytest.mark.parametrize("args, env, toml", [
    (("add", "x", "--priority", "5"), {}, None),
    (("--default-priority", "0", "add", "x"), {}, None),
    (("add", "x"), {"TODO_PRIORITY": "9"}, None),
    (("add", "x"), {}, "default_priority = 4\n"),
])
def test_bad_priority(run, tmp_path, args, env, toml):
    if toml:
        (tmp_path / "todo.toml").write_text(toml, encoding="utf-8")
    r = run(*args, env=env)
    assert r.returncode == 2
    assert r.stderr.strip()
    assert not Path(tmp_path / "tasks.json").exists() or "x" not in (tmp_path / "tasks.json").read_text("utf-8")
