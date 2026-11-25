import random

import pytest

from backend.sync.clocksync import ClockSync


class Pair:
    """Leader/follower clocks with arbitrary origins, drift and asymmetric Wi-Fi delay."""

    def __init__(self, seed=1, drift_ppm=35.0, offset=12345.678):
        self.rng = random.Random(seed)
        self.drift = drift_ppm * 1e-6
        self.offset = offset  # leader cluster time - follower local at true t=0

    def f_local(self, t):
        return 500.0 + t * (1 + self.drift)

    def l_cluster(self, t):
        return 500.0 + self.offset + t

    def exchange(self, cs: ClockSync, t, spike=False):
        ping = cs.make_ping(self.f_local(t))
        up = 0.002 + self.rng.expovariate(1 / 0.003) + (self.rng.uniform(0.03, 0.15) if spike else 0)
        down = 0.002 + self.rng.expovariate(1 / 0.003)
        t2 = self.l_cluster(t + up)
        t3 = t2 + 0.0002
        pong = ClockSync.answer_ping(ping, t2, t3)
        return cs.on_pong(pong, self.f_local(t + up + 0.0002 + down))

    def true_error_ms(self, cs, t):
        return (cs.to_cluster(self.f_local(t)) - self.l_cluster(t)) * 1000


def test_converges_and_tracks_drift():
    p = Pair()
    cs = ClockSync()
    t = 0.0
    for i in range(300):  # skew needs a baseline of minutes
        p.exchange(cs, t, spike=(i % 7 == 3))
        t += 1.0
    assert abs(p.true_error_ms(cs, t)) < 2.0
    assert cs.stats(p.f_local(t)).skew_ppm == pytest.approx(-35, abs=8)


def test_outliers_rejected():
    p = Pair(seed=3)
    cs = ClockSync()
    for i in range(40):
        p.exchange(cs, float(i), spike=(i % 2 == 0))  # half the samples delayed 30-150 ms one-way
    assert abs(p.true_error_ms(cs, 40.0)) < 3.0


def test_slew_is_rate_limited_and_large_errors_step():
    p = Pair(seed=4, drift_ppm=0)
    cs = ClockSync(max_slew_ppm=2000, step_threshold=0.05)
    for i in range(20):
        p.exchange(cs, float(i))
    assert cs.steps == 0
    # leader timebase jumps by 20 ms (e.g. failover to a leader with a slightly different estimate)
    p.offset += 0.020
    cs.reset_samples()
    before = cs.to_cluster(p.f_local(20.0))
    p.exchange(cs, 20.0)
    after = cs.to_cluster(p.f_local(20.0))
    assert after - before < 0.001  # no jump: slewed
    for i in range(21, 40):
        p.exchange(cs, float(i))
    assert abs(p.true_error_ms(cs, 40.0)) < 2.0
    # 300 ms error -> step and step_epoch increments (renderer defers to bar)
    p.offset += 0.300
    cs.reset_samples()
    ep = cs.step_epoch
    p.exchange(cs, 41.0)
    assert cs.step_epoch == ep + 1
    assert abs(p.true_error_ms(cs, 41.0)) < 10


def test_freewheel_prediction_during_outage():
    p = Pair(seed=5, drift_ppm=50)
    cs = ClockSync()
    for i in range(300):
        p.exchange(cs, float(i))
    # 10 minutes without any packet: skew compensation keeps error small
    err = p.true_error_ms(cs, 900.0)
    assert abs(err) < 5.0, err
    uncomp = 50e-6 * 600 * 1000
    assert abs(err) < uncomp / 3  # much better than no compensation (30 ms)
    st = cs.stats(p.f_local(900.0))
    assert st.uncertainty_ms > 50  # reported uncertainty grows honestly (100 ppm bound)


def test_rejects_unknown_or_forged_pongs():
    cs = ClockSync()
    assert cs.on_pong({"n": 99, "t1": 1.0, "t2": 5.0, "t3": 5.0}, 2.0) is None
    ping = cs.make_ping(1.0)
    assert cs.on_pong({"n": ping["n"], "t1": 1.5, "t2": 5, "t3": 5}, 2.0) is None


def test_leader_freeze_is_continuous():
    p = Pair(seed=6)
    cs = ClockSync()
    for i in range(30):
        p.exchange(cs, float(i))
    before = cs.to_cluster(p.f_local(30.0))
    cs.freeze(p.f_local(30.0))
    assert cs.to_cluster(p.f_local(30.0)) == pytest.approx(before, abs=1e-9)
    assert cs.to_local(cs.to_cluster(1234.5)) == pytest.approx(1234.5, abs=1e-9)
