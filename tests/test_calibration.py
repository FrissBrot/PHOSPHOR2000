import random
import struct

import pytest

from backend.services.input_service import EVENT_FMT, is_keyboard, parse_events
from backend.sync.calibration import TapFilter, analyse_taps

BPM = 120.0
P = 0.5


def taps(lag_ms, n=20, sd_ms=8.0, origin=1000.0, seed=1, skip=()):
    r = random.Random(seed)
    return [origin + k * P + lag_ms / 1000 + r.gauss(0, sd_ms / 1000) for k in range(n) if k not in skip]


def test_lag_and_sign_convention():
    res = analyse_taps(taps(+40), 1000.0, BPM, current_offset_ms=0.0)
    assert res.mean_ms == pytest.approx(40, abs=5)
    # flash perceived late -> show visuals earlier -> negative offset
    assert res.suggested_offset_ms == pytest.approx(-40, abs=5)
    res2 = analyse_taps(taps(-25), 1000.0, BPM, current_offset_ms=-10.0)
    assert res2.suggested_offset_ms == pytest.approx(15, abs=5)


def test_phase_wrapping_near_half_period():
    # taps ~240 ms late at 120 BPM are close to the wrap point (±250 ms)
    res = analyse_taps(taps(240, sd_ms=10), 1000.0, BPM)
    assert abs(res.mean_ms) == pytest.approx(240, abs=12) or abs(res.mean_ms + 260) < 12
    # taps around 0 straddling the wrap at 0/period must average to ~0, not ~250
    res0 = analyse_taps(taps(0, sd_ms=10), 1000.0, BPM)
    assert abs(res0.mean_ms) < 6


def test_missing_taps_do_not_matter():
    res = analyse_taps(taps(20, n=30, skip={3, 4, 5, 11, 17, 22}), 1000.0, BPM)
    assert res.n_used == 24 and res.mean_ms == pytest.approx(20, abs=5)


def test_outliers_rejected():
    t = taps(15, n=20, sd_ms=5)
    t += [1000.0 + 3 * P + 0.180, 1000.0 + 9 * P - 0.150]  # two wild taps
    res = analyse_taps(sorted(t), 1000.0, BPM)
    assert res.n_rejected >= 2
    assert res.mean_ms == pytest.approx(15, abs=4)


def test_readiness_and_uncertainty():
    few = analyse_taps(taps(10, n=8), 1000.0, BPM, min_taps=16)
    assert not few.ready
    many = analyse_taps(taps(10, n=32), 1000.0, BPM, min_taps=16)
    assert many.ready and many.std_err_ms < few.std_err_ms
    sloppy = analyse_taps(taps(10, n=32, sd_ms=45), 1000.0, BPM)
    assert sloppy.confidence < many.confidence
    assert any("anticipation" in n for n in many.notes)  # never claims absolute latency


def test_tap_filter_repeat_release_double():
    f = TapFilter(P, 0.35)
    assert f.offer(10.0, 1)
    assert not f.offer(10.05, 2)   # autorepeat
    assert not f.offer(10.08, 0)   # release
    assert not f.offer(10.10, 1)   # accidental double press (< 0.35 beat)
    assert f.offer(10.5, 1)
    assert f.accepted == [10.0, 10.5] and f.ignored == 3


def test_evdev_parsing_and_capabilities(tmp_path):
    buf = struct.pack(EVENT_FMT, 12, 500000, 1, 28, 1) + struct.pack(EVENT_FMT, 12, 600000, 0, 0, 0)
    ev = parse_events(buf)
    assert ev[0] == (12.5, 1, 28, 1) and ev[1][1] == 0
    caps = tmp_path / "event3" / "device" / "capabilities"
    caps.mkdir(parents=True)
    (caps / "key").write_text("1000000000007 ff9f207ac14057ff febeffdfffefffff fffffffffffffffe\n")
    assert is_keyboard(tmp_path / "event3", sys_root=tmp_path)
    (caps / "key").write_text("0\n")
    assert not is_keyboard(tmp_path / "event3", sys_root=tmp_path)
