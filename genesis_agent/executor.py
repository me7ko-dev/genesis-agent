"""The Executor — run LLM-produced Python in a subprocess; capture stdout/stderr."""

from __future__ import annotations

import io
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path


from genesis_agent import dna, sandbox
from genesis_agent.config import EXEC_TIMEOUT_SEC, SANDBOX_DIR


@dataclass
class ExecResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int | None


def _ensure_sandbox() -> Path:
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    return SANDBOX_DIR


def run_python_subprocess(code: str, *, cwd: Path | None = None) -> ExecResult:
    """
    Execute code in a fresh interpreter process via genesis_agent.sandbox.

    The sandbox enforces the real boundary: minimal env (no API-key leak),
    resource limits (CPU/memory/file size), process-group kill on timeout, and
    the SAFE/CONFIRM/BLOCKED risk gate. This is best-effort isolation, not a
    full VM — a dedicated cgroups/container deployment is still recommended for
    fully untrusted code.
    """
    _ensure_sandbox()
    gate = dna.validate_code_before_execution(code)
    if gate:
        return ExecResult(ok=False, stdout="", stderr=f"[GENESIS DNA] {gate}", returncode=None)

    res = sandbox.run_python(code, cwd=cwd, timeout=EXEC_TIMEOUT_SEC)
    return ExecResult(
        ok=res.ok,
        stdout=res.stdout,
        stderr=res.stderr,
        returncode=res.returncode,
    )


def run_python_inprocess(code: str) -> ExecResult:
    """
    Run code in the current process (NOT recommended for untrusted LLM output).
    Reserved for tiny trusted snippets; default path is subprocess.
    """
    gate = dna.validate_code_before_execution(code)
    if gate:
        return ExecResult(ok=False, stdout="", stderr=f"[GENESIS DNA] {gate}", returncode=None)
    buf_out = io.StringIO()
    buf_err = io.StringIO()
    rc = 0
    try:
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            ns: dict[str, object] = {"__name__": "__genesis_exec__"}
            exec(compile(code, "<genesis_exec>", "exec"), ns, ns)
    except Exception:
        rc = 1
        print(traceback.format_exc(), file=buf_err)
    return ExecResult(ok=rc == 0, stdout=buf_out.getvalue(), stderr=buf_err.getvalue(), returncode=rc)


def format_failure_for_brain(result: ExecResult) -> str:
    """Bundle stderr + stdout for self-correction prompts."""
    parts = []
    if result.stderr.strip():
        parts.append("### stderr\n" + result.stderr.strip())
    if result.stdout.strip():
        parts.append("### stdout\n" + result.stdout.strip())
    if result.returncode is not None:
        parts.append(f"### return code\n{result.returncode}")
    return "\n\n".join(parts) if parts else "(no output captured)"
