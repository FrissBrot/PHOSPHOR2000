"""Cluster clock estimation (NTP-style four-timestamp exchange).

Follower sends ``t1`` (its local monotonic); leader answers with ``t2``/``t3``
(leader *cluster* time at receive/send); follower notes ``t4`` (local
monotonic). Then::

    offset = ((t2 - t1) + (t3 - t4)) / 2      # cluster - local
    delay  = (t4 - t1) - (t3 - t2)            # round trip minus leader processing

Only *differences* inside one sample mix clock domains, and the symmetric
path assumption bounds the error by delay/2. Wi-Fi delay is asymmetric and
bursty (power save, retries, background scans), so we keep a window, reject
samples whose delay is well above the window minimum, estimate the skew
(frequency error) by least squares, and apply corrections by slewing with a
bounded rate. Large errors are stepped; the renderer defers visible jumps to
a bar boundary (see timeline.reconcile). During outages the offset keeps
evolving with the last skew estimate (drift compensation), and uncertainty
grows with the time since the last good sample.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field


@dataclass
class Sample:
    local: float    # t4 (local monotonic at receipt)
    offset: float
    delay: float


@dataclass
class SyncStats:
    offset: float
    skew_ppm: float
    rtt_ms: float
    jitter_ms: float
    uncertainty_ms: float
    samples: int
    last_sample_age: float | None
    steps: int
    synced: bool

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class ClockSync:
    window: int = 32
    max_slew_ppm: float = 2000.0
    step_threshold: float = 0.050
    outlier_delay_factor: float = 2.0
    drift_ppm_bound: float = 100.0          # crystal tolerance assumption for uncertainty growth
    samples: list[Sample] = field(default_factory=list)
    # applied offset model: value(t) = base + skew*(t - t0) + extra*min(max(t - t0, 0), dur)
    base: float = 0.0
    t0: float = 0.0
    skew: float = 0.0
    extra_rate: float = 0.0
    extra_dur: float = 0.0
    steps: int = 0
    step_epoch: int = 0                      # increments on every step (renderer: defer to bar)
    synced: bool = False
    frozen: bool = False                     # leader: cluster clock = local + frozen offset
    _pending: dict[int, float] = field(default_factory=dict)
    _seq: int = 0
    last_good: float | None = None
    history: list[tuple[float, float]] = field(default_factory=list)  # (local, offset estimate)
    history_step: float = 5.0
    history_len: int = 360                   # 30 min of skew baseline

    # ---- time conversion ------------------------------------------------
    def offset_at(self, local: float) -> float:
        dt = local - self.t0
        return self.base + self.skew * dt + self.extra_rate * min(max(dt, 0.0), self.extra_dur)

    def to_cluster(self, local: float) -> float:
        return local + self.offset_at(local)

    def to_local(self, cluster: float) -> float:
        loc = cluster - self.offset_at(cluster - self.base)
        for _ in range(3):
            loc = cluster - self.offset_at(loc)
        return loc

    # ---- leader -----------------------------------------------------------
    def freeze(self, local_now: float) -> None:
        """Become the timebase: keep cluster time continuous, stop slewing."""
        off = self.offset_at(local_now)
        self.base, self.t0, self.skew, self.extra_rate, self.extra_dur = off, local_now, 0.0, 0.0, 0.0
        self.frozen = True
        self.synced = True
        self.samples.clear()
        self.history.clear()

    def unfreeze(self) -> None:
        self.frozen = False
        self.samples.clear()

    @staticmethod
    def answer_ping(ping: dict, recv_cluster: float, send_cluster: float) -> dict:
        return {"t": "pong", "n": ping["n"], "t1": ping["t1"], "t2": recv_cluster, "t3": send_cluster}

    # ---- follower ---------------------------------------------------------
    def make_ping(self, local_now: float) -> dict:
        self._seq += 1
        self._pending[self._seq] = local_now
        if len(self._pending) > 64:
            for k in sorted(self._pending)[:32]:
                del self._pending[k]
        return {"t": "ping", "n": self._seq, "t1": local_now}

    def reset_samples(self) -> None:
        """New leader: its clock continues the cluster timebase, but samples restart.

        The skew history is dropped too (the new leader's oscillator defines the
        rate now); the last skew estimate is kept until a new baseline exists."""
        self.samples.clear()
        self._pending.clear()
        self.history.clear()

    def on_pong(self, pong: dict, local_now: float) -> Sample | None:
        t1 = self._pending.pop(int(pong.get("n", -1)), None)
        if t1 is None or abs(t1 - float(pong["t1"])) > 1e-9:
            return None  # unknown / duplicated / forged ping
        t2, t3, t4 = float(pong["t2"]), float(pong["t3"]), local_now
        delay = (t4 - t1) - (t3 - t2)
        if delay < 0 or delay > 2.0 or t3 < t2:
            return None
        s = Sample(t4, ((t2 - t1) + (t3 - t4)) / 2.0, delay)
        self.add_sample(s)
        return s

    def add_sample(self, s: Sample) -> None:
        if self.frozen:
            return
        self.samples.append(s)
        if len(self.samples) > self.window:
            self.samples = self.samples[-self.window:]
        self._update(s.local)

    def good_samples(self) -> list[Sample]:
        if not self.samples:
            return []
        dmin = min(s.delay for s in self.samples)
        limit = dmin * self.outlier_delay_factor + 0.002
        good = [s for s in self.samples if s.delay <= limit]
        return good

    def _estimate(self, now: float) -> tuple[float, float] | None:
        """Offset from the short window (lowest-delay half, median); skew from the
        long-term history of our own estimates (needs minutes of baseline: a 32 s
        window with ~1 ms noise cannot resolve tens of ppm)."""
        good = self.good_samples()
        if not good:
            return None
        skew = self.skew
        h = self.history
        if len(h) >= 6 and h[-1][0] - h[0][0] >= 60.0:
            xs = [x - now for x, _ in h]
            ys = [y for _, y in h]
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            sxx = sum((x - mx) ** 2 for x in xs)
            if sxx > 0:
                skew = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
                skew = max(-200e-6, min(200e-6, skew))
        best = sorted(good, key=lambda s: s.delay)[: max(1, (len(good) + 1) // 2)]
        offset = statistics.median(s.offset + skew * (now - s.local) for s in best)
        if not h or now - h[-1][0] >= self.history_step:
            h.append((now, offset))
            if len(h) > self.history_len:
                del h[: len(h) - self.history_len]
        return offset, skew

    def _update(self, now: float) -> None:
        est = self._estimate(now)
        if est is None:
            return
        target, skew = est
        self.last_good = now
        current = self.offset_at(now)
        err = target - current
        if not self.synced or abs(err) > self.step_threshold:
            self.base, self.t0, self.skew, self.extra_rate, self.extra_dur = target, now, skew, 0.0, 0.0
            if self.synced:
                self.steps += 1
                self.step_epoch += 1
            self.synced = True
            return
        rate = self.max_slew_ppm * 1e-6
        dur = abs(err) / rate
        self.base, self.t0, self.skew = current, now, skew
        self.extra_rate = math.copysign(rate, err) if err else 0.0
        self.extra_dur = dur

    # ---- reporting ----------------------------------------------------------
    def stats(self, now: float) -> SyncStats:
        good = self.good_samples()
        rtt = statistics.median(s.delay for s in good) * 1000 if good else float("nan")
        if len(good) >= 3:
            res = [s.offset - self.offset_at(s.local) for s in good]
            jitter = statistics.pstdev(res) * 1000
        else:
            jitter = float("nan")
        age = None if self.last_good is None else now - self.last_good
        dmin = min((s.delay for s in good), default=0.0)
        unc = (dmin / 2) * 1000 + (0 if math.isnan(jitter) else jitter)
        if age is not None and not self.frozen:
            unc += age * self.drift_ppm_bound * 1e-3  # ms: worst-case drift since last sample
        if self.frozen:
            unc = 0.0
        return SyncStats(self.offset_at(now), self.skew * 1e6, rtt, jitter, unc, len(self.samples), age,
                         self.steps, self.synced)
