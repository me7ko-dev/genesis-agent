"""Одит, седми кръг (2026-11-10) — code_check.runs_code не познаваше обичайните
форми на пускане под Windows: „It works now.“ след зелено пускане се броеше за
твърдение без пускане, а изходът на такова пускане не се четеше за разминаване."""
from __future__ import annotations

import pytest

from genesis_agent.claim_check import counts_as_executed, unsupported_claims
from genesis_agent.code_check import MismatchCheck, runs_code

_RUNS = [
    "node.exe app.js",
    '"C:\\Program Files\\nodejs\\node.exe" app.js',
    ".\\app.exe",
    "app.exe",
    "build\\Release\\app.exe 5",
    "run.bat",
    ".\\run.cmd",
    "powershell -ExecutionPolicy Bypass -File .\\run.ps1",
    "uv run app.py",
    "make run",
    "tsx app.ts",
    "Rscript a.R",
]


def _result(command: str, output: str) -> str:
    return f"[RUN_CMD: {command}]  (rc=0)\n{output}"


@pytest.mark.parametrize("command", _RUNS)
def test_a_windows_launch_counts_as_running_the_code(command: str) -> None:
    assert runs_code(command)
    executed = [("EDIT_FILE", "app.js"),
                counts_as_executed("RUN_CMD", command, _result(command, "42"))]
    assert not unsupported_claims("It works now.", executed)
    check = MismatchCheck()
    check.observe(_result(command, "case 2: expected 4, got 3\nAll tests passed!"))
    assert check.due("Готово.")


@pytest.mark.parametrize("command", [
    "cat run.ps1", "git add run.bat app.exe", "ls build/Release", "type notes.txt",
    "dir *.exe", "git log -- tools/x.exe", "echo done",
])
def test_naming_a_program_is_not_running_it(command: str) -> None:
    assert not runs_code(command)
