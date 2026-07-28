---
name: 'build_a_stdlib_only_async_timeout_cancellation_w'
category: 'autonomous'
description: 'Build a stdlib-only async timeout-cancellation wrapper with type hints, docstring and an assert self-test printing OK.'
triggers: ["build a stdlib only async timeout cancellation w"]
version: '1.0'
author: 'Genesis'
last_updated: '2026-07-27T13:55:05.095443+00:00'
---

## Описание
Build a stdlib-only async timeout-cancellation wrapper with type hints, docstring and an assert self-test printing OK.

## Python Код
```python
"""Async timeout‑cancellation wrapper (stdlib only).

Provides :func:`async_timeout` – runs an awaitable with a deadline.  If the
awaitable does not finish within *timeout* seconds, the task is cancelled
and :class:`asyncio.TimeoutError` is raised.

The implementation uses :func:`asyncio.wait_for`, which automatically
cancels the underlying task on timeout.

Parameters
----------
awaitable: Awaitable[T]
    Any coroutine or awaitable object that produces a result of type ``T``.
timeout: float
    Maximum number of seconds to allow the awaitable to run.  Must be
    non‑negative.

Returns
-------
T
    The result produced by *awaitable* if it completes before the deadline.

Raises
------
asyncio.TimeoutError
    If the timeout expires before *awaitable* finishes.
asyncio.CancelledError
    Propagated if the surrounding task is cancelled.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, TypeVar

T = TypeVar("T")

async def async_timeout(awaitable: Awaitable[T], timeout: float) -> T:
    """Run *awaitable* with a timeout, cancelling it on expiry.

    Args:
        awaitable: The coroutine/awaitable to execute.
        timeout: Maximum time in seconds to wait.

    Returns:
        The result of *awaitable*.

    Raises:
        asyncio.TimeoutError: If the timeout elapses.
    """
    return await asyncio.wait_for(awaitable, timeout)


# --------------------------------------------------------------------------- #
# Self‑test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    async def fast() -> int:
        await asyncio.sleep(0.01)
        return 42

    async def slow() -> int:
        await asyncio.sleep(0.5)
        return -1

    async def _run_tests() -> None:
        # Successful completion before timeout
        result = await async_timeout(fast(), timeout=1.0)
        assert result == 42, "fast() should return 42"

        # Timeout should raise asyncio.TimeoutError
        try:
            await async_timeout(slow(), timeout=0.1)
            raise AssertionError("TimeoutError was not raised")
        except asyncio.TimeoutError:
            pass  # Expected

    asyncio.run(_run_tests())
    print("OK")
```

## Pitfalls
- OK

