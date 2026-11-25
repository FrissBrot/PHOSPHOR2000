"""Synthetic DJ-like test signals for the beat tracker."""
from __future__ import annotations

import numpy as np


def kick(sr: int, dur: float = 0.18) -> np.ndarray:
    t = np.arange(int(sr * dur)) / sr
    f = 50 + 90 * np.exp(-t * 30)  # pitch drop
    ph = 2 * np.pi * np.cumsum(f) / sr
    return (np.sin(ph) * np.exp(-t * 18)).astype(np.float32)


def hat(sr: int, rng, dur: float = 0.04) -> np.ndarray:
    n = int(sr * dur)
    x = rng.standard_normal(n)
    x = np.diff(x, prepend=0)  # crude high-pass
    return (0.15 * x * np.exp(-np.arange(n) / sr * 80)).astype(np.float32)


def track(bpm_fn, seconds: float, sr: int = 48000, phase0: float = 0.1, seed: int = 0, noise: float = 0.01,
          kick_pattern=(1, 1, 1, 1), hats: bool = True, silence: tuple[float, float] | None = None):
    """Returns (signal, beat_times). bpm_fn(t) gives tempo; kicks on pattern beats, hats on off-beats."""
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    x = noise * rng.standard_normal(n).astype(np.float32)
    k = kick(sr)
    beats = []
    t, i = phase0, 0
    while t < seconds:
        beats.append(t)
        if kick_pattern[i % len(kick_pattern)]:
            s = int(t * sr)
            e = min(n, s + len(k))
            x[s:e] += 0.6 * k[: e - s]
        if hats:
            th = t + 30.0 / bpm_fn(t)
            s = int(th * sr)
            h = hat(sr, rng)
            e = min(n, s + len(h))
            if s < n:
                x[s:e] += h[: e - s]
        t += 60.0 / bpm_fn(t)
        i += 1
    if silence:
        a, b = int(silence[0] * sr), int(silence[1] * sr)
        x[a:b] = 0.0005 * rng.standard_normal(b - a)
        beats = [bt for bt in beats if not silence[0] <= bt < silence[1]]
    return x, np.array(beats)
