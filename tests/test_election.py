import random

from backend.cluster.election import Election, LeaderClaim, NodeState
from backend.config import NetworkConfig
from backend.network.base import EventKind, NetEvent, NullAdapter, RadioMode, RadioStatus, ScanEntry


class FakeAdapter(NullAdapter):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.mode = RadioMode.IDLE

    def request_scan(self):
        self.calls.append("scan")

    def start_hotspot(self):
        self.calls.append("ap")

    def connect(self, bssid=None):
        self.calls.append(("sta", bssid))

    def stop(self):
        self.calls.append("stop")

    def status(self):
        return RadioStatus(self.mode, "AA:AA" if self.mode == RadioMode.AP else None)


TABLE = {"pi-01": 100, "pi-02": 90, "pi-03": 80, "pi-04": 70, "pi-05": 60}


def mk(nid="pi-03", **kw):
    ad = FakeAdapter()
    sent = []
    e = Election(nid, TABLE[nid], NetworkConfig(**kw), ad, lambda m, to: sent.append((m, to)),
                 rng=random.Random(1), priority_table=TABLE)
    return e, ad, sent


def test_claim_total_order():
    a = LeaderClaim(3, 90, "pi-02")
    b = LeaderClaim(4, 100, "pi-01")
    assert a.beats(b) and not b.beats(a)          # older epoch wins, priority irrelevant
    c = LeaderClaim(3, 100, "pi-01")
    assert c.beats(a)                              # same epoch: higher priority
    d = LeaderClaim(3, 100, "pi-00")
    assert d.beats(c)                              # then lower id
    claims = [a, b, c, d, LeaderClaim(1, 10, "pi-09")]
    winners = {x for x in claims if all(x.beats(y) or x == y for y in claims)}
    assert len(winners) == 1


def test_rank_uses_table_then_roster():
    e, _, _ = mk("pi-03")
    assert e._rank(0) == 2
    e.failed_leader = "pi-01"
    assert e._rank(0) == 1
    # roster knowledge overrides table: only pi-05 alive besides us
    e._on_beacon({"id": "pi-05", "prio": 60, "state": "FOLLOWER"}, 0)
    assert e._rank(1) == 0


def test_boot_discovery_backoff_hotspot_leader():
    e, ad, _ = mk("pi-02", discovery_window=5, backoff_slot=4, backoff_jitter=0)
    e.tick(0)
    assert e.state == NodeState.DISCOVERING and "scan" in ad.calls
    e.handle_net_event(NetEvent(EventKind.SCAN_RESULT, []), 3)
    e.tick(5.1)
    assert e.state == NodeState.ELECTION_BACKOFF
    assert e.deadline - 5.1 == 8.0  # (rank 1 + 1 slot reserved for a possible audio node) * backoff_slot 4
    e.scan_pending = False
    e.tick(13.2)
    assert e.state == NodeState.ELECTION_BACKOFF and "ap" not in ad.calls  # pre-flight scan first
    e.handle_net_event(NetEvent(EventKind.SCAN_RESULT, []), 16.0)
    e.tick(16.1)
    assert e.state == NodeState.STARTING_HOTSPOT and "ap" in ad.calls
    ad.mode = RadioMode.AP
    e.handle_net_event(NetEvent(EventKind.HOTSPOT_UP, {"bssid": "AA:AA"}), 22)
    assert e.state == NodeState.LEADER and e.my_claim.epoch == 1
    states = [t.new for t in e.transitions]
    assert states == ["DISCOVERING", "ELECTION_BACKOFF", "STARTING_HOTSPOT", "LEADER"]


def test_join_existing_hotspot_and_no_preemption():
    e, ad, _ = mk("pi-01")  # highest priority node returns
    e.tick(0)
    e.handle_net_event(NetEvent(EventKind.SCAN_RESULT, [ScanEntry("PHOSPHOR2000", "BB:BB", 60)]), 2)
    assert e.state == NodeState.RECONNECTING and ("sta", "BB:BB") in ad.calls
    e.handle_net_event(NetEvent(EventKind.CONNECTED, {"bssid": "BB:BB"}), 5)
    e.on_message({"t": "beacon", "_from": "pi-02", "id": "pi-02", "prio": 90, "state": "LEADER",
                  "claim": {"epoch": 7, "priority": 90, "node_id": "pi-02"}, "bssid": "BB:BB"}, 5.5)
    assert e.state == NodeState.RECOVERING
    e.recovery_complete(6)
    assert e.state == NodeState.FOLLOWER and e.current_leader_id() == "pi-02"
    assert e.epoch_seen == 7


def test_follower_suspects_then_discovers():
    e, ad, _ = mk("pi-03", suspect_after=3, failure_after=8)
    e.state = NodeState.FOLLOWER
    e.leader = LeaderClaim(1, 100, "pi-01")
    e.leader_seen = 0
    e.tick(3.5)
    assert e.state == NodeState.SUSPECTED_FAILURE
    e.on_message({"t": "beacon", "_from": "pi-01", "id": "pi-01", "prio": 100, "state": "LEADER",
                  "claim": {"epoch": 1, "priority": 100, "node_id": "pi-01"}}, 4)
    assert e.state == NodeState.FOLLOWER
    e.tick(12.1)
    assert e.state == NodeState.SUSPECTED_FAILURE
    e.tick(12.2 + 8)
    assert e.state == NodeState.DISCOVERING and e.failed_leader == "pi-01"


def test_leader_yields_to_older_claim_with_holddown():
    e, ad, sent = mk("pi-01", yield_holddown=30)
    e.state = NodeState.LEADER
    ad.mode = RadioMode.AP
    e.my_claim = LeaderClaim(5, 100, "pi-01")
    e.on_message({"t": "leader_claim", "_from": "pi-04", "claim": {"epoch": 4, "priority": 70, "node_id": "pi-04"},
                  "bssid": "CC:CC"}, 100)
    assert any(m["t"] == "yield" for m, _ in sent)
    e.tick(100 + e.YIELD_GRACE + 0.01)
    assert e.state == NodeState.RECONNECTING and ("sta", "CC:CC") in ad.calls
    assert e.holddown_until == 100 + e.YIELD_GRACE + 0.01 + 30
    # a newer claim never makes a leader yield
    e2, _, sent2 = mk("pi-02")
    e2.state = NodeState.LEADER
    e2.my_claim = LeaderClaim(2, 90, "pi-02")
    e2.on_message({"t": "leader_claim", "_from": "pi-01", "claim": {"epoch": 3, "priority": 100, "node_id": "pi-01"}}, 1)
    assert not sent2


def test_repeated_yields_grow_holddown():
    e, ad, _ = mk("pi-02", yield_holddown=30)
    holds = []
    for i in range(4):
        e.state = NodeState.LEADER
        e.my_claim = LeaderClaim(9, 90, "pi-02")
        e.pending_yield = None
        e._yield_to(LeaderClaim(1, 50, "pi-05"), "DD", i * 1000.0, "test")
        e.tick(i * 1000.0 + 2)
        holds.append(e.holddown_until - (i * 1000.0 + 2))
    assert holds == [30, 60, 120, 240]


def test_follower_reports_foreign_ap_and_leader_orders_scout():
    f, fad, fsent = mk("pi-05")
    f.state = NodeState.FOLLOWER
    f.leader = LeaderClaim(1, 100, "pi-01")
    f.leader_bssid = "AA:AA"
    f.leader_seen = 0
    f.handle_net_event(NetEvent(EventKind.SCAN_RESULT, [ScanEntry("PHOSPHOR2000", "AA:AA", 80),
                                                        ScanEntry("PHOSPHOR2000", "EE:EE", 40)]), 1)
    assert fsent[-1] == ({"t": "foreign_ap", "bssids": ["EE:EE"]}, "pi-01")
    l, lad, lsent = mk("pi-01")
    l.state = NodeState.LEADER
    lad.mode = RadioMode.AP
    l.my_claim = LeaderClaim(1, 100, "pi-01")
    l._on_beacon({"id": "pi-05", "prio": 60, "state": "FOLLOWER", "leader_id": "pi-01"}, 1)
    l._on_beacon({"id": "pi-04", "prio": 70, "state": "FOLLOWER", "leader_id": "pi-01"}, 1)
    l.on_message({"t": "foreign_ap", "_from": "pi-05", "bssids": ["EE:EE"]}, 1.5)
    order = [m for m, to in lsent if m["t"] == "scout_order"]
    assert order and order[0]["to"] == "pi-05"  # least important CRT scouts


def test_core_restart_resumes_leader_and_follower_without_network_drop():
    e, ad, _ = mk("pi-01")
    e.epoch_seen = 7
    ad.mode = RadioMode.AP
    e.tick(0)
    assert e.state == NodeState.LEADER and e.my_claim.epoch == 7 and "ap" not in ad.calls and "stop" not in ad.calls
    f, fad, _ = mk("pi-03")
    fad.mode = RadioMode.STATION
    f.tick(0)
    assert f.state == NodeState.RECONNECTING and not any(isinstance(c, tuple) for c in fad.calls)
    f.on_message({"t": "beacon", "_from": "pi-01", "id": "pi-01", "prio": 100, "state": "LEADER",
                  "claim": {"epoch": 7, "priority": 100, "node_id": "pi-01"}}, 1)
    assert f.state == NodeState.RECOVERING
