"""Entry point of the native Windows build (genesis.exe, see genesis.spec).

genesis.exe is two programs in one file:

  * `genesis ...`            the normal CLI (genesis_agent.cli.main);
  * `genesis script.py ...`, `genesis -c CODE`, `genesis -m MODULE`
                             a Python interpreter.

The second exists because the agent runs Python through `sys.executable`
(sandbox.run_python, and generated code that calls `[sys.executable, ...]`).
In a pip install that is the venv's python.exe; here it is genesis.exe. With
this, the sandbox works on a machine with no Python installed at all — the
build carries the whole standard library plus requests/yaml/rich, i.e. what
the pipx venv had. A user project's own tests still go to the real Python on
the machine (paths.project_python), because only that one has its packages.
"""
from __future__ import annotations

import os
import sys

# Flags a real `python` accepts before the script. `-X utf8` and `-W x` take a
# value; the rest are switches. Unknown flags mean "not a Python invocation".
_SWITCHES = {"-u", "-B", "-E", "-s", "-S", "-I", "-O", "-OO", "-q", "-b", "-bb"}
_WITH_VALUE = {"-X", "-W"}


def python_invocation(argv: list[str]) -> tuple[str, str, list[str]] | None:
    """(kind, target, rest) if argv looks like `python ...`, else None.

    kind is "c" (target = code), "m" (target = module) or "path" (target =
    script). Never matches a genesis subcommand: those are bare words.
    """
    i = 0
    while i < len(argv) and (argv[i] in _SWITCHES or argv[i] in _WITH_VALUE):
        i += 2 if argv[i] in _WITH_VALUE else 1
    if i >= len(argv):
        return None
    head, rest = argv[i], argv[i + 1:]
    if head in ("-c", "-m"):
        if not rest:
            return None
        return head[1], rest[0], rest[1:]
    if head.lower().endswith((".py", ".pyw")) and not head.startswith("-"):
        return "path", head, rest
    return None


def run_python(kind: str, target: str, rest: list[str]) -> int:
    import runpy
    import traceback

    # The bootloader ignores PYTHON* variables; honour the two the sandbox sets.
    for entry in reversed([p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]):
        sys.path.insert(0, entry)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass
    try:
        if kind == "c":
            sys.argv = ["-c", *rest]
            sys.path.insert(0, "")
            exec(compile(target, "<string>", "exec"), {"__name__": "__main__"})  # noqa: S102 — this IS the interpreter
        elif kind == "m":
            sys.argv = [target, *rest]
            sys.path.insert(0, os.getcwd())
            runpy.run_module(target, run_name="__main__", alter_sys=True)
        else:
            sys.argv = [target, *rest]
            sys.path.insert(0, os.path.dirname(os.path.abspath(target)))
            runpy.run_path(target, run_name="__main__")
    except SystemExit:
        raise
    except BaseException:  # same contract as python.exe: traceback, exit 1
        traceback.print_exc()
        return 1
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (AttributeError, ValueError, OSError):
                pass
    return 0


def main() -> int:
    import multiprocessing
    multiprocessing.freeze_support()

    invocation = python_invocation(sys.argv[1:])
    if invocation is not None:
        return run_python(*invocation)

    from genesis_agent.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
