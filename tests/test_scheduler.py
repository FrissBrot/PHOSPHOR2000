import pytest

from backend.cluster.scheduler import Override, ScheduleState, default_schedule, plan, scene_at
from backend.config import ScenesConfig, load_config
from backend.sync.prng import Mulberry32, fnv1a, mix32

NODES = [f"pi-0{i}" for i in range(1, 6)]


def scenes(**kw):
    base = dict(playlist=[{"scene": "boot", "bars": 2}, {"scene": "dance", "bars": 4}, {"scene": "rain", "bars": 2}])
    base.update(kw)
    return ScenesConfig.model_validate(base)


def test_global_playlist_cycles_on_bars():
    st = default_schedule(scenes(), NODES)
    assert scene_at(st, "pi-01", 0) == ("boot", 0)
    assert scene_at(st, "pi-01", 7.75) == ("boot", 0)
    assert scene_at(st, "pi-01", 8) == ("dance", 8)
    assert scene_at(st, "pi-01", 24) == ("rain", 24)
    assert scene_at(st, "pi-01", 32) == ("boot", 32)
    # same for every node in GLOBAL
    assert {scene_at(st, n, 9)[0] for n in NODES} == {"dance"}


def test_plan_segments_are_contiguous_and_deterministic():
    st = default_schedule(scenes(), NODES)
    p1 = plan(st, "pi-02", 3.1, 70)
    p2 = plan(ScheduleState.from_dict(st.to_dict()), "pi-02", 3.1, 70)
    assert p1 == p2
    for a, b in zip(p1, p1[1:]):
        assert a.end_beat == b.start_beat
        assert a.scene != b.scene or a.origin_beat != b.origin_beat
    assert p1[0].start_beat <= 3.1


def test_seeds_differ_per_instance_but_stable():
    st = default_schedule(scenes(), NODES)
    segs = plan(st, "pi-01", 0, 64)
    boot = [s for s in segs if s.scene == "boot"]
    assert len(boot) == 2 and boot[0].seed != boot[1].seed
    assert segs == plan(st, "pi-01", 0, 64)


def test_individual_mode_rotates_by_node():
    st = default_schedule(scenes(mode="INDIVIDUAL", individual={"pi-05": {"scene": "countdown"}}), NODES)
    got = [scene_at(st, n, 0)[0] for n in NODES]
    assert got[:3] == ["boot", "dance", "rain"]
    assert got[4] == "countdown"
    assert scene_at(st, "pi-05", 999)[0] == "countdown"


def test_choreography_patterns():
    sc = scenes(mode="CHOREOGRAPHED", choreography=[
        {"bars": 2, "assign": {"*": "dance", "pi-03": "rain"}},
        {"bars": 2, "pattern": "rotate", "scenes": ["grid", "rain", "spectrum"]},
        {"bars": 2, "pattern": "wave", "scenes": ["boot", "countdown"]},
    ])
    st = default_schedule(sc, NODES)
    assert [scene_at(st, n, 1)[0] for n in NODES] == ["dance", "dance", "rain", "dance", "dance"]
    assert [scene_at(st, n, 8)[0] for n in NODES] == ["grid", "rain", "spectrum", "grid", "rain"]
    assert [scene_at(st, n, 12)[0] for n in NODES] == ["rain", "spectrum", "grid", "rain", "spectrum"]
    # wave: node i switches i beats after the step boundary (beat 16)
    assert scene_at(st, "pi-01", 16)[0] == "boot"
    assert scene_at(st, "pi-03", 16)[0] == "countdown"  # still previous bar of the wave
    assert scene_at(st, "pi-03", 18)[0] == "boot"


def test_override_on_quantized_boundary_and_targets():
    st = default_schedule(scenes(), NODES)
    st.add_override(Override("spectrum", start_beat=12.0, targets=["pi-02"]), now_beat=10.2)
    assert scene_at(st, "pi-02", 11.75)[0] == "dance"
    assert scene_at(st, "pi-02", 12.0) == ("spectrum", 12.0)
    assert scene_at(st, "pi-01", 12.0)[0] == "dance"
    st.add_override(Override("countdown", start_beat=16.0), now_beat=12.5)  # global supersedes
    assert scene_at(st, "pi-02", 15.75)[0] == "spectrum"
    assert scene_at(st, "pi-02", 16)[0] == "countdown"
    st.clear_overrides(at_beat=20.0)
    assert scene_at(st, "pi-02", 20)[0] == scene_at(st, "pi-01", 20)[0] == "dance"


def test_new_config_takes_effect_at_boundary_only():
    st = default_schedule(scenes(), NODES)
    st.set_scenes(scenes(playlist=[{"scene": "grid", "bars": 1}]), from_beat=40.0)
    assert scene_at(st, "pi-01", 39.75)[0] == "boot"  # old era: beat 39 -> 32..40 boot
    assert scene_at(st, "pi-01", 40.0) == ("grid", 40.0)
    assert scene_at(st, "pi-01", 44.0) == ("grid", 44.0)


def test_prng_golden_vectors():
    # Golden values are shared with tests/js/prng.test.mjs
    assert fnv1a("dance") == 0xF815289C
    r = Mulberry32(0x2000)
    vals = [r.next_u32() for _ in range(4)]
    assert vals == GOLDEN_MULBERRY
    assert mix32(1, 2, 3) == GOLDEN_MIX


GOLDEN_MULBERRY = [162931626, 3207651842, 2514598016, 2453501074]  # == reference JS mulberry32
GOLDEN_MIX = 2901138804


def test_config_loads_defaults(tmp_path):
    cfg = load_config(node_path=tmp_path / "none.yaml", secrets_path=tmp_path / "none2.yaml",
                      overrides={"node": {"id": "pi-03"}})
    assert cfg.node.id == "pi-03" and cfg.node.priority == 80
    assert cfg.scenes.playlist[0].scene == "boot"
    with pytest.raises(Exception):
        load_config(node_path=tmp_path / "n", secrets_path=tmp_path / "s", overrides={"node": {"id": "bad"}})


def test_secrets_permissions_enforced(tmp_path):
    s = tmp_path / "secrets.yaml"
    s.write_text("wifi_psk: supersecret\n")
    s.chmod(0o644)
    with pytest.raises(PermissionError):
        load_config(node_path=tmp_path / "n", secrets_path=s)
    s.chmod(0o600)
    assert load_config(node_path=tmp_path / "n", secrets_path=s).secrets.wifi_psk == "supersecret"
