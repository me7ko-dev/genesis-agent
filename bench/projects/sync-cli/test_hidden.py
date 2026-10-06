import os
import subprocess
import sys

import pytest
from sync import load_settings, sync


def write(path, text="x", mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def ini(tmp_path, body):
    p = tmp_path / "my.ini"
    p.write_text("[sync]\n" + body, encoding="utf-8")
    return str(p)


def test_file_only(tmp_path):
    cfg = ini(tmp_path, "source = /data/a\ntarget = /data/b\nexclude = *.tmp, *.log\ndry_run = yes\n")
    s = load_settings(["--config", cfg], {})
    assert s == {"source": "/data/a", "target": "/data/b", "exclude": ["*.tmp", "*.log"], "dry_run": True}


def test_env_beats_file_and_args_beat_env(tmp_path):
    cfg = ini(tmp_path, "source = /f/src\ntarget = /f/dst\nexclude = *.tmp\ndry_run = no\n")
    env = {"SYNC_SOURCE": "/env/src", "SYNC_TARGET": "/env/dst"}
    s = load_settings(["--config", cfg], env)
    assert (s["source"], s["target"], s["dry_run"]) == ("/env/src", "/env/dst", False)
    s = load_settings(["--config", cfg, "--target", "/arg/dst", "--exclude", "*.bak",
                       "--exclude", "~*", "--dry-run"], env)
    assert (s["source"], s["target"]) == ("/env/src", "/arg/dst")
    assert s["exclude"] == ["*.bak", "~*"] and s["dry_run"] is True


def test_no_config_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = load_settings(["--source", "a", "--target", "b"], {})
    assert (s["source"], s["target"], s["exclude"], s["dry_run"]) == ("a", "b", [], False)


def test_default_config_in_the_current_folder(tmp_path, monkeypatch):
    (tmp_path / "config.ini").write_text("[sync]\nsource = s\ntarget = t\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    s = load_settings([], {})
    assert (s["source"], s["target"]) == ("s", "t")


@pytest.fixture
def tree(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    write(src / "a.txt", "new a", mtime=2_000_000)
    write(src / "docs" / "b.md", "b", mtime=2_000_000)
    write(src / "docs" / "deep" / "c.txt", "c", mtime=2_000_000)
    write(src / "cache.tmp", "junk", mtime=2_000_000)
    write(src / "docs" / "run.log", "junk", mtime=2_000_000)
    write(src / "same.txt", "old", mtime=1_000_000)
    write(dst / "same.txt", "kept", mtime=1_500_000)      # target is newer → not copied
    write(dst / "a.txt", "old a", mtime=1_000_000)        # target is older → copied
    return src, dst


def settings(src, dst, **kw):
    return {"source": str(src), "target": str(dst), "exclude": ["*.tmp", "*.log"], "dry_run": False, **kw}


def test_sync(tree):
    src, dst = tree
    assert sync(settings(src, dst)) == ["a.txt", "docs/b.md", "docs/deep/c.txt"]
    assert (dst / "a.txt").read_text("utf-8") == "new a"
    assert (dst / "docs" / "deep" / "c.txt").read_text("utf-8") == "c"
    assert (dst / "same.txt").read_text("utf-8") == "kept"
    assert not (dst / "cache.tmp").exists() and not (dst / "docs" / "run.log").exists()
    assert sync(settings(src, dst)) == []                 # second run: nothing to do


def test_dry_run_touches_nothing(tree):
    src, dst = tree
    before = sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*"))
    assert sync(settings(src, dst, dry_run=True)) == ["a.txt", "docs/b.md", "docs/deep/c.txt"]
    assert sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*")) == before
    assert (dst / "a.txt").read_text("utf-8") == "old a"


def run_cli(args, cwd):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("SYNC_SOURCE", None)
    env.pop("SYNC_TARGET", None)
    return subprocess.run([sys.executable, "-m", "sync", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=60, check=False)


def test_cli(tree, tmp_path):
    src, dst = tree
    r = run_cli(["--source", str(src), "--target", str(dst), "--exclude", "*.tmp", "--exclude", "*.log"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "docs/deep/c.txt" in r.stdout and "a.txt" in r.stdout
    assert (dst / "docs" / "b.md").exists()


@pytest.mark.parametrize("args", [["--source", "nope", "--target", "x"], ["--target", "x"], []])
def test_cli_errors(tmp_path, args):
    r = run_cli(args, tmp_path)
    assert r.returncode == 2
    assert r.stderr.strip()
    assert "Traceback" not in r.stderr
