"""Integration tests on the discrete-event simulator (SIMULATED, not hardware)."""
import pytest

from backend.cluster.simulation import SimWorld
from backend.sync.timeline import TimingSource


def run_until(w, pred, limit, step=0.25):
    t0 = w.now
    while w.now - t0 < limit:
        if pred():
            return w.now - t0
        w.run_for(step)
    raise AssertionError(f"condition not reached in {limit}s; states={w.states()}")


def settled(w, leader=None):
    ls = w.leaders()
    if len(ls) != 1 or (leader and ls[0] != leader):
        return False
    return all(s in ("LEADER", "FOLLOWER") for s in w.states().values())


@pytest.mark.parametrize("n", [3, 5])
def test_simultaneous_boot_elects_highest_priority(n):
    w = SimWorld(n, seed=n)
    dt = run_until(w, lambda: settled(w, "pi-01"), 120)
    assert dt < 60
    w.run_for(20)
    assert settled(w, "pi-01")
    assert w.beat_spread_ms() < 5.0


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_master_crash_failover_and_return_without_preemption(seed):
    w = SimWorld(4, seed=seed)
    run_until(w, lambda: settled(w, "pi-01"), 120)
    w.run_for(20)
    beat_before = w.handles["pi-03"].node.timeline.bpm
    blob = w.crash("pi-01")
    dt = run_until(w, lambda: settled(w, "pi-02"), 120)
    assert dt < 45, dt  # see docs/network-recovery.md for the timing budget
    # clients continued rendering: timeline is continuous, tempo kept
    assert w.handles["pi-03"].node.timeline.bpm == beat_before
    w.run_for(20)
    assert w.beat_spread_ms() < 5.0
    # old leader returns (reboot with cache): joins as follower, pi-02 stays leader
    w.add_node("pi-01", cache=blob, new_boot=True)
    run_until(w, lambda: w.states().get("pi-01") == "FOLLOWER", 120)
    w.run_for(60)
    assert w.leaders() == ["pi-02"]
    assert w.beat_spread_ms() < 5.0


def test_followers_keep_predicting_during_outage():
    w = SimWorld(3, seed=7)
    run_until(w, lambda: settled(w, "pi-01"), 120)
    w.run_for(20)
    w.crash("pi-01")
    # during the outage every survivor keeps a sane, advancing beat
    for _ in range(10):
        w.run_for(1.0)
        for nid in ("pi-02", "pi-03"):
            h = w.handles[nid]
            c = h.node.cluster_now(h.clock.local(w.now))
            assert h.node.timeline.beat_at(c) > 0
    # spread stays bounded by drift during the outage (no network at all)
    assert w.beat_spread_ms(["pi-02", "pi-03"]) < 5.0


def test_split_brain_scout_reconciles_to_single_leader():
    w = SimWorld(5, seed=11)
    run_until(w, lambda: settled(w, "pi-01"), 120)
    w.run_for(10)
    w.partition(["pi-01", "pi-04"], ["pi-02", "pi-03", "pi-05"])
    run_until(w, lambda: len(w.leaders()) == 2, 150)
    w.run_for(30)
    assert sorted(w.leaders()) == ["pi-01", "pi-02"]
    w.heal()
    dt = run_until(w, lambda: settled(w), 400)
    w.run_for(30)
    # pi-01 holds the older epoch and is never preempted by the newer leader
    assert w.leaders() == ["pi-01"], w.states()
    assert w.handles["pi-02"].node.election.conflicts_resolved >= 1
    assert w.beat_spread_ms() < 5.0
    # the merged clocks/timelines converged
    revs = {h.node.timeline.epoch for h in w.handles.values()}
    assert len(revs) == 1


def test_no_oscillation_after_merge():
    w = SimWorld(5, seed=12)
    run_until(w, lambda: settled(w, "pi-01"), 120)
    w.partition(["pi-01", "pi-04"], ["pi-02", "pi-03", "pi-05"])
    w.run_for(120)
    w.heal()
    run_until(w, lambda: settled(w), 400)
    flips_before = sum(len(h.node.election.transitions) for h in w.handles.values())
    w.run_for(300)
    flips_after = sum(len(h.node.election.transitions) for h in w.handles.values())
    assert flips_after - flips_before == 0
    assert len(w.leaders()) == 1


def test_controlled_handover():
    w = SimWorld(3, seed=5)
    run_until(w, lambda: settled(w, "pi-01"), 120)
    w.run_for(10)
    lead = w.handles["pi-01"].node
    lead.command("handover", {"target": "pi-03"}, w.handles["pi-01"].clock.local(w.now))
    dt = run_until(w, lambda: settled(w, "pi-03"), 90)
    assert dt < 40
    w.run_for(30)
    assert w.leaders() == ["pi-03"]


@pytest.mark.parametrize("bpm", [100, 120, 128, 140, 160])
def test_all_clients_predict_same_future_beats(bpm):
    """Acceptance: identical future beat timestamps within measured clock error (SIMULATED)."""
    w = SimWorld(4, seed=int(bpm))
    w.start_music(bpm, ["pi-03"])  # audio on a follower
    run_until(w, lambda: settled(w, "pi-01"), 120)
    w.run_for(45)
    nodes = sorted(w.states())
    for h in w.handles.values():
        assert h.node.timeline.source == TimingSource.LIVE
        assert h.node.timeline.bpm == pytest.approx(bpm, abs=0.01)
    spread = w.beat_spread_ms(nodes)
    unc = max(h.node.sync.stats(h.clock.local(w.now)).uncertainty_ms for h in w.handles.values()
              if not h.node.is_leader())
    assert spread <= 2 * unc + 1.0, (spread, unc)
    # and the beats line up with the actual music (onset noise 4 ms, delay asymmetry)
    for nid in nodes:
        assert abs(w.music_phase_error_ms(nid)) < 15.0


def test_audio_loss_switches_to_predicted_and_keeps_phase():
    w = SimWorld(3, seed=21)
    w.start_music(128, ["pi-02"])
    run_until(w, lambda: settled(w, "pi-01"), 120)
    w.run_for(30)
    w.stop_music()
    w.run_for(10)
    for h in w.handles.values():
        assert h.node.timeline.source == TimingSource.PREDICTED
    # phase still right for a while (free-running at last BPM)
    w.music = type(w.music) if False else w.music
    assert w.beat_spread_ms() < 5.0


def test_audio_node_crash_failover_live_to_predicted():
    """Replacement leader without audio cannot detect new music: becomes PREDICTED."""
    w = SimWorld(3, seed=22)
    w.start_music(140, ["pi-01"])
    run_until(w, lambda: settled(w, "pi-01"), 120)
    w.run_for(30)
    w.crash("pi-01")
    run_until(w, lambda: settled(w, "pi-02"), 120)
    w.run_for(5)
    tl = w.handles["pi-02"].node.timeline
    assert tl.source == TimingSource.PREDICTED and tl.bpm == pytest.approx(140, abs=0.01)


def test_offsets_survive_failover_and_reboot():
    w = SimWorld(3, seed=31)
    run_until(w, lambda: settled(w, "pi-01"), 120)
    w.run_for(10)
    h1 = w.handles["pi-01"]
    loc = h1.clock.local(w.now)
    h1.node.command("cal_start", {"mode": "single", "target": "pi-03"}, loc)
    h1.node.command("cal_adjust", {"delta_ms": -20}, loc)
    h1.node.command("cal_adjust", {"delta_ms": -5}, loc)
    h1.node.command("cal_save", {}, loc)
    h1.node.command("cal_cancel", {}, loc)
    w.run_for(5)
    for h in w.handles.values():
        assert h.node.shared.calibration.offsets_ms == {"pi-03": -25.0}
    assert w.handles["pi-03"].node.visual_offset_ms() == -25.0
    w.crash("pi-01")
    run_until(w, lambda: settled(w, "pi-02"), 120)
    assert w.handles["pi-02"].node.shared.calibration.offsets_ms == {"pi-03": -25.0}
    blob = w.crash("pi-03")
    h3 = w.add_node("pi-03", cache=blob, new_boot=True)  # reboot, cached config restored
    assert h3.node.visual_offset_ms() == -25.0
    run_until(w, lambda: w.states().get("pi-03") == "FOLLOWER", 120)
    w.run_for(5)
    assert w.handles["pi-03"].node.visual_offset_ms() == -25.0


def test_offsets_changed_in_partition_are_merged():
    w = SimWorld(4, seed=41)
    run_until(w, lambda: settled(w, "pi-01"), 120)
    w.partition(["pi-01", "pi-02"], ["pi-03", "pi-04"])
    run_until(w, lambda: "pi-03" in w.leaders(), 150)
    h3 = w.handles["pi-03"]
    w.run_for(10)
    loc = h3.clock.local(w.now)
    h3.node.command("cal_start", {"mode": "single", "target": "pi-04"}, loc)
    h3.node.command("cal_adjust", {"offset_ms": 12}, loc)
    h3.node.command("cal_save", {}, loc)
    w.heal()
    run_until(w, lambda: settled(w), 400)
    w.run_for(30)
    for h in w.handles.values():
        assert h.node.shared.calibration.offsets_ms.get("pi-04") == 12.0, h.nid
