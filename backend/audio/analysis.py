"""Real-time beat tracking (NumPy only; runs on a Pi 3 core in a few % CPU).

Pipeline per hop (blocksize samples, ~10.7 ms at 48 kHz/512):
  1. Hann-windowed FFT of the last ``2*hop`` samples
  2. log-compressed magnitude -> spectral flux onset detection function (ODF),
     low frequencies weighted up (kick drums carry the beat in DJ music)
  3. adaptive-threshold peak picking -> onsets, timestamped on the *capture
     clock* (ADC time of the analysis window centre, converted to local
     CLOCK_MONOTONIC by the capture layer) — processing delay does not matter
  4. tempo every 0.5 s: autocorrelation of the last 8 s of ODF, comb-summed
     over multiples, log-Gaussian tempo prior, folded into [min_bpm, max_bpm],
     octave (half/double) hysteresis, median + exponential smoothing
  5. Beat-PLL: predicted beat grid; matched onsets correct phase (gain a) and
     period (gain b); periodic comb-filter phase check re-acquires if the PLL
     drifted; emits the next *future* beat time + BPM + confidence.

Outputs are estimates with explicit confidence; the state machine distinguishes
ACQUIRING / LIVE / SILENCE so the cluster can mark timing LIVE vs PREDICTED.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np


@dataclass
class AudioParams:
    samplerate: int = 48000
    hop: int = 512
    min_bpm: float = 80.0
    max_bpm: float = 180.0
    prior_bpm: float = 124.0
    prior_octaves: float = 0.9
    silence_rms: float = 0.003
    silence_hold_s: float = 2.5
    history_s: float = 8.0
    tempo_every_s: float = 0.5
    pll_phase_gain: float = 0.18
    pll_period_gain: float = 0.015
    beat_offset_ms: float = 0.0     # systematic detector lag correction (+ = beats later)
    n_spectrum: int = 32


@dataclass
class BeatEstimate:
    bpm: float
    next_beat: float        # local monotonic seconds (future)
    confidence: float
    phase_unc_ms: float
    state: str


@dataclass
class Features:
    t: float
    rms: float
    bass: float
    mid: float
    treble: float
    spectrum: list[float]
    onset: bool


class BeatTracker:
    def __init__(self, p: AudioParams | None = None) -> None:
        self.p = p or AudioParams()
        sr, hop = self.p.samplerate, self.p.hop
        self.n_fft = 2 * hop
        self.win = np.hanning(self.n_fft).astype(np.float32)
        self.buf = np.zeros(self.n_fft, dtype=np.float32)
        self.freqs = np.fft.rfftfreq(self.n_fft, 1.0 / sr)
        self.fps = sr / hop
        self.low_w = np.where(self.freqs < 150, 3.0, np.where(self.freqs < 400, 1.5, 1.0)).astype(np.float32)
        self.low_mask = self.freqs < 200  # kick band: beat *phase* follows the kick, not hats
        self.prev_log: np.ndarray | None = None
        n_hist = int(self.p.history_s * self.fps)
        self.odf = deque(maxlen=n_hist)
        self.odf_t = deque(maxlen=n_hist)
        self.odf_low = deque(maxlen=n_hist)
        self.recent = deque(maxlen=int(0.6 * self.fps))
        self.last_onset_t = -1e9
        self.onsets: deque = deque(maxlen=64)
        # spectrum bands (log spaced 40 Hz .. 16 kHz)
        edges = np.geomspace(40, min(16000, sr / 2 - 1), self.p.n_spectrum + 1)
        self.band_idx = [np.where((self.freqs >= lo) & (self.freqs < hi))[0] for lo, hi in zip(edges[:-1], edges[1:])]
        self.band_idx = [ix if len(ix) else np.array([int(np.argmin(abs(self.freqs - lo)))]) for ix, lo in
                         zip(self.band_idx, edges[:-1])]
        self.band_mat = np.zeros((self.p.n_spectrum, len(self.freqs)), dtype=np.float32)
        for i, ix in enumerate(self.band_idx):
            self.band_mat[i, ix] = 1.0 / len(ix)
        f = self.freqs
        self.lvl_mat = np.zeros((3, len(f)), dtype=np.float32)
        for i, (lo, hi) in enumerate(((20, 250), (250, 4000), (4000, 16000))):
            m = (f >= lo) & (f < hi)
            self.lvl_mat[i, m] = 1.0 / max(1, int(m.sum()))
        self._onset_pending = False
        self.agc = 1e-3
        self.agc_bands = np.full(3, 1e-3)
        # tempo state
        self.bpm: float | None = None
        self.bpm_hist: deque = deque(maxlen=5)
        self.tempo_conf = 0.0
        self.last_tempo_t = -1e9
        self.octave_challenger: tuple[float, int] | None = None
        # PLL
        self.period: float | None = None
        self.next_beat: float | None = None
        self.lock = 0.0
        self.err_hist: deque = deque(maxlen=16)
        self.beat_no = 0                      # index of next_beat
        self.matched: deque = deque(maxlen=24)  # (onset time, beat index) for period regression
        self.last_comb_t = -1e9
        # silence
        self.silent_since: float | None = None
        self.state = "ACQUIRING"
        self.frames = 0

    # ------------------------------------------------------------------ hop
    def process(self, block: np.ndarray, t_block_start: float) -> tuple[list[BeatEstimate], Features | None]:
        """Feed one hop of mono float samples whose first sample was captured at
        ``t_block_start`` (local monotonic seconds). Returns beats emitted
        during this hop and the latest features."""
        hop = self.p.hop
        if block.shape[0] != hop:
            raise ValueError(f"block must have {hop} samples")
        sr = self.p.samplerate
        self.buf = np.concatenate((self.buf[hop:], block.astype(np.float32, copy=False)))
        t_center = t_block_start + hop / sr - (self.n_fft / 2) / sr  # centre of analysis window
        t_now = t_block_start + hop / sr                              # capture time of newest sample
        self.frames += 1
        rms = float(np.sqrt(np.mean(block * block)))
        mag = np.abs(np.fft.rfft(self.buf * self.win))
        logm = np.log1p(100.0 * mag)
        if self.prev_log is None:
            flux = lflux = 0.0
        else:
            d = np.maximum(logm - self.prev_log, 0.0)
            flux = float(np.sum(d * self.low_w))
            lflux = float(np.sum(d[self.low_mask]))
        self.prev_log = logm
        self.odf.append(flux)
        self.odf_low.append(lflux)
        self.odf_t.append(t_center)
        onset = self._peak_pick(lflux, t_center)
        out: list[BeatEstimate] = []
        # silence handling
        if rms < self.p.silence_rms:
            if self.silent_since is None:
                self.silent_since = t_now
            if t_now - self.silent_since > self.p.silence_hold_s and self.state != "SILENCE":
                self.state = "SILENCE"
                self.lock = 0.0
        else:
            self.silent_since = None
            if self.state == "SILENCE":
                self.state = "ACQUIRING"
        if self.state != "SILENCE":
            if t_now - self.last_tempo_t >= self.p.tempo_every_s and len(self.odf) > 3 * self.fps:
                self.last_tempo_t = t_now
                self._estimate_tempo()
            if onset is not None:
                self._pll_onset(*onset)
            out = self._pll_advance(t_now)
            if self.period and t_now - self.last_comb_t > 2.0 and len(self.odf) > 4 * self.fps:
                self.last_comb_t = t_now
                self._comb_phase_check(t_now)
        feat = self._features(mag, rms, t_now, onset is not None)
        return out, feat

    # --------------------------------------------------------------- onsets
    def _peak_pick(self, flux: float, t: float) -> tuple[float, float] | None:
        self.recent.append(flux)
        if len(self.recent) < 5:
            return None
        arr = np.fromiter(self.recent, dtype=np.float64)
        thr = float(np.median(arr)) * 2.0 + 0.25 * float(np.max(arr)) + 1e-6
        # local max check on the previous frame (1 hop latency, timestamp is exact)
        a, b, c = arr[-3], arr[-2], arr[-1]
        if b > a and b >= c and b > thr:
            t_peak = self.odf_t[-2]
            if t_peak - self.last_onset_t > 0.08:
                self.last_onset_t = t_peak
                strength = b / (thr + 1e-9)
                self.onsets.append((t_peak, strength))
                return t_peak, strength
        return None

    # ---------------------------------------------------------------- tempo
    def _estimate_tempo(self) -> None:
        full = np.fromiter(self.odf, dtype=np.float64)
        low = np.fromiter(self.odf_low, dtype=np.float64)
        x = full / (full.std() + 1e-9) + low / (low.std() + 1e-9)
        x = x - x.mean()
        n = len(x)
        if not np.any(x):
            return
        nfft = 1 << (2 * n - 1).bit_length()
        X = np.fft.rfft(x, nfft)
        ac = np.fft.irfft(X * np.conj(X), nfft)[:n]
        ac /= ac[0] + 1e-12
        fps = self.fps
        lo = int(60 * fps / (self.p.max_bpm * 1.0)) - 1
        hi = int(60 * fps / (self.p.min_bpm * 0.5)) + 2   # allow half tempo candidates
        hi = min(hi, n // 4)
        if hi <= lo + 2:
            return
        lags = np.arange(max(lo, 2), hi)
        score = np.zeros(len(lags))
        for k, w in ((1, 1.0), (2, 0.6), (3, 0.3), (4, 0.4)):
            idx = lags * k
            valid = idx < n
            score[valid] += w * ac[idx[valid]]
        bpms = 60.0 * fps / lags
        prior = np.exp(-0.5 * (np.log2(bpms / self.p.prior_bpm) / self.p.prior_octaves) ** 2)
        weighted = score * prior
        i = int(np.argmax(weighted))
        if 0 < i < len(lags) - 1:  # parabolic interpolation for sub-frame lag
            y0, y1, y2 = weighted[i - 1], weighted[i], weighted[i + 1]
            den = y0 - 2 * y1 + y2
            frac = 0.5 * (y0 - y2) / den if den != 0 else 0.0
        else:
            frac = 0.0
        lag = lags[i] + max(-0.5, min(0.5, frac))
        bpm = 60.0 * fps / lag
        while bpm < self.p.min_bpm:
            bpm *= 2
        while bpm > self.p.max_bpm:
            bpm /= 2
        peak = float(score[i])
        base = float(np.median(score))
        conf = max(0.0, min(1.0, (peak - base) / (abs(peak) + 1e-9)))
        self.tempo_conf = 0.7 * self.tempo_conf + 0.3 * conf
        self._accept_bpm(bpm)

    def _accept_bpm(self, cand: float) -> None:
        if self.bpm is None:
            self.bpm_hist.append(cand)
            if len(self.bpm_hist) >= 3:
                med = float(np.median(self.bpm_hist))
                if all(abs(b - med) / med < 0.03 for b in list(self.bpm_hist)[-3:]):
                    self.bpm = med
                    self.period = 60.0 / med
            return
        ratio = cand / self.bpm
        # octave errors: treat half/double (and 2/3, 3/2) as the same tempo unless persistent
        for r in (2.0, 0.5, 1.5, 2 / 3):
            if abs(ratio - r) / r < 0.04:
                ch = self.octave_challenger
                if ch and abs(ch[0] - cand) / cand < 0.04:
                    self.octave_challenger = (cand, ch[1] + 1)
                    if ch[1] + 1 >= 16:  # ~8 s of consistent evidence
                        self._switch_tempo(cand)
                else:
                    self.octave_challenger = (cand, 1)
                return
        self.octave_challenger = None
        self.bpm_hist.append(cand)
        med = float(np.median(self.bpm_hist))
        if abs(med - self.bpm) / self.bpm > 0.04:
            # genuine tempo change: require agreement of the last 3 estimates
            last = list(self.bpm_hist)[-3:]
            if len(last) == 3 and all(abs(b - med) / med < 0.02 for b in last):
                self._switch_tempo(med)
            return
        new = self.bpm + 0.3 * (med - self.bpm)
        self.bpm = new
        if self.period is not None and self.lock < 0.5:
            # unlocked: the ACF estimate steers the PLL period; once locked, the
            # onset regression (below) is far more precise than ACF lag resolution
            self.period += 0.3 * (60.0 / new - self.period)

    def _switch_tempo(self, bpm: float) -> None:
        self.bpm = bpm
        self.period = 60.0 / bpm
        self.bpm_hist.clear()
        self.bpm_hist.append(bpm)
        self.octave_challenger = None
        self.lock *= 0.5
        self.matched.clear()
        self.next_beat = None  # re-acquire phase with the comb filter

    # ------------------------------------------------------------------ PLL
    def _comb_phase(self, now: float, period: float) -> tuple[float, float]:
        """Best beat phase (time of a beat) over the last 4 s of ODF; returns (t_beat, strength)."""
        ts = np.fromiter(self.odf_t, dtype=np.float64)
        x = np.fromiter(self.odf_low, dtype=np.float64)
        sel = ts > now - 4.0
        ts, x = ts[sel], x[sel]
        if len(x) < 10:
            return now, 0.0
        x = x - np.median(x)
        steps = max(8, int(period * self.fps))
        best_s, best_t = -1e18, now
        for j in range(steps):
            phi = now - period * (j / steps)
            ph = ((ts - phi) / period) % 1.0
            d = np.minimum(ph, 1 - ph) * period
            s = float(np.sum(x * np.exp(-0.5 * (d / 0.018) ** 2)))
            if s > best_s:
                best_s, best_t = s, phi
        total = float(np.sum(np.abs(x))) + 1e-9
        return best_t, best_s / total

    def _pll_onset(self, t: float, strength: float) -> None:
        if self.period is None or self.next_beat is None:
            return
        P = self.period
        k = round((t - self.next_beat) / P)
        nearest = self.next_beat + k * P
        e = t - nearest
        if abs(e) > 0.2 * P:
            return
        w = min(1.0, strength / 3.0)
        self.next_beat += self.p.pll_phase_gain * w * e
        dp = self.p.pll_period_gain * w * e
        self.matched.append((t, self.beat_no + k))
        new_p = self.period + dp
        if len(self.matched) >= 8:
            ts = np.array([m[0] for m in self.matched])
            ns = np.array([m[1] for m in self.matched], dtype=np.float64)
            if ns.max() - ns.min() >= 6:
                slope = float(np.polyfit(ns, ts, 1)[0])
                new_p = 0.5 * new_p + 0.5 * slope
        if self.bpm:
            ref = 60.0 / self.bpm
            self.period = min(ref * 1.03, max(ref * 0.97, new_p))
        self.err_hist.append(e)
        q = 1.0 - min(1.0, abs(e) / (0.1 * P))
        self.lock = 0.85 * self.lock + 0.15 * q

    def _pll_advance(self, now: float) -> list[BeatEstimate]:
        if self.period is None:
            return []
        if self.next_beat is None:
            t_b, strength = self._comb_phase(now, self.period)
            if strength <= 0:
                return []
            self.next_beat = t_b
            while self.next_beat <= now:
                self.next_beat += self.period
            self.lock = 0.3
        out = []
        # emit the upcoming beat once per beat, when the previous one passes
        while self.next_beat <= now:
            self.next_beat += self.period
            self.beat_no += 1
            self.lock *= 0.97  # decays unless onsets keep confirming
            out.append(self._estimate())
        if self.state != "SILENCE":
            self.state = "LIVE" if self.confidence() >= 0.35 else "ACQUIRING"
        return out

    def _comb_phase_check(self, now: float) -> None:
        assert self.period is not None and self.next_beat is not None
        t_b, strength = self._comb_phase(now, self.period)
        e = ((t_b - self.next_beat) / self.period + 0.5) % 1.0 - 0.5
        if abs(e) > 0.25 and strength > 0.02 and self.lock < 0.6:
            # PLL locked to off-beats/drifted: re-acquire phase from the comb
            nb = t_b
            while nb <= now:
                nb += self.period
            self.next_beat = nb
            self.matched.clear()
            self.lock = 0.3

    def confidence(self) -> float:
        return max(0.0, min(1.0, 0.5 * self.tempo_conf + 0.5 * self.lock))

    def _estimate(self) -> BeatEstimate:
        assert self.period is not None and self.next_beat is not None
        unc = float(np.std(self.err_hist) * 1000) if len(self.err_hist) >= 4 else 50.0
        return BeatEstimate(bpm=60.0 / self.period, next_beat=self.next_beat + self.p.beat_offset_ms / 1000.0,
                            confidence=self.confidence(), phase_unc_ms=unc, state=self.state)

    # -------------------------------------------------------------- features
    def _features(self, mag: np.ndarray, rms: float, t: float, onset: bool) -> Features | None:
        self._onset_pending = self._onset_pending or onset
        if self.frames % 3:  # ~31 Hz is plenty for visuals (sent at 20 Hz)
            return None
        bands = self.band_mat @ mag
        lb = np.log1p(bands * 10.0)
        self.agc = max(self.agc * 0.997, float(lb.max()), 1e-3)
        spec = np.clip(lb / self.agc, 0, 1)
        e = self.lvl_mat @ mag
        self.agc_bands = np.maximum(self.agc_bands * 0.995, e)
        lv = np.clip(e / (self.agc_bands + 1e-9), 0, 1)
        on, self._onset_pending = self._onset_pending, False
        return Features(t, rms, float(lv[0]), float(lv[1]), float(lv[2]), np.round(spec, 3).tolist(), on)
