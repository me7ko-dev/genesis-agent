---
name: 'build_a_stdlib_only_in_process_job_queue_with_re'
category: 'autonomous'
description: 'Build a stdlib-only in-process job queue with retry and exponential backoff, with type hints, docstring and an assert-based self-test that prints OK.'
triggers: ["build a stdlib only in process job queue with re"]
version: '1.0'
author: 'Genesis'
last_updated: '2026-08-12T15:37:22.066407+00:00'
---

## Описание
Build a stdlib-only in-process job queue with retry and exponential backoff, with type hints, docstring and an assert-based self-test that prints OK.

## Python Код
```python
"""
In‑process job queue with retry and exponential back‑off.

Only the Python standard library is used.  Jobs are callables executed by a
single background thread.  If a job raises an exception it is retried
`retries` times.  The delay before each retry starts at `initial_delay`
seconds and is multiplied by `backoff_factor` after every failure.

The queue returns a ``JobFuture`` object that can be awaited (via ``result()``
or ``exception()``) and provides ``done()`` and ``wait()`` helpers.
"""

from __future__ import annotations

import threading
import time
import queue
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Tuple, Dict


@dataclass
class _Task:
    fn: Callable[..., Any]
    args: Tuple[Any, ...]
    kwargs: Dict[str, Any]
    retries_left: int
    delay: float
    backoff_factor: float
    future: "JobFuture" = field(repr=False)


class JobFuture:
    """
    Minimal Future‑like object returned by :class:`JobQueue.enqueue`.

    It stores the result or exception of the job and provides thread‑safe
    accessors.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._result: Any = None
        self._exception: Optional[BaseException] = None

    def set_result(self, value: Any) -> None:
        self._result = value
        self._event.set()

    def set_exception(self, exc: BaseException) -> None:
        self._exception = exc
        self._event.set()

    def result(self, timeout: Optional[float] = None) -> Any:
        """Return the job result or raise the exception that occurred."""
        if not self._event.wait(timeout):
            raise TimeoutError("Job did not complete in time")
        if self._exception is not None:
            raise self._exception
        return self._result

    def exception(self, timeout: Optional[float] = None) -> Optional[BaseException]:
        """Return the exception raised by the job, if any."""
        if not self._event.wait(timeout):
            raise TimeoutError("Job did not complete in time")
        return self._exception

    def done(self) -> bool:
        """Return ``True`` if the job has finished (either success or failure)."""
        return self._event.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until the job finishes; return ``True`` if it finished."""
        return self._event.wait(timeout)


class JobQueue:
    """
    Simple in‑process job queue.

    Parameters
    ----------
    maxsize:
        Maximum number of pending jobs; ``0`` (default) means infinite.
    """

    def __init__(self, maxsize: int = 0) -> None:
        self._queue: queue.Queue[_Task] = queue.Queue(maxsize)
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._stop_event = threading.Event()
        self._started = False

    def start(self) -> None:
        """Start the background worker thread (idempotent)."""
        if not self._started:
            self._thread.start()
            self._started = True

    def stop(self, timeout: Optional[float] = None) -> None:
        """
        Signal the worker to stop and wait for it.

        The worker finishes the current job but discards pending jobs.
        """
        self._stop_event.set()
        # Unblock queue.get() if it's waiting
        try:
            self._queue.put_nowait(_Task(lambda: None, (), {}, 0, 0, 0, JobFuture()))
        except queue.Full:
            pass
        self._thread.join(timeout)

    def enqueue(
        self,
        fn: Callable[..., Any],
        *args: Any,
        retries: int = 3,
        backoff_factor: float = 2.0,
        initial_delay: float = 0.1,
        **kwargs: Any,
    ) -> JobFuture:
        """
        Schedule a callable for execution.

        Returns a ``JobFuture`` that can be used to retrieve the result.
        """
        if not self._started:
            self.start()
        future = JobFuture()
        task = _Task(
            fn=fn,
            args=args,
            kwargs=kwargs,
            retries_left=retries,
            delay=initial_delay,
            backoff_factor=backoff_factor,
            future=future,
        )
        self._queue.put(task)
        return future

    # --------------------------------------------------------------------- #
    # Internal worker implementation
    # --------------------------------------------------------------------- #
    def _worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                task: _Task = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            # Sentinel task used only for stopping; ignore it.
            if task.fn is None:
                continue

            try:
                result = task.fn(*task.args, **task.kwargs)
            except Exception as exc:  # noqa: BLE001
                if task.retries_left > 0:
                    # schedule retry after sleeping for the current delay
                    time.sleep(task.delay)
                    task.retries_left -= 1
                    task.delay *= task.backoff_factor
                    self._queue.put(task)
                else:
                    task.future.set_exception(exc)
            else:
                task.future.set_result(result)
            finally:
                self._queue.task_done()


# -------------------------------------------------------------------------
# Self‑test
# -------------------------------------------------------------------------
if __name__ == "__main__":
    # 1. Job that fails twice before succeeding.
    call_counter = {"cnt": 0}

    def flaky_job(x: int) -> int:
        call_counter["cnt"] += 1
        if call_counter["cnt"] < 3:
            raise ValueError("temporary failure")
        return x * 2

    q = JobQueue()
    future = q.enqueue(flaky_job, 21, retries=5, backoff_factor=1.0, initial_delay=0.01)

    # Wait for completion and verify result.
    result = future.result(timeout=5)
    assert result == 42, "Result should be doubled input"
    assert call_counter["cnt"] == 3, "Job should have been attempted three times"

    # 2. Job that exhausts retries and propagates the exception.
    call_counter["cnt"] = 0

    def always_fail() -> None:
        call_counter["cnt"] += 1
        raise RuntimeError("always fails")

    future2 = q.enqueue(always_fail, retries=2, backoff_factor=1.0, initial_delay=0.01)
    try:
        future2.result(timeout=5)
        raise AssertionError("Exception expected but not raised")
    except RuntimeError as e:
        assert str(e) == "always fails"
        assert call_counter["cnt"] == 3, "Should have run initial + 2 retries"

    q.stop()
    print("OK")
```

## Pitfalls
- OK

