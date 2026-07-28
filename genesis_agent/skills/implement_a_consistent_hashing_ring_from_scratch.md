---
name: 'implement_a_consistent_hashing_ring_from_scratch'
category: 'autonomous'
description: 'Implement a consistent-hashing ring from scratch (stdlib only) with type hints, docstring and an assert-based self-test that prints OK.'
triggers: ["implement a consistent hashing ring from scratch"]
version: '1.0'
author: 'Genesis'
last_updated: '2026-07-27T13:55:55.648775+00:00'
---

## Описание
Implement a consistent-hashing ring from scratch (stdlib only) with type hints, docstring and an assert-based self-test that prints OK.

## Python Код
```python
#!/usr/bin/env python3
"""
Consistent hashing ring implementation (stdlib only).

The ring maps arbitrary string keys to a set of nodes using virtual replicas.
It supports adding and removing nodes while keeping the hash space sorted.
"""

from __future__ import annotations

import hashlib
import bisect
from typing import Callable, Iterable, List, Optional, Tuple, Set


class ConsistentHashRing:
    """
    Consistent hashing ring.

    Parameters
    ----------
    nodes : Iterable[str]
        Initial collection of node identifiers.
    replicas : int, default 100
        Number of virtual replicas per real node.
    hash_fn : Callable[[bytes], object], default hashlib.md5
        Function that takes ``bytes`` and returns a hash object supporting
        ``digest()``. The first 8 bytes of the digest are used as a 64‑bit integer.
    """

    def __init__(
        self,
        nodes: Iterable[str],
        replicas: int = 100,
        hash_fn: Callable[[bytes], object] = hashlib.md5,
    ) -> None:
        self.replicas: int = replicas
        self.hash_fn: Callable[[bytes], object] = hash_fn
        self._ring: List[Tuple[int, str]] = []          # sorted list of (hash, node)
        self._nodes: Set[str] = set()                  # live nodes
        for node in nodes:
            self.add_node(node)

    @staticmethod
    def _hash_key(key: str, hash_fn: Callable[[bytes], object]) -> int:
        """Return a 64‑bit integer hash for *key* using *hash_fn*."""
        digest = hash_fn(key.encode()).digest()
        return int.from_bytes(digest[:8], "big", signed=False)

    def _hash(self, key: str) -> int:
        """Instance wrapper around :meth:`_hash_key` using the ring's hash function."""
        return self._hash_key(key, self.hash_fn)

    def add_node(self, node: str) -> None:
        """Add *node* and its virtual replicas to the ring."""
        if node in self._nodes:
            return
        self._nodes.add(node)
        for i in range(self.replicas):
            vnode_key = f"{node}:{i}"
            h = self._hash(vnode_key)
            bisect.insort(self._ring, (h, node))

    def remove_node(self, node: str) -> None:
        """Remove *node* and all its virtual replicas from the ring."""
        if node not in self._nodes:
            return
        self._nodes.remove(node)
        self._ring = [(h, n) for (h, n) in self._ring if n != node]

    def get_node(self, key: str) -> Optional[str]:
        """
        Return the node responsible for *key*.

        If the ring is empty, ``None`` is returned.
        """
        if not self._ring:
            return None
        h = self._hash(key)
        idx = bisect.bisect_left(self._ring, (h, ""))
        if idx == len(self._ring):
            idx = 0  # wrap‑around
        return self._ring[idx][1]

    def __contains__(self, node: str) -> bool:
        """Return ``True`` if *node* is present in the ring."""
        return node in self._nodes

    def __len__(self) -> int:
        """Number of real nodes (not virtual replicas)."""
        return len(self._nodes)

    def __iter__(self):
        """Iterate over the real nodes."""
        return iter(self._nodes)


if __name__ == "__main__":
    # Basic deterministic mapping test
    ring = ConsistentHashRing(["A", "B", "C"], replicas=10)
    keys = [f"key{i}" for i in range(20)]
    mapping1 = [ring.get_node(k) for k in keys]

    # Adding a node should keep most keys on the same node (minimal movement)
    ring.add_node("D")
    mapping2 = [ring.get_node(k) for k in keys]
    moved = sum(1 for a, b in zip(mapping1, mapping2) if a != b)
    assert moved < len(keys) // 2, "too many keys moved after adding a node"

    # Removing a node should re‑distribute its keys
    ring.remove_node("B")
    mapping3 = [ring.get_node(k) for k in keys]
    # keys that were on B must now be on some other node
    for k, before, after in zip(keys, mapping2, mapping3):
        if before == "B":
            assert after != "B", "key still maps to removed node"

    # Ring invariants
    assert len(ring) == 3, "node count mismatch after add/remove"
    assert all(isinstance(node, str) for node in ring), "node type mismatch"
    assert all(node in ring for node in ["A", "C", "D"]), "missing nodes"

    # Consistency: same key always maps to same node (deterministic)
    for _ in range(5):
        for k in keys:
            assert ring.get_node(k) == ring.get_node(k), "non‑deterministic mapping"

    print("OK")
```

## Pitfalls
- OK

