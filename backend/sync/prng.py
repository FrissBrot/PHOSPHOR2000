"""Deterministic hashing / PRNG shared bit-for-bit with frontend/js/engine/prng.js.

Both sides use 32-bit FNV-1a for string hashing, a murmur3-style finaliser to
mix integers, and mulberry32 as the stream generator. Tests compare golden
vectors to guarantee Python and JavaScript agree.
"""
from __future__ import annotations

MASK = 0xFFFFFFFF


def fnv1a(text: str) -> int:
    h = 0x811C9DC5
    for b in text.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & MASK
    return h


def mix32(*values: int) -> int:
    """Order-dependent combination of 32-bit integers (murmur3 fmix based)."""
    h = 0x9E3779B9
    for v in values:
        k = v & MASK
        h ^= k
        h = (h * 0x85EBCA6B) & MASK
        h ^= h >> 13
        h = (h * 0xC2B2AE35) & MASK
        h ^= h >> 16
    return h


def seed_for(global_seed: int, scene: str, start_sixteenth: int) -> int:
    return mix32(global_seed, fnv1a(scene), start_sixteenth)


class Mulberry32:
    def __init__(self, seed: int) -> None:
        self.state = seed & MASK

    def next_u32(self) -> int:
        self.state = (self.state + 0x6D2B79F5) & MASK
        t = self.state
        t = ((t ^ (t >> 15)) * (t | 1)) & MASK
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & MASK)) & MASK
        return (t ^ (t >> 14)) & MASK

    def random(self) -> float:
        return self.next_u32() / 4294967296.0
