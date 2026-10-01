"""One chat message from the terminal, even when it spans several lines.

input() returns at the first Enter, so a pasted multi-line task arrived as one
message per line — bench_fcc 2026-09-29: a 16-line task became 16 turns, the
first ("write it in solution.py") without the task, and the model went to
search the web for it. Two ways a message now spans lines:

- a paste: after the first line, whatever is already waiting in the console
  is the rest of that paste, not something the user typed after reading a reply;
- a block: a line that is only \"\"\" opens it, the next such line closes it —
  for scripts and pipes, where nothing can tell a paste from separate lines.
"""
from __future__ import annotations

import sys
from collections.abc import Callable

BLOCK = '"""'


def pending_console_input() -> bool:
    """True when more of a paste is already waiting in an interactive console."""
    try:
        if not sys.stdin.isatty():
            return False
        if sys.platform == "win32":
            import msvcrt
            return bool(msvcrt.kbhit())
        import select
        return bool(select.select([sys.stdin], [], [], 0.02)[0])
    except (OSError, ValueError, AttributeError):
        return False


def read_message(first_line: Callable[[], str],
                 more: Callable[[], str] = input,
                 pending: Callable[[], bool] = pending_console_input) -> str:
    """The whole message; EOF inside a block ends the block, not the chat."""
    line = first_line()
    if line.strip() == BLOCK:
        lines: list[str] = []
        while True:
            try:
                nxt = more()
            except EOFError:
                break
            if nxt.strip() == BLOCK:
                break
            lines.append(nxt)
        return "\n".join(lines).strip()
    lines = [line]
    while pending():
        try:
            lines.append(more())
        except EOFError:
            break
    return "\n".join(lines).strip()
