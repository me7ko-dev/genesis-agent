---
name: 'create_a_stdlib_only_word_level_diff_highlighter'
category: 'autonomous'
description: 'Create a stdlib-only word-level diff highlighter between two strings with type hints, docstring, and an assert self-test printing OK.'
triggers: ["create a stdlib only word level diff highlighter"]
version: '1.0'
author: 'Genesis'
last_updated: '2026-07-27T13:53:39.563907+00:00'
---

## Описание
Create a stdlib-only word-level diff highlighter between two strings with type hints, docstring, and an assert self-test printing OK.

## Python Код
```python
import re
import difflib
from typing import List


def highlight_diff(old: str, new: str) -> str:
    """
    Word‑level diff highlighter between two strings.

    The function tokenises both strings into words and non‑word characters
    (preserving whitespace and punctuation) and then uses
    :class:`difflib.SequenceMatcher` to compute a diff at the token level.

    Added words are wrapped in ``\x1b[32m`` (green) and ``\x1b[0m``.
    Removed words are wrapped in ``\x1b[31m`` (red)   and ``\x1b[0m``.
    Unchanged parts are left untouched.

    Args:
        old: Original text.
        new: Modified text.

    Returns:
        A single string with ANSI colour codes highlighting the differences.
    """
    # Tokenise while keeping delimiters (spaces, punctuation, etc.)
    old_tokens: List[str] = re.findall(r'\w+|\W+', old)
    new_tokens: List[str] = re.findall(r'\w+|\W+', new)

    matcher = difflib.SequenceMatcher(a=old_tokens, b=new_tokens)

    parts: List[str] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            parts.append(''.join(old_tokens[i1:i2]))
        elif tag == "delete":
            parts.append('\x1b[31m')
            parts.append(''.join(old_tokens[i1:i2]))
            parts.append('\x1b[0m')
        elif tag == "insert":
            parts.append('\x1b[32m')
            parts.append(''.join(new_tokens[j1:j2]))
            parts.append('\x1b[0m')
        elif tag == "replace":
            # Show removal then addition
            parts.append('\x1b[31m')
            parts.append(''.join(old_tokens[i1:i2]))
            parts.append('\x1b[0m')
            parts.append('\x1b[32m')
            parts.append(''.join(new_tokens[j1:j2]))
            parts.append('\x1b[0m')

    return ''.join(parts)


if __name__ == "__main__":
    # Self‑test
    old_text = "The quick brown fox"
    new_text = "The quick red fox jumps"

    diff = highlight_diff(old_text, new_text)

    # Expected ANSI‑coded fragments
    assert "\x1b[31mbrown\x1b[0m" in diff, "Removed word 'brown' not highlighted in red"
    assert "\x1b[32mred\x1b[0m" in diff, "Added word 'red' not highlighted in green"
    assert "\x1b[32m jumps\x1b[0m" in diff, "Added word ' jumps' not highlighted in green"

    print("OK")
```

## Pitfalls
- OK

