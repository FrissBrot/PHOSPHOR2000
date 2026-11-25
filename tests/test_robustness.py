"""Hang detection, duplicate node ids, sd_notify, SD-friendly logging, version reporting."""
import os
import socket
import time

import pytest

from backend.config import load_config
from backend.network.base import ExternalNetworkAdapter
from backend.services import core as core_mod
from backend.services.core import Core
from backend.services.sdnotify import notify


@pytest.fixture()
def core(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOSPHOR_NO_KIOSK", "0")
    cfg = load_config(node_path=tmp_path / "n.yaml", secrets_path=tmp_path / "s.yaml",
                      overrides={"node": {"id": "pi-02"}})
    c = Core(cfg, adapter=ExternalNetworkAdapter(), state_dir=tmp_path, has_audio=False)
    return c


def test_sd_notify(tmp_path, monkeypatch):
    path = str(tmp_path / "notify.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(path)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    assert notify("WATCHDOG=1")
    assert s.recv(100) == b"WATCHDOG=1"
    monkeypatch.delenv("NOTIFY_SOCKET")
    assert not notify("READY=1")


def test_renderer_watchdog_restarts_hung_kiosk_with_backoff(core, monkeypatch):
    calls = []
    monkeypatch.setattr(core_mod, "run_action", lambda what: calls.append(what) or True)
    core.kiosk = True
    t0 = core.started
    core.renderer_watchdog(t0 + 100)
    assert calls == []                                  # grace period after start
    core.node.renderer = {"fps": 25, "_rx": t0 + 200}
    core.renderer_watchdog(t0 + 201)
    assert calls == []                                  # healthy
    core.renderer_watchdog(t0 + 201 + 46)               # silent > 45 s
    assert calls == ["renderer"]
    core.renderer_watchdog(t0 + 201 + 46 + 60)          # still silent, but backoff (120 s)
    assert calls == ["renderer"]
    core.renderer_watchdog(t0 + 201 + 46 + 300)
    assert calls == ["renderer", "renderer"]


def test_renderer_watchdog_detects_frozen_page(core, monkeypatch):
    calls = []
    monkeypatch.setattr(core_mod, "run_action", lambda what: calls.append(what) or True)
    core.kiosk = True
    t = core.started + 300
    core.node.renderer = {"fps": 25, "_rx": t}
    core.renderer_watchdog(t)
    for i in range(1, 40):                              # page reports, but draws nothing
        core.node.renderer = {"fps": 0.0, "_rx": t + i}
        core.renderer_watchdog(t + i)
    assert calls == ["renderer"]


def test_duplicate_node_id_is_detected(core):
    n = core.node
    now = time.monotonic()
    n.on_message({"t": "beacon", "_from": "pi-02", "_boot": "other", "id": "pi-02", "prio": 90,
                  "state": "LEADER", "claim": {"epoch": 3, "priority": 90, "node_id": "pi-02"}}, now, "192.168.50.77")
    assert n.alerts(now) and "DUPLICATE NODE ID" in n.alerts(now)[0]
    assert "pi-02" not in n.election.peers           # never treated as a peer/leader
    assert n.render_state(now)["alerts"]
    assert n.alerts(now + 120) == []                  # clears when the other device is gone


def test_event_log_is_ram_only_but_calibrations_persist(tmp_path):
    from backend.storage.cache import EventLog
    log = EventLog(tmp_path / "cal.sqlite3")
    for i in range(50):
        log.add(1.0 + i, "pi-01", "info", f"msg {i}")
    log.add_calibration(5.0, "pi-03", -12.0, {"n_used": 20})
    assert len(log.recent(10)) == 10
    del log
    log2 = EventLog(tmp_path / "cal.sqlite3")
    assert log2.recent(10) == []                      # events were RAM only
    assert log2.calibrations()[0]["offset_ms"] == -12.0


def test_version_is_reported(core):
    st = core.node.status(time.monotonic())
    assert st["version"] and st["version"] != ""
    b = core.node._beacon(time.monotonic(), 0.0)
    assert b["ver"] == st["version"]
