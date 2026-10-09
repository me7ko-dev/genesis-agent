"""Голямата проверка 2026-10-09: `genesis fix`, `export`, `mission`, `setup`,
`budget`. Всеки тест пада без поправката си (пуснато срещу стария код)."""
from __future__ import annotations

import builtins
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import ClassVar

import pytest

from genesis_agent import budget, cli, deliver, paths, repo_agent
from genesis_agent import setup_wizard as sw

PY = f'"{sys.executable}" -m pytest -q -p no:cacheprovider'
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX")


def _git(root, *a):
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def _tc(name, args, i=0):
    return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class _Reply:
    def __init__(self, tool_calls=None, raw_text=""):
        self.tool_calls = tool_calls
        self.raw_text = raw_text
        self.code = ""


def _brain(monkeypatch, replies, then=None):
    it = iter(replies)

    class B:
        def __init__(self, **kw):
            pass

        def complete(self, *a, **kw):
            return next(it, then or _Reply(raw_text="готово"))
    monkeypatch.setattr(repo_agent, "Brain", B)


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_agent, "CHECKPOINT_DIR", tmp_path / "cps")
    root = tmp_path / "proj"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    return root


# ── genesis fix ────────────────────────────────────────────────────────────

def test_fix_never_mixes_in_or_destroys_uncommitted_work(proj, monkeypatch):
    _git(proj, "init", "-q")
    (proj / "notes.py").write_text("x = 1\n", encoding="utf-8")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    (proj / "notes.py").write_text("x = 1\n# МОЯТА РАБОТА\n", encoding="utf-8")
    _brain(monkeypatch, [_Reply([_tc("EDIT_FILE", {"path": "calc.py", "old": "a - b", "new": "a + b"})])])
    out = repo_agent.repair(proj, "fix add", test_command=PY, max_rounds=3, on_status=lambda m: None)
    assert out.success and out.checkpoint is not None          # снимка — сама
    assert "МОЯТА РАБОТА" not in out.diff and "calc.py" in out.diff
    assert "checkout ." not in out.summary


def test_a_fix_made_with_a_command_counts(proj, monkeypatch):
    script = proj / "fixer.py"
    script.write_text("import pathlib\np = pathlib.Path('calc.py')\n"
                      "p.write_text(p.read_text().replace('a - b', '(a + b)'))\n", encoding="utf-8")
    _brain(monkeypatch, [_Reply([_tc("RUN_CMD", {"command": f'"{sys.executable}" fixer.py'})])],
           then=_Reply(raw_text="Поправено: add вече събира."))
    script_stamp = script.stat().st_mtime_ns
    out = repo_agent.repair(proj, "add is wrong", test_command=PY, max_rounds=6,
                            on_status=lambda m: None)
    assert script.stat().st_mtime_ns == script_stamp
    assert out.success and "calc.py" in out.files_touched and out.tests_after.passed


def test_green_suite_and_no_edit_is_not_a_success(proj, monkeypatch):
    (proj / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _brain(monkeypatch, [], then=_Reply([_tc("READ_FILE", {"path": "calc.py"})]))
    out = repo_agent.repair(proj, "направи нещо", test_command=PY, max_rounds=3,
                            on_status=lambda m: None)
    assert out.success is False and out.files_touched == []


def test_no_tests_is_not_failing_tests(proj):
    (proj / "test_calc.py").unlink()
    (proj / "pyproject.toml").write_text("[project]\nname='p'\nversion='0'\n", encoding="utf-8")
    from genesis_agent.repo_map import detect_project
    run = repo_agent.run_tests(proj, detect_project(proj).test_command)
    assert run.ran is False


def test_npm_init_placeholder_is_not_a_test_script(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps(
        {"scripts": {"test": "echo \"Error: no test specified\" && exit 1"}}), encoding="utf-8")
    from genesis_agent.repo_map import detect_project
    assert detect_project(tmp_path).test_command == ""


def test_revert_keeps_the_work_done_after_the_snapshot(proj, capsys):
    repo_agent.create_checkpoint(proj)
    (proj / "test_calc.py").write_text("# нови тестове, писани на другия ден\n", encoding="utf-8")
    assert cli.main(["fix", "--revert", str(proj)]) == 0
    out = capsys.readouterr().out
    assert "нови тестове" not in (proj / "test_calc.py").read_text(encoding="utf-8")
    assert "отпреди" in out and "--revert" in out
    assert cli.main(["fix", "--revert", str(proj)]) == 0    # връщането се връща
    assert "нови тестове" in (proj / "test_calc.py").read_text(encoding="utf-8")


@posix_only
def test_snapshots_are_private(proj, tmp_path, monkeypatch):
    home = tmp_path / "fresh_home" / ".genesis"
    monkeypatch.setattr(paths, "GENESIS_HOME", home)
    monkeypatch.setattr(repo_agent, "CHECKPOINT_DIR", home / "checkpoints")
    (proj / ".env").write_text("KEY=1\n", encoding="utf-8")
    cp = repo_agent.create_checkpoint(proj)
    assert stat.S_IMODE(cp.stat().st_mode) == 0o600
    assert stat.S_IMODE(cp.parent.stat().st_mode) == 0o700


def test_revert_without_a_snapshot_fails(proj):
    assert cli.main(["fix", "--revert", str(proj)]) == 1


def test_stale_bytecode_of_an_edited_module_is_dropped(proj):
    cache = proj / "__pycache__"
    cache.mkdir()
    pyc = cache / f"calc.cpython-{sys.version_info[0]}{sys.version_info[1]}.pyc"
    pyc.write_bytes(b"stale")
    other = cache / "other.cpython-312.pyc"
    other.write_bytes(b"keep")
    repo_agent._drop_stale_bytecode(proj, ["calc.py"])
    assert not pyc.exists() and other.exists()


# ── genesis export ─────────────────────────────────────────────────────────

def _names(zp: Path) -> list[str]:
    with zipfile.ZipFile(zp) as z:
        return z.namelist()


def test_export_keeps_nested_build_env_dist_folders(tmp_path):
    root = tmp_path / "app"
    for rel in ("mypkg/__init__.py", "mypkg/build/builder.py", "mypkg/env/settings.py",
                "mypkg/dist/distribution.py", "web/target/aim.js", "main.py", "build/out.bin",
                "backend/env/pyvenv.cfg"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x = 1\n", encoding="utf-8")
    deliver.main([str(root), "--no-tests", "-o", str(tmp_path / "out.zip")])
    names = _names(tmp_path / "out.zip")
    for rel in ("mypkg/build/builder.py", "mypkg/env/settings.py", "mypkg/dist/distribution.py",
                "web/target/aim.js"):
        assert f"app/{rel}" in names
    assert "app/build/out.bin" not in names and "app/backend/env/pyvenv.cfg" not in names


def test_export_withholds_more_secrets_and_history(tmp_path):
    root = tmp_path / "p"
    for rel in ("main.py", ".hg/store/data/x.i", ".svn/pristine/aa", ".terraform/state",
                "terraform.tfstate", ".pgpass", ".dockercfg"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("secret\n", encoding="utf-8")
    keep, withheld = deliver.project_files(root)
    assert [f.relative_to(root).as_posix() for f in keep] == ["main.py"]
    assert {"terraform.tfstate", ".pgpass", ".dockercfg"} <= set(withheld)


@posix_only
def test_export_survives_a_non_utf8_name_and_says_so(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "main.py").write_text("print(1)\n", encoding="utf-8")
    with open(os.fsencode(str(root)) + b"/\xff\xfe-data.txt", "wb") as f:
        f.write(b"x")
    assert deliver.main([str(root), "--no-tests", "-o", str(tmp_path / "o.zip")]) == 0
    report = zipfile.ZipFile(tmp_path / "o.zip").read("p/GENESIS_REPORT.md").decode()
    assert "не е UTF-8" in report


def test_export_zips_the_files_before_the_tests_touch_them(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "report.py").write_text("def make(p):\n    open(p, 'w').write('r')\n", encoding="utf-8")
    (root / "out.txt").write_bytes("истинският изход\n".encode("utf-8"))
    (root / "test_report.py").write_text(
        "import os\nfrom report import make\n\n"
        "def test_make():\n    make('out.txt')\n    os.remove('out.txt')\n", encoding="utf-8")
    deliver.main([str(root), "-o", str(tmp_path / "o.zip")])
    assert not (tmp_path / "o.zip.part").exists()
    with zipfile.ZipFile(tmp_path / "o.zip") as z:
        assert z.read("app/out.txt").decode("utf-8") == "истинският изход\n"
        assert "app/GENESIS_REPORT.md" in z.namelist()


def test_pyproject_dependencies_are_read_without_tomllib(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "tomllib", None)   # като Python 3.10
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='app'\nversion='0'\ndependencies=['requests>=2']\n"
        "[project.optional-dependencies]\ndev = ['pytest']\n", encoding="utf-8")
    from genesis_agent.py_deps import declared
    assert declared(tmp_path) == {"requests", "pytest"}


# ── genesis mission ────────────────────────────────────────────────────────

def test_mission_does_not_save_what_the_critic_rejected(monkeypatch, tmp_path, capsys):
    import genesis_agent.autonomous_loop as al
    from genesis_agent import local_repair_agent
    from genesis_agent.brain import Brain as RealBrain
    from genesis_agent.storage_monitor import StorageReport
    code = ("import sys\n\ndef add(a, b):\n    return a + b\n\nassert add(2, 3) == 5\n"
            "print(\"No such file 'settings.ini' - using defaults\", file=sys.stderr)\nprint('OK')\n")

    class R:
        def __init__(self, raw_text="", code=None):
            self.raw_text, self.code, self.tool_calls, self.usage = raw_text, code, None, None

    class FakeBrain:
        trim_round_history = staticmethod(RealBrain.trim_round_history)
        local = None
        chain: ClassVar[list] = [{"provider": "fake", "model": "fake"}]
        current = None

        def __init__(self, *a, **kw): pass
        def route_for_goal(self, goal): pass
        def build_context(self, goal): return ""
        def system_prompt_base(self): return "sys"
        def escalate(self): return False
        def escalate_to_coding_chain(self): pass
        def generate_local_candidates(self, *a, **kw): return []

        def complete(self, messages, tools=None, avoid=None):
            if "strict code reviewer" in str(messages[0].get("content")):
                return R("NO: the goal says save the sum to result.txt; nothing is written.")
            return R("```python\n" + code + "```", code)
    saved: list = []
    monkeypatch.setattr(al, "Brain", FakeBrain)
    monkeypatch.setattr(al, "check_storage", lambda: StorageReport(
        total_bytes=0, threshold_bytes=1, compression_required=False, log_path=None,
        candidates_path=None))
    monkeypatch.setattr("genesis_agent.telemetry.STATUS_FILE", tmp_path / "s.json")
    monkeypatch.setattr("genesis_agent.reflection.lessons_for_prompt", lambda *a, **kw: "")
    monkeypatch.setattr(al, "save_skill", lambda **kw: saved.append(kw) or al.SKILLS_ROOT / "x.md")
    monkeypatch.setattr(local_repair_agent.TinyLLM, "is_available", lambda self: False)
    monkeypatch.setattr(al, "MAX_LLM_RETRIES", 2)
    rc = cli.main(["mission", "add two numbers and save the sum to result.txt"])
    assert rc == 1 and not saved


def test_mission_help_is_help(monkeypatch, capsys):
    import genesis_agent.autonomous_loop as al
    started: list = []
    monkeypatch.setattr(al, "run_autonomous_loop", lambda goal: started.append(goal))
    assert cli.main(["mission", "--help"]) == 0
    assert not started and "Употреба" in capsys.readouterr().out


# ── genesis setup ──────────────────────────────────────────────────────────

@pytest.fixture
def env_file(tmp_path, monkeypatch):
    home = tmp_path / "gh"
    home.mkdir()
    envf = home / ".env"
    monkeypatch.setattr(sw, "ENV_FILE", envf)
    monkeypatch.setattr(paths, "ENV_FILE", envf)
    monkeypatch.setattr(paths, "ENV_FILES", (str(tmp_path / "install.env"), str(envf)))
    monkeypatch.setattr(sw, "ensure_genesis_home", lambda: home)
    monkeypatch.setattr(sw, "_test_key", lambda *a: (True, "работи"))
    monkeypatch.setattr(sw, "_test_anthropic_key", lambda *a: (True, "работи"))
    for v, *_ in sw.PROVIDERS + sw.PAID_PROVIDERS:
        monkeypatch.delenv(v, raising=False)
    return envf


def _answers(monkeypatch, seq):
    it = iter(seq)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(it, ""))


def test_setup_does_not_copy_a_paid_key_from_the_shell(env_file, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-shell-env-only")
    _answers(monkeypatch, ["hf_new"] + [""] * 4 + ["n"])
    sw.run()
    assert "sk-from-shell-env-only" not in env_file.read_text(encoding="utf-8")


def test_setup_survives_a_non_utf8_env_file(env_file, monkeypatch):
    env_file.write_bytes("# ключове\nHF_TOKEN=hf_old\n".encode("cp1251"))
    _answers(monkeypatch, ["", "", "", "", "", "n"])
    sw.run()
    backups = list(env_file.parent.glob(".env.backup-*"))
    assert backups and backups[0].read_bytes() == "# ключове\nHF_TOKEN=hf_old\n".encode("cp1251")
    assert "HF_TOKEN=hf_old" in env_file.read_text(encoding="utf-8")


# ── genesis budget ─────────────────────────────────────────────────────────

def test_budget_with_a_huge_day_count(capsys):
    assert cli.main(["budget", "1000000"]) == 0


def test_budget_skips_log_lines_that_are_not_records(capsys):
    budget.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    budget.LOG_PATH.write_text('{"ts": "2026-10-09T00:00:00+00:00", "provider": "groq", '
                               '"total_tokens": 5}\n7\n[1]\n', encoding="utf-8")
    assert cli.main(["budget"]) == 0
