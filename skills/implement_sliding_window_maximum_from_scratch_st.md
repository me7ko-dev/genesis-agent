---
name: 'implement_sliding_window_maximum_from_scratch_st'
category: 'autonomous'
description: 'Implement sliding-window maximum from scratch (stdlib only) with type hints, docstring and an assert-based self-test that prints OK.'
triggers: ["implement sliding window maximum from scratch st"]
version: '1.0'
author: 'Genesis'
last_updated: '2026-07-27T13:54:05.528659+00:00'
---

## Описание
Implement sliding-window maximum from scratch (stdlib only) with type hints, docstring and an assert-based self-test that prints OK.

## Python Код
```python
from collections import deque
from typing import List


def sliding_window_max(nums: List[int], k: int) -> List[int]:
    """
    Return a list of the maximum values of each sliding window of size ``k`` over ``nums``.

    The algorithm keeps a deque of indices whose corresponding values are in
    decreasing order. The leftmost index always points to the current window's
    maximum. Indices that fall out of the window or correspond to smaller values
    are removed as the window slides.

    Parameters
    ----------
    nums : List[int]
        Input list of integers.
    k : int
        Size of the sliding window.

    Returns
    -------
    List[int]
        Maximum of each window; empty list if ``k`` is invalid or ``nums`` is empty.
    """
    n = len(nums)
    if k <= 0 or k > n:
        return []

    dq: deque[int] = deque()          # stores indices
    result: List[int] = []

    for i, value in enumerate(nums):
        # Remove indices that are out of the current window
        while dq and dq[0] <= i - k:
            dq.popleft()

        # Remove indices whose values are smaller than current value
        while dq and nums[dq[-1]] < value:
            dq.pop()

        dq.append(i)

        # Record max when the first window is complete
        if i >= k - 1:
            result.append(nums[dq[0]])

    return result


if __name__ == "__main__":
    # basic example
    assert sliding_window_max([1, 3, -1, -3, 5, 3, 6, 7], 3) == [3, 3, 5, 5, 6, 7]
    # k = 1 (each element is its own max)
    assert sliding_window_max([4, 2, 12, 5], 1) == [4, 2, 12, 5]
    # k equals length of list
    assert sliding_window_max([2, 1, 3], 3) == [3]
    # empty input
    assert sliding_window_max([], 3) == []
    # invalid k (zero or larger than list)
    assert sliding_window_max([1, 2, 3], 0) == []
    assert sliding_window_max([1, 2, 3], 5) == []

    print("OK")
```

## Pitfalls
- OK

