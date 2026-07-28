---
name: 'build_a_stdlib_only_async_semaphore_bounded_fan'
category: 'autonomous'
description: 'Build a stdlib-only async semaphore-bounded fan-out/fan-in with type hints, docstring and an assert self-test printing OK.'
triggers: ["build a stdlib only async semaphore bounded fan"]
version: '1.0'
author: 'Genesis'
last_updated: '2026-07-27T13:52:53.042568+00:00'
---

## Описание
Build a stdlib-only async semaphore-bounded fan-out/fan-in with type hints, docstring and an assert self-test printing OK.

## Python Код
```python
import asyncio
from typing import Iterable, Awaitable, List, TypeVar

T = TypeVar('T')

async def bounded_gather(coros: Iterable[Awaitable[T]], limit: int) -> List[T]:
    """
    Run awaitable objects concurrently with a concurrency limit.

    Parameters
    ----------
    coros: Iterable[Awaitable[T]]
        An iterable of awaitable objects (coroutines, Tasks, etc.).
    limit: int
        Maximum number of awaitables that may run simultaneously.
        Must be > 0.

    Returns
    -------
    List[T]
        List of results in the same order as the input iterable.
        If any awaitable raises an exception, that exception is
        propagated immediately (like asyncio.gather with the default
        return_exceptions=False).

    Notes
    -----
    An asyncio.Semaphore is used to bound the number of concurrently
    executing awaitables. Each awaitable is wrapped in a worker that
    acquires the semaphore, runs the awaitable, and releases the semaphore.
    The workers are scheduled as asyncio.Tasks and awaited with
    asyncio.gather to preserve order.
    """
    if limit <= 0:
        raise ValueError("limit must be > 0")
    sem = asyncio.Semaphore(limit)

    async def worker(coro: Awaitable[T]) -> T:
        async with sem:
            return await coro

    tasks = [asyncio.create_task(worker(c)) for c in coros]
    return await asyncio.gather(*tasks)


async def _self_test() -> None:
    # Shared counter to track active tasks
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def make_coro(delay: float, idx: int) -> int:
        nonlocal active, max_active
        async with lock:
            active += 1
            if active > max_active:
                max_active = active
        try:
            await asyncio.sleep(delay)
            return idx
        finally:
            async with lock:
                active -= 1

    N = 10
    limit = 3
    coros = [make_coro(0.01 * (N - i), i) for i in range(N)]  # varying delays
    results = await bounded_gather(coros, limit)

    # Verify order and correctness
    assert results == list(range(N)), f"results mismatch: {results}"
    # Verify concurrency limit respected
    assert max_active <= limit, f"max_active {max_active} > limit {limit}"
    # Ensure all tasks completed
    assert active == 0, f"some tasks still active: {active}"
    print("OK")


if __name__ == "__main__":
    asyncio.run(_self_test())
```

## Pitfalls
- OK

