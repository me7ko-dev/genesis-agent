"""genesis_agent.code_validate — deterministic tests mock subprocess so they
never depend on ruff actually being installed (it's an optional `quality`
extra, see pyproject.toml). The two @pytest.mark.ruff tests at the bottom
additionally exercise the real binary when it's on PATH, for higher
confidence than mocking alone can give."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from genesis_agent import code_validate


def test_fail_open_when_ruff_missing(monkeypatch) -> None:
    monkeypatch.setattr(code_validate.shutil, "which", lambda _: None)
    ok, detail = code_validate.validate_code_with_ruff("def f(:\n")
    assert ok is True
    assert detail == ""


def test_fail_open_on_timeout(monkeypatch) -> None:
    monkeypatch.setattr(code_validate.shutil, "which", lambda _: "/usr/bin/ruff")

    def raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="ruff", timeout=10)

    monkeypatch.setattr(code_validate.subprocess, "run", raise_timeout)
    ok, detail = code_validate.validate_code_with_ruff("whatever")
    assert ok is True
    assert detail == ""


def test_fail_open_on_oserror(monkeypatch) -> None:
    monkeypatch.setattr(code_validate.shutil, "which", lambda _: "/usr/bin/ruff")
    monkeypatch.setattr(code_validate.subprocess, "run",
                         lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    ok, detail = code_validate.validate_code_with_ruff("whatever")
    assert ok is True
    assert detail == ""


class _FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_clean_code_returns_ok_with_fixed_source(monkeypatch) -> None:
    monkeypatch.setattr(code_validate.shutil, "which", lambda _: "/usr/bin/ruff")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "--fix" in cmd:
            return _FakeCompleted(stdout="def f():\n    return 1\n")
        return _FakeCompleted(stdout="[]", returncode=0)

    monkeypatch.setattr(code_validate.subprocess, "run", fake_run)
    ok, detail = code_validate.validate_code_with_ruff("import os\ndef f():\n    return 1\n")
    assert ok is True
    assert detail == "def f():\n    return 1\n"
    assert len(calls) == 2


def test_unfixable_issues_return_formatted_error_list(monkeypatch) -> None:
    monkeypatch.setattr(code_validate.shutil, "which", lambda _: "/usr/bin/ruff")
    errors = [{"location": {"row": 1}, "code": "F821", "message": "undefined name 'x'"}]

    def fake_run(cmd, **kw):
        if "--fix" in cmd:
            return _FakeCompleted(stdout="x\n")
        return _FakeCompleted(stdout=json.dumps(errors), returncode=1)

    monkeypatch.setattr(code_validate.subprocess, "run", fake_run)
    ok, detail = code_validate.validate_code_with_ruff("x")
    assert ok is False
    assert "F821" in detail
    assert "L1" in detail


def test_error_list_truncated_past_max_shown(monkeypatch) -> None:
    monkeypatch.setattr(code_validate.shutil, "which", lambda _: "/usr/bin/ruff")
    errors = [{"location": {"row": i}, "code": "F821", "message": "x"} for i in range(20)]

    def fake_run(cmd, **kw):
        if "--fix" in cmd:
            return _FakeCompleted(stdout="x\n")
        return _FakeCompleted(stdout=json.dumps(errors), returncode=1)

    monkeypatch.setattr(code_validate.subprocess, "run", fake_run)
    ok, detail = code_validate.validate_code_with_ruff("x")
    assert ok is False
    assert "и още 5" in detail


def test_malformed_ruff_output_surfaces_raw_text(monkeypatch) -> None:
    monkeypatch.setattr(code_validate.shutil, "which", lambda _: "/usr/bin/ruff")

    def fake_run(cmd, **kw):
        if "--fix" in cmd:
            return _FakeCompleted(stdout="x\n")
        return _FakeCompleted(stdout="not json", stderr="ruff crashed", returncode=2)

    monkeypatch.setattr(code_validate.subprocess, "run", fake_run)
    ok, detail = code_validate.validate_code_with_ruff("x")
    assert ok is False
    assert "not json" in detail


_ruff_missing = shutil.which("ruff") is None


@pytest.mark.skipif(_ruff_missing, reason="ruff not installed")
def test_real_ruff_flags_syntax_error() -> None:
    ok, detail = code_validate.validate_code_with_ruff("def f(:\n    pass")
    assert ok is False
    assert "invalid-syntax" in detail or "L1" in detail


@pytest.mark.skipif(_ruff_missing, reason="ruff not installed")
def test_real_ruff_passes_clean_code() -> None:
    ok, detail = code_validate.validate_code_with_ruff("def f():\n    return 1\n")
    assert ok is True
