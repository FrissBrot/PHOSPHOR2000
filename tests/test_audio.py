"""Beat tracker on synthetic signals (SIMULATED audio; real DJ material must be
validated on hardware, see docs/hardware.md)."""
import queue
import time
import types

import numpy as np
import pytest

from backend.audio.analysis import AudioParams, BeatTracker
from backend.audio.service import Analyzer, Capture
from tests.audio_synth import track

SR = 48000
HOP = 512
T0 = 5000.0  # arbitrary capture-clock origin


def run(x, bpm_fn=None, **params):
    bt = BeatTracker(AudioParams(samplerate=SR, hop=HOP, **params))
    ests, states = [], []
    for i in range(len(x) // HOP):
        t = T0 + i * HOP / SR
        out, _ = bt.process(x[i * HOP:(i + 1) * HOP], t)
        ests += [(t - T0, e) for e in out]
        states.append((t - T0, bt.state))
    return bt, ests, states


def phase_errors(ests, beats, t_from, t_to):
    errs = []
    for t, e in ests:
        nb = e.next_beat - T0
        if t_from <= t and nb < t_to:
            errs.append((nb - beats[np.argmin(abs(beats - nb))]) * 1000)
    return np.array(errs)


@pytest.mark.parametrize("bpm", [100, 120, 128, 140, 160, 174])
def test_tempo_and_phase(bpm):
    x, beats = track(lambda t: bpm, 30, sr=SR, seed=bpm)
    bt, ests, _ = run(x)
    late = [e for t, e in ests if t > 20]
    assert late and late[-1].state == "LIVE"
    assert np.median([e.bpm for e in late]) == pytest.approx(bpm, rel=0.003)
    err = phase_errors(ests, beats, 20, 29.5)
    assert abs(np.mean(err)) < 8.0 and np.std(err) < 4.0, (np.mean(err), np.std(err))
    assert late[-1].confidence > 0.6


def test_predicts_future_beats_not_past():
    x, beats = track(lambda t: 128, 20, sr=SR)
    _, ests, _ = run(x)
    for t, e in ests:
        assert e.next_beat - T0 > t  # always a future beat in capture time


def test_noise_and_hats_do_not_flip_phase():
    x, beats = track(lambda t: 128, 30, sr=SR, noise=0.05, seed=3)
    _, ests, _ = run(x)
    err = phase_errors(ests, beats, 20, 29.5)
    assert np.all(abs(err) < 30), err


def test_tempo_change_is_followed():
    fn = lambda t: 120.0 if t < 20 else 128.0
    x, beats = track(fn, 45, sr=SR)
    _, ests, _ = run(x)
    late = [e for t, e in ests if t > 38]
    assert np.median([e.bpm for e in late]) == pytest.approx(128, rel=0.004)
    err = phase_errors(ests, beats, 38, 44.5)
    assert abs(np.mean(err)) < 10


def test_halftime_kick_pattern_keeps_octave():
    x, beats = track(lambda t: 140, 30, sr=SR, kick_pattern=(1, 0, 1, 0))
    _, ests, _ = run(x)
    late = [e for t, e in ests if t > 20]
    assert np.median([e.bpm for e in late]) == pytest.approx(140, rel=0.01)


def test_silence_detection_and_reacquire():
    x, beats = track(lambda t: 128, 40, sr=SR, silence=(15, 25))
    _, ests, states = run(x, silence_hold_s=2.0)
    st = dict(states)
    s_in = [s for t, s in states if 18 < t < 24]
    assert set(s_in) == {"SILENCE"}
    assert not [e for t, e in ests if 18 < t < 24]  # no beats emitted during silence
    after = [s for t, s in states if t > 36]
    assert after[-1] == "LIVE"


def test_callback_never_blocks_and_counts_drops():
    cap = Capture(HOP, SR, None, maxsize=4)
    ti = types.SimpleNamespace(currentTime=10.0, inputBufferAdcTime=9.99)
    block = np.zeros((HOP, 1), dtype=np.float32)
    t = time.perf_counter()
    for _ in range(100):
        cap.callback(block, HOP, ti, 0)
    assert time.perf_counter() - t < 0.05
    assert cap.q.qsize() == 4 and cap.dropped == 96
    mono, adc = cap.q.get_nowait()
    assert mono.shape == (HOP,)
    assert 0.009 < time.monotonic() - adc < 0.5  # ADC time converted to local monotonic


def test_analyzer_rechunks_odd_driver_blocks():
    sent = []
    link = types.SimpleNamespace(send=sent.append)
    x, _ = track(lambda t: 128, 12, sr=SR)
    cap = Capture(HOP, SR, 0.0, maxsize=10000)
    an = Analyzer(cap, BeatTracker(AudioParams(samplerate=SR, hop=HOP)), link)
    t0 = 100.0
    i, sz = 0, 441
    while i + sz <= len(x):
        cap.q.put_nowait((x[i:i + sz], t0 + i / SR))
        i += sz
    an.start()
    deadline = time.time() + 30
    while cap.q.qsize() and time.time() < deadline:
        time.sleep(0.05)
    an.stop_ev.set()
    an.join(2)
    kinds = {m["t"] for m in sent}
    assert {"audio_beat", "audio_features"} <= kinds
    beats = [m for m in sent if m["t"] == "audio_beat"]
    assert beats[-1]["bpm"] == pytest.approx(128, rel=0.01)
