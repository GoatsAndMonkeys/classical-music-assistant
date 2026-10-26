"""Immutable internal ids: a type prefix plus a ULID-style, time-ordered random suffix (PLAN.md §4)."""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lower-case


def _b32(n: int, width: int) -> str:
    out = []
    for _ in range(width):
        out.append(_ALPHABET[n & 31])
        n >>= 5
    return "".join(reversed(out))


def new_id(prefix: str) -> str:
    """'w' -> 'w_01jb...' (48-bit millisecond time + 80 random bits)."""
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rnd = int.from_bytes(os.urandom(10), "big")
    return f"{prefix}_{_b32(ms, 10)}{_b32(rnd, 16)}"


def kind_of(entity_id: str) -> str | None:
    head, sep, _ = entity_id.partition("_")
    return head if sep else None
