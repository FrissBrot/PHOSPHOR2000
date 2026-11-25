"""Enter-key visual calibration: circular statistics of taps against a beat grid.

The operator watches one CRT flashing at a fixed calibration tempo and taps
Enter *in anticipation* of each flash. Tap times (converted to cluster time)
are mapped to the flash grid as phases; the circular mean gives the average
lead/lag of the perceived flash relative to the scheduled beat.

IMPORTANT: this measures *perceived* alignment including the human's own
anticipation bias (typically tens of ms, person-dependent). It is an
alignment aid, not a latency measurement. Physical latency must be measured
with a camera or photodiode (docs/hardware.md).

Sign convention (consistent with the renderer): visual offset ``o`` in ms;
negative schedules visuals earlier. If taps lag the scheduled beat by ``d``
ms (the flash appears late), the suggested new offset is ``o - d``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class TapResult:
    n_taps: int
    n_used: int
    n_rejected: int
    mean_ms: float | None          # + = taps (perceived flash) after scheduled beat
    circ_std_ms: float | None
    std_err_ms: float | None
    resultant: float | None        # R in [0, 1]
    confidence: float              # 0..1 heuristic
    suggested_offset_ms: float | None
    ready: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class TapFilter:
    """Rejects key repeats/releases and accidental double presses."""

    period: float
    double_press_fraction: float = 0.35
    last: float | None = None
    accepted: list[float] = field(default_factory=list)
    ignored: int = 0

    def offer(self, t: float, value: int = 1) -> bool:
        """value: Linux evdev semantics 1=press, 2=autorepeat, 0=release."""
        if value != 1:
            self.ignored += 1
            return False
        if self.last is not None and t - self.last < self.double_press_fraction * self.period:
            self.ignored += 1
            return False
        self.last = t
        self.accepted.append(t)
        return True


def _circ_mean(angles: list[float]) -> tuple[float, float]:
    c = sum(math.cos(a) for a in angles) / len(angles)
    s = sum(math.sin(a) for a in angles) / len(angles)
    return math.atan2(s, c), math.hypot(c, s)


def analyse_taps(taps: list[float], origin: float, bpm: float, current_offset_ms: float = 0.0,
                 min_taps: int = 16, outlier_ms: float = 150.0) -> TapResult:
    """Phase statistics of tap times relative to flash grid ``origin + k*60/bpm``.

    ``origin`` must be the cluster time of a scheduled (uncorrected) flash; the
    renderer shows the flash at scheduled time + current offset + latency.
    """
    period = 60.0 / bpm
    n = len(taps)
    notes: list[str] = []
    if n < 3:
        return TapResult(n, 0, 0, None, None, None, None, 0.0, None, False, ["need more taps"])
    angles = [2 * math.pi * (((t - origin) / period) % 1.0) for t in taps]
    mu, R = _circ_mean(angles)
    used = angles
    rejected = 0
    for _ in range(3):  # iterative outlier rejection around the circular mean
        def dev(a: float) -> float:
            d = (a - mu + math.pi) % (2 * math.pi) - math.pi
            return abs(d) / (2 * math.pi) * period * 1000
        circ_std = math.sqrt(max(0.0, -2 * math.log(max(R, 1e-12)))) / (2 * math.pi) * period * 1000
        lim = min(outlier_ms, max(3 * circ_std, 25.0))
        kept = [a for a in angles if dev(a) <= lim]
        if len(kept) < 3:
            break
        rejected = n - len(kept)
        used = kept
        mu, R = _circ_mean(used)
    mean_ms = mu / (2 * math.pi) * period * 1000  # in (-period/2, period/2]
    circ_std = math.sqrt(max(0.0, -2 * math.log(max(R, 1e-12)))) / (2 * math.pi) * period * 1000
    se = circ_std / math.sqrt(len(used))
    ready = len(used) >= min_taps
    if not ready:
        notes.append(f"{min_taps - len(used)} more valid taps needed")
    if circ_std > 60:
        notes.append("taps very irregular: tap in rhythm, anticipating the flash")
    if rejected > 0.25 * n:
        notes.append("many outliers rejected")
    # confidence: high R, many taps, low standard error
    conf = max(0.0, min(1.0, R)) * min(1.0, len(used) / max(min_taps, 1)) * (1.0 / (1.0 + se / 10.0))
    suggested = current_offset_ms - mean_ms
    notes.append("human tapping includes anticipation bias; verify with camera/photodiode")
    return TapResult(n, len(used), rejected, mean_ms, circ_std, se, R, conf, suggested, ready, notes)
