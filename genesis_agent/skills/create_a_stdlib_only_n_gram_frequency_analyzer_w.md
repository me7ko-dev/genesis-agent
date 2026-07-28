---
name: 'create_a_stdlib_only_n_gram_frequency_analyzer_w'
category: 'autonomous'
description: 'Create a stdlib-only n-gram frequency analyzer with type hints, docstring, and an assert self-test printing OK.'
triggers: ["create a stdlib only n gram frequency analyzer w"]
version: '1.0'
author: 'Genesis'
last_updated: '2026-07-27T13:53:54.138140+00:00'
---

## Описание
Create a stdlib-only n-gram frequency analyzer with type hints, docstring, and an assert self-test printing OK.

## Python Код
```python
"""N‑gram frequency analyzer.

Provides:
- ngram_frequencies: count n‑gram occurrences in a text.
- most_common_ngrams: retrieve the top‑k most frequent n‑grams.
"""

from collections import Counter
from typing import Dict, List, Tuple


def ngram_frequencies(text: str, n: int) -> Dict[Tuple[str, ...], int]:
    """Return a mapping from n‑grams to their frequencies.

    The text is split on whitespace; punctuation is retained as‑is.
    An empty or too‑short text yields an empty dictionary.

    Args:
        text: Input string.
        n: Size of the n‑gram (must be >= 1).

    Returns:
        Dictionary mapping each n‑gram (as a tuple of words) to its count.
    """
    if n <= 0:
        raise ValueError("n must be a positive integer")
    words = text.split()
    if len(words) < n:
        return {}
    grams = (tuple(words[i:i + n]) for i in range(len(words) - n + 1))
    return dict(Counter(grams))


def most_common_ngrams(
    freq: Dict[Tuple[str, ...], int], k: int
) -> List[Tuple[Tuple[str, ...], int]]:
    """Return the top‑k most frequent n‑grams.

    Args:
        freq: Mapping of n‑grams to frequencies (as produced by
              :func:`ngram_frequencies`).
        k: Number of top items to return (k <= 0 returns empty list).

    Returns:
        List of (n‑gram, frequency) tuples sorted by descending frequency.
    """
    if k <= 0:
        return []
    return sorted(freq.items(), key=lambda item: item[1], reverse=True)[:k]


if __name__ == "__main__":
    # Example test from the specification
    sample = (
        "the quick brown fox jumps over the lazy dog the quick brown fox"
    )
    freq_bigram = ngram_frequencies(sample, 2)
    top1 = most_common_ngrams(freq_bigram, 1)[0]
    assert top1 == (("the", "quick"), 2)

    # Additional sanity checks
    # Empty input
    assert ngram_frequencies("", 2) == {}
    # n larger than number of words
    assert ngram_frequencies("hello world", 3) == {}
    # most_common_ngrams with k=0
    assert most_common_ngrams(freq_bigram, 0) == []
    # k larger than available items
    all_ngrams = most_common_ngrams(freq_bigram, 100)
    assert len(all_ngrams) == len(freq_bigram)

    print("OK")
```

## Pitfalls
- OK

