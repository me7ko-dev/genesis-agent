---
name: 'implement_a_stdlib_only_function_chunked_seq_n_t'
category: 'autonomous'
description: 'Implement a stdlib-only function chunked(seq, n) that splits a sequence into lists of length n (last one may be shorter), with type hints, docstring and an assert-based self-test that prints OK.'
triggers: ["implement a stdlib only function chunked seq n t"]
version: '1.0'
author: 'Genesis'
last_updated: '2026-08-12T15:25:57.441903+00:00'
---

## Описание
Implement a stdlib-only function chunked(seq, n) that splits a sequence into lists of length n (last one may be shorter), with type hints, docstring and an assert-based self-test that prints OK.

## Python Код
```python
from typing import Sequence, TypeVar, List

T = TypeVar('T')


def chunked(seq: Sequence[T], n: int) -> List[List[T]]:
    """Split a sequence into chunks of length n.

    Args:
        seq: The sequence to split (list, tuple, string, etc.).
        n: The size of each chunk. Must be a positive integer.

    Returns:
        A list of lists, where each inner list has length n (except
        possibly the last one, which may be shorter).

    Raises:
        ValueError: If n is not a positive integer.

    Examples:
        >>> chunked([1, 2, 3, 4, 5], 2)
        [[1, 2], [3, 4], [5]]
        >>> chunked('abcdefg', 3)
        [['a', 'b', 'c'], ['d', 'e', 'f'], ['g']]
        >>> chunked((1, 2, 3), 5)
        [[1, 2, 3]]
        >>> chunked([], 3)
        []
    """
    if not isinstance(n, int) or n <= 0:
        raise ValueError("n must be a positive integer")

    result: List[List[T]] = []
    for i in range(0, len(seq), n):
        result.append(list(seq[i:i + n]))
    return result


if __name__ == "__main__":
    # Basic functionality tests
    assert chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert chunked([1, 2, 3, 4, 5, 6], 3) == [[1, 2, 3], [4, 5, 6]]
    assert chunked([1, 2, 3], 5) == [[1, 2, 3]]
    assert chunked([], 3) == []
    assert chunked("abcdefg", 3) == [['a', 'b', 'c'], ['d', 'e', 'f'], ['g']]
    assert chunked((1, 2, 3, 4), 2) == [[1, 2], [3, 4]]

    # Edge cases
    assert chunked([1], 1) == [[1]]
    assert chunked([1, 2], 1) == [[1], [2]]
    assert chunked([1, 2, 3], 2) == [[1, 2], [3]]

    # Error handling
    try:
        chunked([1, 2, 3], 0)
        assert False, "Expected ValueError for n=0"
    except ValueError:
        pass

    try:
        chunked([1, 2, 3], -1)
        assert False, "Expected ValueError for n=-1"
    except ValueError:
        pass

    try:
        chunked([1, 2, 3], 2.5)
        assert False, "Expected ValueError for non-int n"
    except ValueError:
        pass

    # Type hint verification (runtime check via isinstance)
    result = chunked([1, 2, 3], 2)
    assert isinstance(result, list)
    assert all(isinstance(chunk, list) for chunk in result)
    assert all(isinstance(item, int) for chunk in result for item in chunk)

    print("OK")
```

## Pitfalls
- OK

