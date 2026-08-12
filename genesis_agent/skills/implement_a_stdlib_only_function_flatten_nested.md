---
name: 'implement_a_stdlib_only_function_flatten_nested'
category: 'autonomous'
description: 'Implement a stdlib-only function flatten(nested) that flattens an arbitrarily nested list of ints into a flat list, with type hints, docstring and an assert-based self-test that prints OK.'
triggers: ["implement a stdlib only function flatten nested"]
version: '1.0'
author: 'Genesis'
last_updated: '2026-08-12T15:27:21.468246+00:00'
---

## Описание
Implement a stdlib-only function flatten(nested) that flattens an arbitrarily nested list of ints into a flat list, with type hints, docstring and an assert-based self-test that prints OK.

## Python Код
```python
from __future__ import annotations
from typing import List, Union, Iterable

NestedIntList = Union[int, List["NestedIntList"]]


def flatten(nested: List[NestedIntList]) -> List[int]:
    """
    Flatten an arbitrarily nested list of integers into a flat list.

    Args:
        nested: A list that may contain integers or further nested lists of integers.

    Returns:
        A new list containing all the integers from ``nested`` in left‑to‑right order.

    Example:
        >>> flatten([1, [2, [3, 4], 5], 6])
        [1, 2, 3, 4, 5, 6]
    """
    flat: List[int] = []

    def _extend(item: NestedIntList) -> None:
        if isinstance(item, int):
            flat.append(item)
        else:
            for sub in item:  # type: ignore[arg-type]  # item is a List[NestedIntList]
                _extend(sub)

    for element in nested:
        _extend(element)

    return flat


if __name__ == "__main__":
    # Self‑tests
    assert flatten([]) == []
    assert flatten([1, 2, 3]) == [1, 2, 3]
    assert flatten([1, [2, 3], 4]) == [1, 2, 3, 4]
    assert flatten([1, [2, [3, [4]], 5], 6]) == [1, 2, 3, 4, 5, 6]
    assert flatten([[[]]]) == []
    # Mixed deeper nesting
    nested = [1, [2, [3, [4, [5]]]], 6, [], [7, [8, []]], 9]
    assert flatten(nested) == [1, 2, 3, 4, 5, 6, 7, 8, 9]

    print("OK")
```

## Pitfalls
- OK

