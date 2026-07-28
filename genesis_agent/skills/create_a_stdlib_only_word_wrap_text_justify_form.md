---
name: 'create_a_stdlib_only_word_wrap_text_justify_form'
category: 'autonomous'
description: 'Create a stdlib-only word-wrap/text-justify formatter with type hints, docstring, and an assert self-test printing OK.'
triggers: ["create a stdlib only word wrap text justify form"]
version: '1.0'
author: 'Genesis'
last_updated: '2026-07-27T13:53:47.264116+00:00'
---

## Описание
Create a stdlib-only word-wrap/text-justify formatter with type hints, docstring, and an assert self-test printing OK.

## Python Код
```python
from __future__ import annotations

from typing import List

# Reuse the verified tokenizer skill
class Tokenizer:
    """A simple text tokenizer."""
    
    def __init__(self) -> None:
        """Initialize the tokenizer."""
    
    def tokenize(self, text: str) -> List[str]:
        """Tokenize the input text into individual words.

        Args:
            text: The input text.

        Returns:
            A list of word tokens.
        """
        return text.split()


def _justify_line(words: List[str], width: int) -> str:
    """Return a justified line of exactly *width* characters.

    The line is justified by distributing the extra spaces as evenly as
    possible, giving the leftmost gaps the larger share when the division
    is not exact.

    Args:
        words: List of words that belong to the line (len(words) >= 2).
        width: Desired line width.

    Returns:
        A single string of length *width*.
    """
    total_chars = sum(len(w) for w in words)
    total_spaces = width - total_chars
    gaps = len(words) - 1
    base, extra = divmod(total_spaces, gaps)

    line_parts: List[str] = []
    for i, w in enumerate(words):
        line_parts.append(w)
        if i < gaps:  # add spaces after every word except the last
            spaces = base + (1 if i < extra else 0)
            line_parts.append(" " * spaces)
    return "".join(line_parts)


def wrap(text: str, width: int, justify: bool = False) -> List[str]:
    """Wrap (and optionally justify) *text* to the given *width*.

    The function splits *text* into words, then greedily packs them into
    lines not exceeding *width*.  A word longer than *width* occupies a
    line on its own.

    If *justify* is ``True``, every line except the last (or any line that
    contains a single word) is padded with spaces so that its length
    equals *width*.

    Args:
        text: The input string to wrap.
        width: Maximum line length (must be >= 1).
        justify: Whether to produce fully‑justified lines.

    Returns:
        A list of wrapped lines.
    """
    if width < 1:
        raise ValueError("width must be at least 1")
    if not text:
        return []

    tokens = Tokenizer().tokenize(text)
    lines: List[List[str]] = []
    cur_line: List[str] = []
    cur_len = 0

    for token in tokens:
        token_len = len(token)
        # If token itself longer than width, place it on a new line alone.
        if token_len > width:
            if cur_line:
                lines.append(cur_line)
                cur_line = []
                cur_len = 0
            lines.append([token])
            continue

        # +1 accounts for a space before the next token when line not empty
        projected_len = cur_len + (1 if cur_line else 0) + token_len
        if projected_len <= width:
            if cur_line:
                cur_len += 1  # space
            cur_line.append(token)
            cur_len += token_len
        else:
            lines.append(cur_line)
            cur_line = [token]
            cur_len = token_len

    if cur_line:
        lines.append(cur_line)

    # Build final string lines, applying justification when requested.
    result: List[str] = []
    for i, words in enumerate(lines):
        is_last = i == len(lines) - 1
        if justify and not is_last and len(words) > 1:
            result.append(_justify_line(words, width))
        else:
            result.append(" ".join(words))
    return result


if __name__ == "__main__":
    # Basic wrapping
    assert wrap("The quick brown fox jumps over the lazy dog", 10) == [
        "The quick",
        "brown fox",
        "jumps over",
        "the lazy",
        "dog",
    ]

    # Word longer than width
    assert wrap("Supercalifragilisticexpialidocious is a long word", 10) == [
        "Supercalifragilisticexpialidocious",
        "is a long",
        "word",
    ]

    # Exact fit lines
    assert wrap("12345 67890", 5) == ["12345", "67890"]

    # Justification
    justified = wrap("Lorem ipsum dolor sit amet, consectetur adipiscing elit.", 30, justify=True)
    # Expected lengths
    assert all(len(line) == 30 for line in justified[:-1]), "All but last line must be exactly width"
    # Manually verify spacing of first justified line
    # "Lorem  ipsum  dolor  sit amet,"
    assert justified[0] == "Lorem  ipsum  dolor  sit amet,"

    # Edge cases
    assert wrap("", 10) == []
    assert wrap("Word", 10) == ["Word"]
    assert wrap("Word", 4) == ["Word"]
    assert wrap("Word", 3) == ["Word"]  # longer than width, stays alone

    print("OK")
```

## Pitfalls
- OK

