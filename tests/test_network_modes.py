"""Network modes (hotspot / existing Wi-Fi / Ethernet switch), internet sharing, package updates."""
import json
import random
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.auth import hash_password
from backend.cluster.messages import CodecError, seal, unseal
from backend.cluster.node import ClusterNode, CommandError
from backend.config import NetworkConfig, load_config
from backend.network.base import ExternalNetworkAdapter
from backend.network.links import Interfaces, parse_ip_json
from backend.network.nmcli import NmcliAdapter, write_mode_keyfiles
from backend.services.core import Core

IP_JSON = json.dumps([
    {"ifname": "lo", "flags": ["LOOPBACK", "UP"], "addr_info": [{"family": "inet", "local": "127.0.0.1"}]},
    {"ifname": "eth0", "flags": ["BROADCAST", "UP"], "addr_info": [
        {"family": "inet", "local": "192.168.1.40", "broadcast": "192.168.1.255"},
        {"family": "inet", "local": "10.20.0.13", "broadcast": "10.20.0.255"}]},
    {"ifname": "wlan0", "flags": ["BROADCAST", "UP"], "addr_info": [
        {"family": "inet", "local": "192.168.50.1", "broadcast": "192.168.50.255"}]},
])


def test_seal_roundtrip_and_tamper():
    tok = seal("k" * 32, "venue-wifi-pass")
    assert "venue" not in tok and unseal("k" * 32, tok) == "venue-wifi-pass"
    with pytest.raises(CodecError):
        unseal("other-key", tok)
    assert seal("k" * 32, "x" * 10) != seal("k" * 32, "x" * 10)   # random nonce


def test_keyfiles_per_mode(tmp_path):
    cfg = NetworkConfig(mode="ethernet")
    names = {p.name for p in write_mode_keyfiles(cfg, "pi-04", "clusterpass1", "", tmp_path)}
    assert names == {"phosphor-ap.nmconnection", "phosphor-sta.nmconnection", "phosphor-eth.nmconnection"}
    eth = (tmp_path / "phosphor-eth.nmconnection").read_text()
    assert "autoconnect=true" in eth and "address1=10.20.0.14/24" in eth and "method=auto" in eth
    cfg = NetworkConfig(mode="wlan", wlan_ssid="Mattliball Crew")
    write_mode_keyfiles(cfg, "pi-02", "clusterpass1", "venuepass9", tmp_path)
    wl = (tmp_path / "phosphor-wlan.nmconnection").read_text()
    assert "ssid=Mattliball Crew" in wl and "psk=venuepass9" in wl and "autoconnect=true" in wl
    assert "autoconnect=false" in (tmp_path / "phosphor-eth.nmconnection").read_text()
    for m in ((tmp_path / f).stat().st_mode & 0o777 for f in ("phosphor-wlan.nmconnection", "phosphor-eth.nmconnection")):
        assert m == 0o600
    with pytest.raises(ValueError):
        write_mode_keyfiles(NetworkConfig(mode="wlan"), "pi-02", "clusterpass1", "", tmp_path)
    with pytest.raises(Exception):
        NetworkConfig(wlan_ssid="evil\nssid=x")


def test_broadcasts_stay_on_the_cluster_interface():
    assert {x[0] for x in parse_ip_json(IP_JSON)} == {"eth0", "wlan0"}
    hot = Interfaces("192.168.50.255", runner=lambda: IP_JSON, only_iface=None)
    hot.refresh(0)
    assert hot.broadcasts() == ["192.168.50.255"]           # never on the home LAN cable
    eth = Interfaces("255.255.255.255", runner=lambda: IP_JSON, only_iface="eth0")
    eth.refresh(0)
    assert eth.broadcasts() == ["10.20.0.255", "192.168.1.255"]
    assert eth.address_of("eth0") == "192.168.1.40"         # DHCP address preferred for mDNS


def test_uplink_toggle_uses_fixed_nmcli_argv(tmp_path):
    calls = []
    ad = NmcliAdapter(NetworkConfig(), runner=lambda argv, t: (calls.append(list(argv)) or (0, "", "")),
                      monitor_interval=999, sysfs=tmp_path)
    try:
        ad._op_uplink(True)
        ad._op_uplink(False)
    finally:
        ad.close()
    mods = [c for c in calls if "modify" in c]
    assert mods[0][-4:] == ["ipv4.never-default", "no", "ipv4.route-metric", "700"]
    assert mods[1][-4:] == ["ipv4.never-default", "yes", "ipv4.route-metric", "700"]


def test_existing_network_election_is_fast_and_survives_leader_loss():
    """wlan/ethernet mode: no hotspot to negotiate; leaders resolve over the shared L2 network."""
    cfg0 = load_config(overrides={})
    nodes, alive = {}, {}

    def bus(src):
        def send(m, to):
            from backend.cluster.messages import Codec, derive_key
            for nid, n in nodes.items():
                if nid != src and alive[nid] and (to is None or to == nid):
                    n.on_message({**m, "_from": src}, now[0])
        return send

    now = [0.0]
    for nid in ("pi-01", "pi-02", "pi-03"):
        cfg = cfg0.model_copy(deep=True)
        cfg.node.id = nid
        cfg.node.priority = cfg.node.priority_table[nid]
        nodes[nid] = ClusterNode(cfg, ExternalNetworkAdapter(), bus(nid), rng=random.Random(nid))
        alive[nid] = True
    while now[0] < 120:
        for nid, n in nodes.items():
            if alive[nid]:
                n.tick(now[0])
        now[0] += 0.05
        leaders = [k for k, n in nodes.items() if alive[k] and n.is_leader()]
        if leaders == ["pi-01"] and all(nodes[k].state.value in ("LEADER", "FOLLOWER") for k in nodes):
            break
    assert leaders == ["pi-01"] and now[0] < 60
    alive["pi-01"] = False
    t0 = now[0]
    while now[0] - t0 < 120:
        for nid, n in nodes.items():
            if alive[nid]:
                n.tick(now[0])
        now[0] += 0.05
        if [k for k, n in nodes.items() if alive[k] and n.is_leader()] == ["pi-02"] and \
                nodes["pi-03"].state.value == "FOLLOWER" and nodes["pi-03"].election.current_leader_id() == "pi-02":
            break
    assert now[0] - t0 < 35          # no Wi-Fi switching: detection + backoff only


# ------------------------------------------------------------------ core-level

@pytest.fixture()
def core(tmp_path):
    cfg = load_config(node_path=tmp_path / "n.yaml", secrets_path=tmp_path / "s.yaml",
                      overrides={"node": {"id": "pi-01"},
                                 "network": {"discovery_window": 0.2, "backoff_jitter": 0, "backoff_slot": 0.2},
                                 "secrets": {"cluster_key": "k" * 32, "admin_password_hash": hash_password("pw123456")}})
    c = Core(cfg, adapter=ExternalNetworkAdapter(), state_dir=tmp_path, has_audio=False)
    c.netmode.run_dir = c.packages.run_dir = c.updater.run_dir = tmp_path / "run"
    deadline = time.time() + 5
    while not c.node.is_leader() and time.time() < deadline:
        c.node.tick(time.monotonic()); time.sleep(0.05)
    return c


def test_network_mode_change_is_sealed_delayed_and_applied(core):
    client = TestClient(create_app(core))
    client.post("/api/login", json={"password": "pw123456"})
    h = {"X-PHOS": "1"}
    assert client.post("/api/network/mode", json={"mode": "wlan"}, headers=h).status_code == 409   # SSID missing
    r = client.post("/api/network/mode", json={"mode": "wlan", "wlan_ssid": "Venue", "wlan_psk": "venuepass1"}, headers=h)
    assert r.status_code == 200
    site = core.node.shared.site
    assert site.net_mode == "wlan" and "venuepass1" not in site.net_wlan_psk_sealed
    assert not any("venuepass1" in ln["msg"] for ln in core.node.log_lines)     # never logged
    nm = core.netmode
    nm.start_unit = lambda: True
    t = time.monotonic()
    nm.tick(t)
    assert nm.requests == []                      # waits 15 s so every Pi has the request
    nm.tick(t + 16)
    req = json.loads((nm.run_dir / "netmode-request.json").read_text())
    assert req["mode"] == "wlan" and req["wlan_ssid"] == "Venue" and req["wlan_psk"] == "venuepass1"
    assert oct((nm.run_dir / "netmode-request.json").stat().st_mode & 0o777) == "0o600"
    nm.tick(t + 40)
    assert len(nm.requests) == 1                  # applied once
    # generic config patch cannot touch the network fields
    assert client.post("/api/cmd/set_shared", json={"patch": {"site": {"net_mode": "ethernet"}}}, headers=h).status_code == 409


def test_trial_falls_back_and_leader_cancels(core, tmp_path):
    nm = core.netmode
    nm.start_unit = lambda: True
    (tmp_path / "netmode-trial.json").write_text(json.dumps({
        "rev": 7, "deadline": time.time() - 1, "target": "wlan", "peers_before": 2,
        "previous": {"mode": "hotspot", "wlan_ssid": "", "wlan_psk": ""}}))
    core.ifaces.addrs = []
    nm.tick(time.monotonic())
    req = json.loads((nm.run_dir / "netmode-request.json").read_text())
    assert req["fallback"] and req["mode"] == "hotspot" and nm.failed_rev == 7
    # the leader stops a request that failed on a Pi (no switching loop)
    site = core.node.shared.site.model_copy(update={"net_mode": "wlan", "net_rev": 7, "net_wlan_ssid": "Venue"})
    core.node.shared = core.node.shared.model_copy(update={"site": site})
    nm.tick(time.monotonic())
    assert core.node.shared.site.net_mode is None


def test_trial_succeeds_when_cluster_found(core, tmp_path):
    nm = core.netmode
    (tmp_path / "netmode-trial.json").write_text(json.dumps({
        "rev": 3, "deadline": time.time() + 100, "target": "ethernet", "peers_before": 2, "previous": {}}))
    core.node.election._on_beacon({"id": "pi-02", "prio": 90, "state": "FOLLOWER"}, time.monotonic())
    nm.tick(time.monotonic())
    assert not (tmp_path / "netmode-trial.json").exists() and nm.failed_rev == 0


def test_package_rollout_order_and_live_music_guard(core):
    from backend.sync.timeline import TimingSource
    pk = core.packages
    runs = []
    pk.start_unit = lambda: runs.append(1)
    sent = []
    core.node.send = lambda m, to: sent.append((m, to))
    now = time.monotonic()
    for nid, prio in (("pi-02", 90), ("pi-03", 80)):
        core.node.election._on_beacon({"id": nid, "prio": prio, "state": "FOLLOWER", "leader_id": "pi-01"}, now)
    core.node.timeline = replace(core.node.timeline, source=TimingSource.LIVE)
    core.updater.tick(now)
    with pytest.raises(RuntimeError):
        pk.start_all(now)                                     # music playing
    r = pk.start_all(now, force=True)
    assert r["queue"] == ["pi-03", "pi-02", "pi-01"]          # least important first, leader last
    pk.tick(now)
    assert sent[-1] == ({"t": "apt_cmd", "to": "pi-03"}, "pi-03")
    core.node.peer_status["pi-03"] = {"apt": {"state": "done", "at": time.time() + 1}, "_rx": now}
    pk.tick(now + 1)
    assert sent[-1][1] == "pi-02"
    core.node.peer_status["pi-02"] = {"apt": {"state": "error", "at": time.time() + 1}, "_rx": now}
    pk.tick(now + 2)
    assert runs == [1]                                        # leader itself last
    (pk.run_dir).mkdir(parents=True, exist_ok=True)
    (pk.run_dir / "apt-status.json").write_text(json.dumps({"state": "done", "at": time.time() + 1}))
    pk.tick(now + 3)
    pk.tick(now + 4)
    assert r["state"] == "done" and r["failed"] == ["pi-02"] and set(r["done"]) == {"pi-03", "pi-01"}
