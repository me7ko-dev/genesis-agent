---
name: 'build_a_stdlib_only_in_process_rate_limiter_usin'
category: 'autonomous'
description: 'Build a stdlib-only in-process rate limiter using a token bucket, with type hints, docstring and an assert-based self-test that prints OK.'
triggers: ["build a stdlib only in process rate limiter usin"]
version: '1.0'
author: 'Genesis'
last_updated: '2026-08-12T15:47:47.662977+00:00'
---

## Описание
Build a stdlib-only in-process rate limiter using a token bucket, with type hints, docstring and an assert-based self-test that prints OK.

## Python Код
```python
"""
In‑process token‑bucket rate limiter.

The implementation uses only the Python standard library and is safe for
concurrent use from multiple threads.

Typical usage
-------------
    limiter = RateLimiter(capacity=5, refill_rate=2.0)   # 2 tokens per second
    if limiter.allow():
        # proceed with the operation
        ...

The bucket holds at most ``capacity`` tokens. Tokens are added continuously
at ``refill_rate`` tokens per second. ``allow()`` consumes a single token
if one is available and returns ``True``; otherwise it returns ``False``.
"""

from __future__ import annotations

import time
import threading
from typing import Final


class RateLimiter:
    """
    Token‑bucket rate limiter.

    Parameters
    ----------
    capacity: int
        Maximum number of tokens the bucket can hold. Must be > 0.
    refill_rate: float
        Number of tokens added to the bucket per second. Must be > 0.
    """

    _capacity: Final[int]
    _refill_rate: Final[float]

    def __init__(self, capacity: int, refill_rate: float) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if refill_rate <= 0:
            raise ValueError("refill_rate must be > 0")
        self._capacity = capacity
        self._refill_rate = refill_rate

        self._tokens: float = float(capacity)          # start full
        self._last_ts: float = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        """Recalculate the current token count based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_ts
        if elapsed > 0:
            added = elapsed * self._refill_rate
            self._tokens = min(self._capacity, self._tokens + added)
            self._last_ts = now

    def allow(self) -> bool:
        """
        Attempt to consume a single token.

        Returns
        -------
        bool
            ``True`` if a token was available and consumed, ``False`` otherwise.
        """
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(capacity={self._capacity}, "
            f"refill_rate={self._refill_rate})"
        )


# --------------------------------------------------------------------------- #
# Self‑test (executed when the module is run directly)
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    # Basic functionality test
    lim = RateLimiter(capacity=2, refill_rate=1.0)  # 1 token per sec, burst of 2

    # Initially we have two tokens
    assert lim.allow() is True, "first token should be allowed"
    assert lim.allow() is True, "second token should be allowed"
    # Bucket is empty now
    assert lim.allow() is False, "third request should be rejected"

    # Wait a bit more than one second – one token should be refilled
    time.sleep(1.12)
    assert lim.allow() is True, "after refill, one token should be allowed"
    assert lim.allow() is False, "no more tokens until next refill"

    # Wait two seconds – bucket should be full again (capacity=2)
    time.sleep(2.05)
    assert lim.allow() is True, "after long wait, token available"
    assert lim.allow() is True, "second token available"
    assert lim.allow() is False, "exhausted again"

    # Concurrency sanity check – run many threads trying to acquire tokens
    successes = 0
    failures = 0
    lock = threading.Lock()

    def worker() -> None:
        global successes, failures
        if lim.allow():
            with lock:
                successes += 1
        else:
            with lock:
                failures += 1

    # Refill bucket fully first
    time.sleep(2.1)  # ensures 2 tokens are back
    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Only two threads should have succeeded because capacity is 2
    assert successes == 2, f"expected 2 successes, got {successes}"
    assert failures == 8, f"expected 8 failures, got {failures}"

    print("OK")
```

## Pitfalls
- OK

