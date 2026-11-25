"""Distributed musical timeline.

All times in this module are *cluster time* in seconds (float). Cluster time is
a virtual clock: on the leader it is ``local_monotonic + frozen_offset``; on
followers it is ``local_monotonic + estimated_offset`` (see clocksync.py).
Raw monotonic timestamps of different machines are never compared.

The beat position is a piecewise-linear function::

    beat(t) = anchor_beat + (t - anchor_time) * bpm / 60 + slew(t)
    slew(t) = -slew_delta * (1 - ramp(t)),   ramp = clamp((t - s0) / (s1 - s0), 0, 1)

``slew`` lets a node converge smoothly (or step at a bar boundary when
s0 == s1) from its locally predicted phase to an authoritative one without
discontinuities in the middle of a bar. The identical formula is implemented in
frontend/js/engine/timeline.js; tests check both against golden vectors.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from enum import Enum


class TimingSource(str, Enum):
    LIVE = "LIVE"            # beat phase tracked from real audio right now
    PREDICTED = "PREDICTED"  # free-running prediction (audio lost / no audio node)
    MANUAL = "MANUAL"        # operator-set BPM / tap tempo / phase nudge


@dataclass(frozen=True)
class Timeline:
    bpm: float = 120.0
    anchor_time: float = 0.0
    anchor_beat: float = 0.0
    beats_per_bar: int = 4
    confidence: float = 0.0
    source: TimingSource = TimingSource.PREDICTED
    revision: int = 0
    epoch: int = 0
    seed: int = 0x2000
    slew_delta: float = 0.0
    slew_start: float = 0.0
    slew_end: float = 0.0
    live_at: float | None = None  # cluster time of the last LIVE audio update

    # ---- core evaluation -------------------------------------------------
    def slew(self, t: float) -> float:
        if self.slew_delta == 0.0:
            return 0.0
        if self.slew_end <= self.slew_start:
            ramp = 1.0 if t >= self.slew_end else 0.0
        else:
            ramp = min(1.0, max(0.0, (t - self.slew_start) / (self.slew_end - self.slew_start)))
        return -self.slew_delta * (1.0 - ramp)

    def raw_beat(self, t: float) -> float:
        return self.anchor_beat + (t - self.anchor_time) * self.bpm / 60.0

    def beat_at(self, t: float) -> float:
        return self.raw_beat(t) + self.slew(t)

    def time_of_beat(self, beat: float) -> float:
        """Inverse of beat_at (monotonic as long as slews are gentle)."""
        spb = 60.0 / self.bpm
        t = self.anchor_time + (beat - self.anchor_beat) * spb
        for _ in range(8):
            err = self.beat_at(t) - beat
            if abs(err) < 1e-9:
                break
            t -= err * spb
        return t

    def period(self) -> float:
        return 60.0 / self.bpm

    def phase(self, t: float) -> float:
        b = self.beat_at(t)
        return b - math.floor(b)

    def bar_at(self, t: float) -> float:
        return self.beat_at(t) / self.beats_per_bar

    # ---- derived timelines -----------------------------------------------
    def rebased(self, t: float) -> "Timeline":
        """Same function, anchor moved to t, slew folded in where finished."""
        if self.slew_delta and t >= max(self.slew_start, self.slew_end):
            return replace(self, anchor_beat=self.raw_beat(t), anchor_time=t, slew_delta=0.0, slew_start=0.0, slew_end=0.0)
        return replace(self, anchor_beat=self.raw_beat(t), anchor_time=t)

    def with_tempo(self, t: float, bpm: float, **kw) -> "Timeline":
        """Change tempo at t while preserving the beat position (phase continuity)."""
        base = self.rebased(t)
        return replace(base, bpm=float(bpm), **kw)

    def shifted(self, t: float, delta_beats: float, **kw) -> "Timeline":
        """Phase nudge: beat position jumps by delta at t (used for manual offset)."""
        base = self.rebased(t)
        return replace(base, anchor_beat=base.anchor_beat + delta_beats, **kw)

    def next_grid_time(self, t: float, division: float = 1.0, lead: float = 0.0) -> tuple[float, float]:
        """Next beat-grid point (beat, time) at least `lead` seconds after t.

        division: 4 = bar (if 4/4), 1 = beat, 0.5 = eighth, 0.25 = sixteenth.
        """
        b = self.beat_at(t + lead)
        nb = math.ceil(b / division - 1e-9) * division
        return nb, self.time_of_beat(nb)

    def upcoming(self, t: float, count: int, division: float = 1.0) -> list[tuple[float, float]]:
        b0 = math.ceil(self.beat_at(t) / division - 1e-9) * division
        return [(b0 + i * division, self.time_of_beat(b0 + i * division)) for i in range(count)]

    # ---- serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["source"] = self.source.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Timeline":
        d = dict(d)
        d["source"] = TimingSource(d.get("source", "PREDICTED"))
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def wrap_half(x: float) -> float:
    """Wrap to [-0.5, 0.5)."""
    return x - math.floor(x + 0.5)


@dataclass
class Reconciliation:
    timeline: Timeline
    delta_beats: float
    mode: str  # "none" | "slew" | "deferred"


def reconcile(local: Timeline, target: Timeline, t: float, slew_threshold_beats: float = 0.06,
              slew_duration_beats: float = 4.0) -> Reconciliation:
    """Adopt `target` without visual discontinuity mid-bar.

    * Tempo of `target` is adopted immediately (phase-continuously).
    * Phase/beat-index error ``delta`` (target - local) below the threshold is
      slewed over ``slew_duration_beats``.
    * Larger errors (including whole-beat/bar index differences after a long
      outage or a split brain) are applied as a step at the next local bar
      boundary, so the jump coincides with a musical boundary/scene change.
    """
    lb = local.beat_at(t)
    tb = target.beat_at(t)
    delta = tb - lb
    if abs(delta) < 1e-6:
        return Reconciliation(replace(target.rebased(t), slew_delta=0.0, slew_start=0.0, slew_end=0.0), 0.0, "none")
    base = target.rebased(t)
    base = replace(base, slew_delta=0.0, slew_start=0.0, slew_end=0.0)
    if abs(delta) <= slew_threshold_beats:
        dur = slew_duration_beats * base.period()
        return Reconciliation(replace(base, slew_delta=delta, slew_start=t, slew_end=t + dur), delta, "slew")
    # Deferred step: hold the local phase (at the new tempo) until the next
    # *target* bar downbeat. At that instant every in-sync CRT starts a bar, so
    # the snapping display lands exactly on the cluster's downbeat.
    bpb = target.beats_per_bar
    next_bar_beat = math.floor(tb / bpb + 1e-9) * bpb + bpb
    t_step = base.time_of_beat(next_bar_beat)
    held = replace(base, slew_delta=delta, slew_start=t_step, slew_end=t_step)
    return Reconciliation(held, delta, "deferred")


@dataclass
class TapTempo:
    """Operator tap tempo; intervals only, so client clock offsets cancel."""

    max_taps: int = 8
    reset_after: float = 2.5
    taps: list[float] = field(default_factory=list)

    def tap(self, t: float) -> float | None:
        if self.taps and t - self.taps[-1] > self.reset_after:
            self.taps.clear()
        if self.taps and t - self.taps[-1] < 0.15:
            return self.bpm()  # bounce / double press
        self.taps.append(t)
        self.taps = self.taps[-self.max_taps:]
        return self.bpm()

    def bpm(self) -> float | None:
        if len(self.taps) < 3:
            return None
        iv = sorted(b - a for a, b in zip(self.taps, self.taps[1:]))
        med = iv[len(iv) // 2]
        good = [x for x in iv if abs(x - med) < 0.2 * med]
        return 60.0 / (sum(good) / len(good))
