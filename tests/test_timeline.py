import math

import pytest

from backend.sync.timeline import Timeline, TimingSource, TapTempo, reconcile, wrap_half


def test_beat_at_and_inverse():
    tl = Timeline(bpm=128.0, anchor_time=1000.0, anchor_beat=10.0)
    assert tl.beat_at(1000.0) == pytest.approx(10.0)
    assert tl.beat_at(1000.0 + 60 / 128) == pytest.approx(11.0)
    for b in (10.0, 11.5, 300.25, 7.0):
        assert tl.beat_at(tl.time_of_beat(b)) == pytest.approx(b, abs=1e-9)


def test_tempo_change_is_phase_continuous():
    tl = Timeline(bpm=120.0, anchor_time=0.0, anchor_beat=0.0)
    t = 10.3
    before = tl.beat_at(t)
    tl2 = tl.with_tempo(t, 140.0)
    assert tl2.beat_at(t) == pytest.approx(before, abs=1e-12)
    assert tl2.beat_at(t + 60 / 140) == pytest.approx(before + 1)


@pytest.mark.parametrize("bpm", [100, 120, 128, 140, 160])
def test_same_future_beats_from_serialised_state(bpm):
    """Every client evaluating the same replicated state predicts identical beats."""
    tl = Timeline(bpm=bpm, anchor_time=5000.123, anchor_beat=77.25, revision=3)
    clones = [Timeline.from_dict(tl.to_dict()) for _ in range(5)]
    ref = tl.upcoming(5003.0, 64, 0.25)
    for c in clones:
        got = c.upcoming(5003.0, 64, 0.25)
        for (b1, t1), (b2, t2) in zip(ref, got):
            assert b1 == b2
            assert t1 == pytest.approx(t2, abs=1e-12)
    # beat spacing matches tempo
    beats = tl.upcoming(5003.0, 8, 1.0)
    for (_, a), (_, b) in zip(beats, beats[1:]):
        assert b - a == pytest.approx(60 / bpm, abs=1e-9)


def test_subdivisions_and_grid():
    tl = Timeline(bpm=120.0)
    b, t = tl.next_grid_time(0.01, 4.0)
    assert (b, t) == (4.0, pytest.approx(2.0))
    b, t = tl.next_grid_time(0.01, 0.25)
    assert b == 0.25 and t == pytest.approx(0.125)
    b, t = tl.next_grid_time(0.0, 1.0, lead=0.6)
    assert b == 2.0  # at least 0.6 s ahead -> beat 2 (t=1.0)


def test_reconcile_small_error_slews_without_jump():
    local = Timeline(bpm=120.0, anchor_time=0.0, anchor_beat=0.0)
    target = Timeline(bpm=120.0, anchor_time=0.0, anchor_beat=0.04, revision=2)
    t = 100.0
    r = reconcile(local, target, t, 0.06, 4.0)
    assert r.mode == "slew"
    tl = r.timeline
    assert tl.beat_at(t) == pytest.approx(local.beat_at(t), abs=1e-9)  # continuous
    end = t + 4 * 0.5
    assert tl.beat_at(end + 0.1) == pytest.approx(target.beat_at(end + 0.1), abs=1e-9)
    # monotonic and no jumps during the ramp
    prev = tl.beat_at(t)
    for i in range(1, 200):
        cur = tl.beat_at(t + i * 0.01)
        assert 0 < cur - prev < 0.03
        prev = cur


def test_reconcile_large_error_is_deferred_to_target_bar():
    local = Timeline(bpm=120.0, anchor_time=0.0, anchor_beat=0.0)
    target = Timeline(bpm=124.0, anchor_time=0.0, anchor_beat=9.3)  # many beats off
    t = 30.1
    r = reconcile(local, target, t)
    assert r.mode == "deferred"
    tl = r.timeline
    assert tl.beat_at(t) == pytest.approx(local.beat_at(t), abs=1e-9)
    t_step = tl.slew_start
    assert t_step > t
    # after the step we are exactly on the target, and the target is on a downbeat
    assert tl.beat_at(t_step) == pytest.approx(target.beat_at(t_step), abs=1e-9)
    assert target.beat_at(t_step) % 4 == pytest.approx(0, abs=1e-9)
    assert t_step - t <= 4 * 60 / 124 + 1e-9


def test_wrap_half():
    assert wrap_half(0.7) == pytest.approx(-0.3)
    assert wrap_half(-0.2) == pytest.approx(-0.2)
    assert wrap_half(1.5) == pytest.approx(-0.5)


def test_tap_tempo():
    tt = TapTempo()
    bpm = None
    for i in range(6):
        bpm = tt.tap(10 + i * 0.5)
    assert bpm == pytest.approx(120, abs=0.01)
    tt.tap(10 + 5 * 0.5 + 0.05)  # bounce ignored
    assert tt.bpm() == pytest.approx(120, abs=0.01)
    tt.tap(100)  # reset after pause
    assert tt.bpm() is None


def test_source_serialisation():
    tl = Timeline(source=TimingSource.LIVE)
    assert Timeline.from_dict(tl.to_dict()).source is TimingSource.LIVE
