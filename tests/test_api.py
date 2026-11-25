"""API, auth and input validation (FastAPI TestClient)."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.auth import hash_password, verify_password
from backend.config import load_config
from backend.network.base import ExternalNetworkAdapter
from backend.services.core import Core

PW = "correct horse battery"


@pytest.fixture()
def core(tmp_path):
    cfg = load_config(node_path=tmp_path / "n.yaml", secrets_path=tmp_path / "s.yaml",
                      overrides={"node": {"id": "pi-01"}, "network": {"discovery_window": 0.2, "backoff_jitter": 0},
                                 "secrets": {"admin_password_hash": hash_password(PW), "session_secret": "x" * 32}})
    c = Core(cfg, adapter=ExternalNetworkAdapter(), state_dir=tmp_path, has_audio=False)
    deadline = time.time() + 5
    while not c.node.is_leader() and time.time() < deadline:
        c.node.tick(time.monotonic())
        time.sleep(0.05)
    assert c.node.is_leader()
    return c


@pytest.fixture()
def client(core):
    return TestClient(create_app(core))


def login(client):
    r = client.post("/api/login", json={"password": PW})
    assert r.status_code == 200
    return client


def test_password_hashing():
    h = hash_password("abcdefgh")
    assert verify_password("abcdefgh", h) and not verify_password("abcdefgx", h)
    with pytest.raises(ValueError):
        hash_password("short")


def test_static_and_health(client):
    assert client.get("/").status_code == 200
    assert client.get("/admin/").status_code == 200
    r = client.get("/api/health").json()
    assert r["node"] == "pi-01" and r["state"] == "LEADER"
    assert "Content-Security-Policy" in client.get("/").headers


def test_auth_required_and_wrong_password(client):
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/cmd/set_bpm", json={"bpm": 120}, headers={"X-PHOS": "1"}).status_code == 401
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401


def test_rate_limit(client):
    codes = [client.post("/api/login", json={"password": "x"}).status_code for _ in range(7)]
    assert 429 in codes


def test_csrf_header_required(client):
    login(client)
    assert client.post("/api/cmd/set_bpm", json={"bpm": 128}).status_code == 403
    assert client.post("/api/cmd/set_bpm", json={"bpm": 128}, headers={"X-PHOS": "1"}).status_code == 200


def test_validation(client, core):
    login(client)
    h = {"X-PHOS": "1"}
    assert client.post("/api/cmd/set_bpm", json={"bpm": 1000}, headers=h).status_code == 422
    assert client.post("/api/cmd/set_bpm", json={"bpm": 120, "evil": 1}, headers=h).status_code == 422
    assert client.post("/api/cmd/scene", json={"scene": "rm -rf"}, headers=h).status_code == 422
    assert client.post("/api/cmd/restart", json={"target": "pi-01", "what": "shell"}, headers=h).status_code == 422
    assert client.post("/api/cmd/nonexistent", json={}, headers=h).status_code == 404
    bad = {"patch": {"scenes": {"playlist": []}}}
    assert client.post("/api/cmd/set_shared", json=bad, headers=h).status_code == 409
    assert client.post("/api/cmd/set_shared", json={"patch": {"calibration": {"offsets_ms": {"pi-01": 3}}}},
                       headers=h).status_code == 409


def test_commands_change_cluster_state(client, core):
    login(client)
    h = {"X-PHOS": "1"}
    client.post("/api/cmd/set_bpm", json={"bpm": 133.5}, headers=h)
    assert core.node.timeline.bpm == 133.5 and core.node.timeline.source.value == "MANUAL"
    r = client.post("/api/cmd/scene", json={"scene": "rain", "quantize": "beat"}, headers=h).json()
    assert r["start_beat"] == int(r["start_beat"])
    st = client.get("/api/status").json()
    assert st["schedule"]["overrides"][-1]["scene"] == "rain"
    client.post("/api/cmd/set_shared", json={"patch": {"safety": {"reduced_flashing": True}}}, headers=h)
    assert core.node.shared.safety.reduced_flashing


def test_calibration_flow_over_api(client, core):
    login(client)
    h = {"X-PHOS": "1"}
    s = client.post("/api/cmd/cal_start", json={"mode": "single", "target": "pi-01", "bpm": 120}, headers=h).json()
    origin = s["origin"]
    # taps that lag the scheduled flash by 30 ms, with jitter, plus a key repeat and a double press
    import random
    rnd = random.Random(1)
    for k in range(20):
        t = origin + k * 0.5 + 0.030 + rnd.gauss(0, 0.008)
        client.post("/api/cmd/cal_tap", json={"cluster": t}, headers=h)
        if k == 5:
            client.post("/api/cmd/cal_tap", json={"cluster": t + 0.02, "value": 2}, headers=h)  # repeat
            client.post("/api/cmd/cal_tap", json={"cluster": t + 0.05}, headers=h)              # double press
    res = core.node.cal.result
    assert res["n_taps"] == 20
    assert res["mean_ms"] == pytest.approx(30, abs=6)
    assert res["suggested_offset_ms"] == pytest.approx(-30, abs=6)
    client.post("/api/cmd/cal_adjust", json={"apply_suggestion": True}, headers=h)
    client.post("/api/cmd/cal_adjust", json={"delta_ms": -1}, headers=h)
    client.post("/api/cmd/cal_save", headers=h)
    saved = core.node.shared.calibration.offsets_ms["pi-01"]
    assert saved == pytest.approx(-31, abs=6)
    assert core.node.visual_offset_ms() == saved
    logs = client.get("/api/logs").json()
    assert logs["calibrations"][0]["node"] == "pi-01"
    client.post("/api/cmd/cal_reset", json={"target": "pi-01"}, headers=h)
    assert "pi-01" not in core.node.shared.calibration.offsets_ms


def test_export_import_roundtrip(client, core):
    login(client)
    exp = client.get("/api/config/export").json()
    exp["shared"]["scenes"]["countdown_label"] = "PARTY"
    exp["shared"]["calibration"]["offsets_ms"] = {"pi-02": -12.5}
    r = client.post("/api/config/import", content=json.dumps(exp), headers={"X-PHOS": "1"})
    assert r.status_code == 200
    assert core.node.shared.scenes.countdown_label == "PARTY"
    assert core.node.shared.calibration.offsets_ms == {"pi-02": -12.5}
    bad = client.post("/api/config/import", content=b'{"shared": {"scenes": {"intensity": 9}}}', headers={"X-PHOS": "1"})
    assert bad.status_code == 422


def test_render_ws_and_sync(client, core):
    with client.websocket_connect("/ws/render") as ws:
        st = json.loads(ws.receive_text())
        assert st["t"] == "render_state" and st["segments"]
        ws.send_text(json.dumps({"t": "sync", "c": 1234.5}))
        for _ in range(10):
            m = json.loads(ws.receive_text())
            if m["t"] == "sync":
                break
        assert m["c"] == 1234.5 and abs(m["s"] - core.node.cluster_now(time.monotonic())) < 1.0
        ws.send_text(json.dumps({"t": "report", "fps": 25.0, "scene": "boot"}))
        time.sleep(0.2)
        json.loads(ws.receive_text())
    assert core.node.renderer.get("fps") == 25.0


def test_admin_ws_requires_login(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/admin") as ws:
            ws.receive_text()
    login(client)
    with client.websocket_connect("/ws/admin") as ws:
        assert json.loads(ws.receive_text())["t"] == "status"


def test_keyboard_calibration_mode(core):
    n = core.node
    now = time.monotonic()
    core.on_key(4, 1, now, now)  # key "3" -> calibrate pi-03 (selection works even if pi-03 is offline)
    assert n.cal.active and n.cal.target == "pi-03"
    core.on_key(103, 1, now, now)   # up: +5 ms
    core.on_key(106, 1, now, now)   # right: +1
    core.on_key(106, 2, now, now)   # autorepeat ignored
    assert n.cal.trial_offset_ms == 6
    origin = n.cal.origin
    for k in range(18):
        ev_local = n.sync.to_local(origin + k * 0.5 - 0.010)
        core.on_key(28, 1, ev_local, time.monotonic())
        core.on_key(28, 0, ev_local + 0.08, time.monotonic())  # release ignored
    assert len(n.cal.taps) == 18
    assert n.cal.result["mean_ms"] == pytest.approx(-10, abs=1)
    core.on_key(31, 1, now, now)  # S = save
    assert n.shared.calibration.offsets_ms["pi-03"] == 6
    core.on_key(1, 1, now, now)   # ESC
    assert not n.cal.active
