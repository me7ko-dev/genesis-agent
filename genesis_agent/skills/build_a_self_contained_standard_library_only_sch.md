---
name: 'build_a_self_contained_standard_library_only_sch'
category: 'autonomous'
description: 'Build a self-contained (standard-library-only) schema migration diff generator for dict records module with a docstring, type hints and an assert-based self-test that prints OK.'
triggers: ["build a self contained standard library only sch"]
version: '1.0'
author: 'Genesis'
last_updated: '2026-07-27T13:53:30.074296+00:00'
---

## Описание
Build a self-contained (standard-library-only) schema migration diff generator for dict records module with a docstring, type hints and an assert-based self-test that prints OK.

## Python Код
```python
from typing import Dict, List, Tuple, Any, Type


def generate_migration_diff(
    old: Dict[str, Type],
    new: Dict[str, Type],
) -> List[Tuple[str, str, Any]]:
    """
    Compute a migration diff between two schema dictionaries.

    Each schema maps a field name to a Python ``type`` object.
    The diff is a list of tuples ``(action, field, info)`` where:

    * ``action`` is one of ``'add'``, ``'remove'`` or ``'type_change'``.
    * ``field`` is the name of the affected field.
    * ``info`` is:
        - for ``'add'`` – the new type,
        - for ``'remove'`` – the old type,
        - for ``'type_change'`` – the new type (the old type can be obtained
          from the ``old`` schema if needed).

    The order of the resulting list is not significant.

    Args:
        old: Original schema.
        new: Target schema.

    Returns:
        A list describing the differences required to migrate from ``old`` to ``new``.

    Raises:
        TypeError: If either ``old`` or ``new`` is not a ``dict``.
    """
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise TypeError("Both 'old' and 'new' must be dictionaries")

    diff: List[Tuple[str, str, Any]] = []

    old_keys = set(old.keys())
    new_keys = set(new.keys())

    # Added fields
    for field in new_keys - old_keys:
        diff.append(("add", field, new[field]))

    # Removed fields
    for field in old_keys - new_keys:
        diff.append(("remove", field, old[field]))

    # Type changes
    for field in old_keys & new_keys:
        if old[field] is not new[field]:
            diff.append(("type_change", field, new[field]))

    return diff


if __name__ == "__main__":
    # Example schemas
    old_schema = {
        "id": int,
        "name": str,
        "age": int,
    }

    new_schema = {
        "id": int,
        "full_name": str,
        "age": str,      # type changed
        "email": str,    # added
    }

    expected = {
        ("add", "full_name", str),
        ("add", "email", str),
        ("remove", "name", str),
        ("type_change", "age", str),
    }

    result = generate_migration_diff(old_schema, new_schema)
    assert set(result) == expected, f"Diff mismatch: {result!r}"
    print("OK")
```

## Pitfalls
- OK

