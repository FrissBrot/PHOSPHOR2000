"""Leader election / hotspot ownership state machine.

This is deliberately *not* a consensus protocol. A Pi 3 has one radio; when
the hotspot owner dies, every follower loses the network and nobody can talk
to anybody until a replacement AP is up. Decisions are therefore made locally
from (a) cached knowledge of the cluster roster and (b) Wi-Fi scan results,
using staggered backoff by priority rank. Competing hotspots (split brain)
are detected via scans/beacons and reconciled deterministically by the total
order on ``LeaderClaim`` with a "scout" exchange (see docs/network-recovery.md).

Rules:
* lower epoch wins (the longer-established leader is never preempted by a
  newcomer); ties: higher priority, then lower node id.
* a node that boots or returns always *joins* an existing hotspot first.
* a node that yielded enters an exponentially growing hold-down before it may
  start a hotspot again (prevents AP flapping under asymmetric range).
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from backend.config import NetworkConfig
from backend.network.base import EventKind, NetEvent, NetworkAdapter, RadioMode

log = logging.getLogger("cs2k.election")


class NodeState(str, Enum):
    BOOTING = "BOOTING"
    DISCOVERING = "DISCOVERING"
    FOLLOWER = "FOLLOWER"
    LEADER = "LEADER"
    SUSPECTED_FAILURE = "SUSPECTED_FAILURE"
    ELECTION_BACKOFF = "ELECTION_BACKOFF"
    STARTING_HOTSPOT = "STARTING_HOTSPOT"
    RECONNECTING = "RECONNECTING"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"


@dataclass(frozen=True)
class LeaderClaim:
    epoch: int
    priority: int
    node_id: str

    def beats(self, other: "LeaderClaim") -> bool:
        """Deterministic total order: True if self should remain leader over other."""
        return (self.epoch, -self.priority, self.node_id) < (other.epoch, -other.priority, other.node_id)

    def to_dict(self) -> dict:
        return {"epoch": self.epoch, "priority": self.priority, "node_id": self.node_id}

    @classmethod
    def from_dict(cls, d: dict) -> "LeaderClaim":
        return cls(int(d["epoch"]), int(d["priority"]), str(d["node_id"]))


@dataclass
class Peer:
    id: str
    priority: int
    state: str
    last_seen: float
    leader_id: str | None = None
    epoch: int = 0
    bssid: str | None = None
    info: dict = field(default_factory=dict)


@dataclass
class Transition:
    at: float
    old: str
    new: str
    reason: str


Send = Callable[[dict, str | None], None]


class Election:
    FOREIGN_SCAN_INTERVAL = 45.0
    LONE_LEADER_RESCAN = 90.0
    SCOUT_COOLDOWN = 60.0
    SCOUT_TIMEOUT = 30.0
    ROSTER_TTL = 90.0
    BEACON_WAIT = 6.0
    RECOVER_TIMEOUT = 4.0
    YIELD_GRACE = 1.5
    HANDOVER_EXPECT = 40.0

    def __init__(self, node_id: str, priority: int, cfg: NetworkConfig, adapter: NetworkAdapter,
                 send: Send, rng: random.Random | None = None, priority_table: dict[str, int] | None = None,
                 epoch_seen: int = 0, on_transition: Callable[[Transition], None] | None = None,
                 can_lead: bool = True) -> None:
        self.id = node_id
        self.priority = priority
        self.cfg = cfg
        self.adapter = adapter
        self.send = send
        self.rng = rng or random.Random()
        self.priority_table = dict(priority_table or {})
        self.epoch_seen = epoch_seen
        self.on_transition = on_transition
        self.can_lead = can_lead

        self.state = NodeState.BOOTING
        self.state_since = 0.0
        self.deadline: float | None = None
        self.transitions: list[Transition] = []
        self.peers: dict[str, Peer] = {}
        self.leader: LeaderClaim | None = None
        self.leader_bssid: str | None = None
        self.leader_seen = 0.0
        self.failed_leader: str | None = None
        self.my_claim: LeaderClaim | None = None
        self.leader_since = 0.0
        self.holddown_until = 0.0
        self.yield_count = 0
        self.bad_bssids: dict[str, float] = {}
        self.scan_pending = False
        self.net_pending = False
        self.hotspot_failures = 0
        self.connect_failures = 0
        self.next_foreign_scan = 0.0
        self.next_lone_rescan = 0.0
        self.foreign: dict[str, float] = {}       # foreign BSSID -> last reported
        self.scout_sent: dict[str, float] = {}    # foreign BSSID -> when we ordered a scout
        self.scout: dict | None = None            # active scout task (follower side)
        self.pending_yield: dict | None = None
        self.expected_leader: tuple[str, float] | None = None
        self.discovery_started = 0.0
        self.boot = True
        self.conflicts_resolved = 0

    # ------------------------------------------------------------------ util
    def _go(self, new: NodeState, now: float, reason: str, deadline: float | None = None) -> None:
        if new != self.state:
            tr = Transition(now, self.state.value, new.value, reason)
            self.transitions.append(tr)
            self.transitions = self.transitions[-200:]
            log.info("%s: %s -> %s (%s)", self.id, self.state.value, new.value, reason)
            if self.on_transition:
                self.on_transition(tr)
        self.state = new
        self.state_since = now
        self.deadline = deadline

    def is_leader(self) -> bool:
        return self.state == NodeState.LEADER

    def connected(self) -> bool:
        return self.state in (NodeState.LEADER, NodeState.FOLLOWER, NodeState.SUSPECTED_FAILURE, NodeState.RECOVERING)

    def current_leader_id(self) -> str | None:
        if self.state == NodeState.LEADER:
            return self.id
        return self.leader.node_id if self.leader else None

    def followers(self, now: float) -> list[str]:
        return [p.id for p in self.peers.values()
                if p.leader_id == self.id and now - p.last_seen < max(5.0, 3 * self.cfg.beacon_interval)]

    def _rank(self, now: float) -> int:
        cands: dict[str, int] = {}
        for p in self.peers.values():
            if now - p.last_seen > self.ROSTER_TTL or p.id == self.failed_leader:
                continue
            if p.state in (NodeState.DEGRADED.value,) or p.info.get("can_lead") is False:
                continue
            cands[p.id] = p.priority
        if not cands:  # no roster knowledge (fresh boot): use the configured table
            cands = {k: v for k, v in self.priority_table.items() if k != self.failed_leader}
        cands.pop(self.id, None)
        return sum(1 for k, v in cands.items() if v > self.priority or (v == self.priority and k < self.id))

    def _backoff(self, now: float) -> float:
        rank = self._rank(now)
        extra = 0.0
        if self.expected_leader and self.expected_leader[0] != self.id and now < self.expected_leader[1]:
            extra = self.expected_leader[1] - now
        return rank * self.cfg.backoff_slot + self.rng.uniform(0, self.cfg.backoff_jitter) + extra

    def _scan(self) -> None:
        if not self.scan_pending and not self.net_pending:
            self.scan_pending = True
            self.adapter.request_scan()

    # ---------------------------------------------------------------- beacon
    def beacon_fields(self, now: float) -> dict:
        return {
            "id": self.id, "prio": self.priority, "state": self.state.value,
            "leader_id": self.current_leader_id(),
            "claim": self.my_claim.to_dict() if self.is_leader() and self.my_claim else None,
            "leader_claim": self.leader.to_dict() if self.leader else None,
            "bssid": self.adapter.status().bssid if self.is_leader() else self.leader_bssid,
            "epoch_seen": self.epoch_seen,
            "followers": len(self.followers(now)) if self.is_leader() else None,
            "can_lead": self.can_lead,
            "mac": self.adapter.status().own_mac,
        }

    # -------------------------------------------------------------- messages
    def on_message(self, msg: dict, now: float) -> None:
        t = msg.get("t")
        sender = msg.get("_from")
        if t == "beacon":
            self._on_beacon(msg, now)
        elif t == "yield" and sender == self.current_leader_id() and not self.is_leader():
            self._follow_yield(msg, now)
        elif t == "handover" and sender == self.current_leader_id():
            self._on_handover(msg, now)
        elif t == "foreign_ap" and self.is_leader():
            self._on_foreign_report(msg, now)
        elif t == "scout_order" and msg.get("to") == self.id and sender == self.current_leader_id():
            self._start_scout(msg, now)
        elif t == "leader_claim":
            self._on_leader_claim(msg, now)

    def _on_beacon(self, b: dict, now: float) -> None:
        pid = b["id"]
        p = self.peers.get(pid) or Peer(pid, int(b.get("prio", 0)), b.get("state", "?"), now)
        p.priority, p.state, p.last_seen = int(b.get("prio", 0)), b.get("state", "?"), now
        p.leader_id, p.bssid = b.get("leader_id"), b.get("bssid")
        p.info = {k: v for k, v in b.items() if not k.startswith("_")}
        self.peers[pid] = p
        self.epoch_seen = max(self.epoch_seen, int(b.get("epoch_seen", 0)))
        claim = LeaderClaim.from_dict(b["claim"]) if b.get("claim") else None
        if claim:
            self.epoch_seen = max(self.epoch_seen, claim.epoch)
        if claim is None:
            return
        if self.state in (NodeState.DISCOVERING, NodeState.ELECTION_BACKOFF):
            # Only possible on an externally managed shared network: follow directly.
            self.leader, self.leader_bssid, self.leader_seen = claim, b.get("bssid"), now
            self._reconnect(now, b.get("bssid"), f"leader {claim.node_id} heard on shared network")
            return
        if self.is_leader():
            # Another leader on *our* L2 segment (should not happen with one SSID, but
            # can with bridged APs / misconfiguration): resolve immediately.
            if self.my_claim and claim.beats(self.my_claim):
                self._yield_to(claim, b.get("bssid"), now, "competing leader beacon on same network")
            return
        if self.scout and self.scout.get("phase") == "foreign":
            self._scout_reached_foreign(claim, b.get("bssid"), now)
            return
        if self.scout and self.scout.get("phase") == "return":
            self._scout_returned(claim, now)
            # fall through: we follow our leader again
        if self.leader and claim.node_id != self.leader.node_id and self.state == NodeState.FOLLOWER:
            if not claim.beats(self.leader):
                return  # stale/losing leader heard: ignore, ours wins
        self.leader = claim
        self.leader_bssid = b.get("bssid") or self.leader_bssid
        self.leader_seen = now
        if self.failed_leader == claim.node_id:
            self.failed_leader = None
        if self.state in (NodeState.RECONNECTING,):
            self._go(NodeState.RECOVERING, now, f"leader {claim.node_id} e{claim.epoch} heard",
                     now + self.RECOVER_TIMEOUT)
        elif self.state == NodeState.SUSPECTED_FAILURE:
            self._go(NodeState.FOLLOWER, now, "leader beacon resumed")

    def recovery_complete(self, now: float) -> None:
        """Called by the node once replicated state has been reconciled."""
        if self.state == NodeState.RECOVERING:
            self._go(NodeState.FOLLOWER, now, "state reconciled")

    def _follow_yield(self, msg: dict, now: float) -> None:
        target = msg.get("winner_bssid")
        self.failed_leader = self.leader.node_id if self.leader else None
        self.leader = None
        self._reconnect(now, target, "leader yielded to " + str(msg.get("winner_id")))

    def _on_handover(self, msg: dict, now: float) -> None:
        target = msg.get("target")
        self.expected_leader = (target, now + self.HANDOVER_EXPECT)
        self.failed_leader = self.leader.node_id if self.leader else None
        if target == self.id and self.can_lead:
            self.adapter.stop()
            self._go(NodeState.STARTING_HOTSPOT, now, "controlled handover to me",
                     now + self.YIELD_GRACE + 1.0)
            self.pending_yield = {"start_at": now + self.YIELD_GRACE + 1.0}
        else:
            self.leader = None
            self._go(NodeState.DISCOVERING, now, f"controlled handover to {target}", now + self.YIELD_GRACE + 4.0)
            self.discovery_started = now
            self.boot = False

    def _on_foreign_report(self, msg: dict, now: float) -> None:
        own = self.adapter.status().bssid
        for bssid in msg.get("bssids", []):
            if bssid and bssid != own:
                self.foreign[bssid] = now
        self._maybe_order_scout(now)

    def _maybe_order_scout(self, now: float) -> None:
        # A "foreign" AP whose owner has meanwhile joined us is no longer a competitor.
        ours = {p.info.get("mac") for p in self.peers.values() if p.leader_id == self.id}
        for b in list(self.foreign):
            if b in ours or now - self.foreign[b] > self.FOREIGN_SCAN_INTERVAL + 20:
                del self.foreign[b]
        live = list(self.foreign)
        for bssid in live:
            if now - self.scout_sent.get(bssid, -1e9) < self.SCOUT_COOLDOWN:
                continue
            fol = [self.peers[f] for f in self.followers(now) if f in self.peers]
            if not fol:
                continue
            scout = min(fol, key=lambda p: (p.priority, p.id))  # least important CRT
            self.scout_sent[bssid] = now
            log.warning("%s: competing hotspot %s detected, sending scout %s", self.id, bssid, scout.id)
            self.send({"t": "scout_order", "to": scout.id, "bssid": bssid,
                       "claim": self.my_claim.to_dict(), "home_bssid": self.adapter.status().bssid}, scout.id)

    def _start_scout(self, msg: dict, now: float) -> None:
        self.scout = {"phase": "foreign", "bssid": msg["bssid"], "home_claim": msg["claim"],
                      "home_bssid": msg.get("home_bssid"), "until": now + self.SCOUT_TIMEOUT}
        self._reconnect(now, msg["bssid"], "scouting competing hotspot")

    def _scout_reached_foreign(self, foreign: LeaderClaim, foreign_bssid: str | None, now: float) -> None:
        assert self.scout is not None
        home = LeaderClaim.from_dict(self.scout["home_claim"])
        # Tell the foreign leader about our home leader (it will yield if home wins).
        self.send({"t": "leader_claim", "claim": home.to_dict(), "bssid": self.scout.get("home_bssid")}, None)
        self.scout.update(phase="return", foreign_claim=foreign.to_dict(), foreign_bssid=foreign_bssid,
                          until=now + self.SCOUT_TIMEOUT)
        if home.beats(foreign):
            self.leader = None
            self._reconnect(now, self.scout.get("home_bssid"), "scout: home wins, returning")
        else:
            # Foreign wins: go home and tell our leader to yield (it would otherwise keep its AP).
            self.leader = None
            self._reconnect(now, self.scout.get("home_bssid"), "scout: foreign wins, informing home")

    def _scout_returned(self, claim: LeaderClaim, now: float) -> None:
        assert self.scout is not None
        fc = self.scout.get("foreign_claim")
        if fc:
            self.send({"t": "leader_claim", "claim": fc, "bssid": self.scout.get("foreign_bssid")}, None)
        self.scout = None

    def _on_leader_claim(self, msg: dict, now: float) -> None:
        claim = LeaderClaim.from_dict(msg["claim"])
        self.epoch_seen = max(self.epoch_seen, claim.epoch)
        if self.is_leader() and self.my_claim and claim.node_id != self.id and claim.beats(self.my_claim):
            self._yield_to(claim, msg.get("bssid"), now, f"split-brain: {claim.node_id} e{claim.epoch} wins")

    def _yield_to(self, winner: LeaderClaim, bssid: str | None, now: float, reason: str) -> None:
        if self.pending_yield:
            return
        self.conflicts_resolved += 1
        msg = {"t": "yield", "winner_id": winner.node_id, "winner_bssid": bssid, "claim": winner.to_dict()}
        for _ in range(3):
            self.send(msg, None)
        self.pending_yield = {"at": now + self.YIELD_GRACE, "bssid": bssid, "winner": winner, "reason": reason}
        log.warning("%s: yielding: %s", self.id, reason)

    # ------------------------------------------------------------ net events
    def handle_net_event(self, ev: NetEvent, now: float) -> None:
        k = ev.kind
        if k == EventKind.SCAN_RESULT:
            self.scan_pending = False
            self._on_scan(ev.data or [], now)
            return
        self.net_pending = False
        if k == EventKind.HOTSPOT_UP and self.state == NodeState.STARTING_HOTSPOT:
            self.hotspot_failures = 0
            self.epoch_seen += 1
            self.my_claim = LeaderClaim(self.epoch_seen, self.priority, self.id)
            self.leader = None
            self.leader_since = now
            self.expected_leader = None
            self.foreign.clear()
            self.next_lone_rescan = now + self.LONE_LEADER_RESCAN + self.rng.uniform(0, 20)
            self._go(NodeState.LEADER, now, f"hotspot up, epoch {self.epoch_seen}")
        elif k == EventKind.HOTSPOT_FAILED and self.state == NodeState.STARTING_HOTSPOT:
            self.hotspot_failures += 1
            if self.hotspot_failures >= 3:
                self._go(NodeState.DEGRADED, now, f"hotspot failed x{self.hotspot_failures}: {ev.error}",
                         now + self.cfg.degraded_retry)
            else:
                self._enter_discovery(now, "hotspot start failed: " + ev.error, short=True)
        elif k == EventKind.CONNECTED and self.state == NodeState.RECONNECTING:
            self.connect_failures = 0
            self.deadline = now + self.BEACON_WAIT
        elif k == EventKind.CONNECT_FAILED and self.state == NodeState.RECONNECTING:
            self.connect_failures += 1
            if self.scout:
                self.scout = None
            if self.connect_failures >= 6:
                self._go(NodeState.DEGRADED, now, "repeated connect failures", now + self.cfg.degraded_retry)
            else:
                self._enter_discovery(now, "connect failed: " + ev.error, short=True)
        elif k == EventKind.LINK_LOST:
            if self.state == NodeState.LEADER:
                self.my_claim = None
                self._go(NodeState.STARTING_HOTSPOT, now, "hotspot link lost, restarting",
                         now + self.cfg.hotspot_start_timeout + 5)
                self.net_pending = True
                self.adapter.start_hotspot()
            elif self.connected():
                self._go(NodeState.SUSPECTED_FAILURE, now, "wifi link lost")
                self.leader_seen = min(self.leader_seen, now - self.cfg.suspect_after)

    def _on_scan(self, entries: list, now: float) -> None:
        ours = [e for e in entries if e.ssid == self.cfg.ssid
                and self.bad_bssids.get(e.bssid, 0) < now]
        if self.state in (NodeState.DISCOVERING, NodeState.ELECTION_BACKOFF):
            if ours:
                best = max(ours, key=lambda e: (e.bssid == self.leader_bssid, e.signal))
                self._reconnect(now, best.bssid, f"found hotspot {best.bssid}")
        elif self.state == NodeState.FOLLOWER:
            foreign = [e.bssid for e in ours if e.bssid != self.leader_bssid]
            if foreign:
                self.send({"t": "foreign_ap", "bssids": foreign}, self.current_leader_id())
        elif self.state == NodeState.LEADER:
            own = self.adapter.status().bssid
            others = [e.bssid for e in ours if e.bssid != own]
            if others and not self.followers(now):
                self._yield_to(LeaderClaim(-1, 0, "?"), others[0], now, "lone leader found another hotspot")

    # ------------------------------------------------------------------ tick
    def _enter_discovery(self, now: float, reason: str, short: bool = False) -> None:
        self.discovery_started = now
        window = self.cfg.discovery_window if (self.boot and not short) else min(4.0, self.cfg.discovery_window)
        self._go(NodeState.DISCOVERING, now, reason, now + window)
        self._scan()

    def _reconnect(self, now: float, bssid: str | None, reason: str) -> None:
        self._go(NodeState.RECONNECTING, now, reason, now + self.cfg.connect_timeout + self.BEACON_WAIT)
        self.net_pending = True
        self.adapter.connect(bssid)

    def tick(self, now: float) -> None:
        s = self.state
        if s == NodeState.BOOTING:
            self._enter_discovery(now, "boot")
            return
        # pending yield (leader side) / pending handover start (target side)
        if self.pending_yield and "at" in self.pending_yield and now >= self.pending_yield["at"]:
            py, self.pending_yield = self.pending_yield, None
            self.yield_count += 1
            hold = min(self.cfg.yield_holddown * (2 ** (self.yield_count - 1)), 600.0)
            self.holddown_until = now + hold
            self.failed_leader = self.id
            self.my_claim = None
            self.leader = py["winner"] if py["winner"].epoch >= 0 else None
            self._reconnect(now, py.get("bssid"), py["reason"])
            return
        if s == NodeState.STARTING_HOTSPOT and self.pending_yield and "start_at" in self.pending_yield:
            if now >= self.pending_yield["start_at"]:
                self.pending_yield = None
                self.net_pending = True
                self.adapter.start_hotspot()
                self.deadline = now + self.cfg.hotspot_start_timeout + 5
            return

        if s == NodeState.DISCOVERING:
            if self.deadline is not None and now >= self.deadline:
                if self.can_lead and now >= self.holddown_until:
                    self._go(NodeState.ELECTION_BACKOFF, now, f"no hotspot found (rank {self._rank(now)})",
                             now + self._backoff(now))
                else:
                    self.deadline = now + 4.0
                    self._scan()
            else:
                self._scan()
        elif s == NodeState.ELECTION_BACKOFF:
            if now >= (self.deadline or now):
                if now < self.holddown_until:
                    self._go(NodeState.DISCOVERING, now, "hold-down active", self.holddown_until)
                else:
                    self.boot = False
                    self._go(NodeState.STARTING_HOTSPOT, now, "backoff expired",
                             now + self.cfg.hotspot_start_timeout + 5)
                    self.net_pending = True
                    self.adapter.start_hotspot()
            else:
                self._scan()
        elif s == NodeState.STARTING_HOTSPOT:
            if self.deadline is not None and now >= self.deadline:
                self.hotspot_failures += 1
                self._enter_discovery(now, "hotspot start timeout", short=True)
        elif s == NodeState.RECONNECTING:
            if self.deadline is not None and now >= self.deadline:
                st = self.adapter.status()
                if st.mode == RadioMode.STATION and st.bssid:
                    # associated but no leader beacon: zombie AP (core dead) -> avoid it
                    self.bad_bssids[st.bssid] = now + 60.0
                if self.scout:
                    self.scout = None
                self._enter_discovery(now, "no leader beacon after connect", short=True)
        elif s == NodeState.RECOVERING:
            if self.deadline is not None and now >= self.deadline:
                self._go(NodeState.FOLLOWER, now, "recovery timeout, following with local state")
        elif s == NodeState.FOLLOWER:
            self.boot = False
            if now - self.leader_seen > self.cfg.suspect_after:
                self._go(NodeState.SUSPECTED_FAILURE, now, f"no leader beacon for {now - self.leader_seen:.1f}s")
            elif now >= self.next_foreign_scan:
                self.next_foreign_scan = now + self.FOREIGN_SCAN_INTERVAL + self.rng.uniform(0, 15)
                self._scan()
        elif s == NodeState.SUSPECTED_FAILURE:
            if now - self.leader_seen > self.cfg.failure_after:
                self.failed_leader = self.leader.node_id if self.leader else None
                self.leader = None
                self._enter_discovery(now, f"leader {self.failed_leader} lost", short=True)
        elif s == NodeState.LEADER:
            if self.scout_sent or self.foreign:
                self._maybe_order_scout(now)
            if now >= self.next_lone_rescan:
                self.next_lone_rescan = now + self.LONE_LEADER_RESCAN + self.rng.uniform(0, 20)
                if not self.followers(now) and now - self.leader_since > self.cfg.min_leader_tenure:
                    if self.adapter.supports_scan_in_ap_mode():
                        self._scan()
                    else:
                        # Give up a follower-less AP briefly to look for a competing cluster.
                        self.my_claim = None
                        self.failed_leader = None
                        self.adapter.stop()
                        self._enter_discovery(now, "lone leader re-discovery", short=True)
        elif s == NodeState.DEGRADED:
            if self.deadline is not None and now >= self.deadline:
                self.hotspot_failures = 0
                self.connect_failures = 0
                self._enter_discovery(now, "degraded retry", short=True)
        # scout timeout (follower side)
        if self.scout and now >= self.scout.get("until", now):
            self.scout = None
            if self.state in (NodeState.RECONNECTING, NodeState.RECOVERING):
                self._enter_discovery(now, "scout timeout", short=True)

    # --------------------------------------------------------------- control
    def request_handover(self, target: str, now: float) -> bool:
        """Controlled failover initiated by the admin on the leader."""
        if not self.is_leader() or target == self.id:
            return False
        p = self.peers.get(target)
        if not p or now - p.last_seen > 5.0 or p.info.get("can_lead") is False:
            return False
        for _ in range(3):
            self.send({"t": "handover", "target": target}, None)
        self.expected_leader = (target, now + self.HANDOVER_EXPECT)
        self.pending_yield = {"at": now + self.YIELD_GRACE, "bssid": None,
                              "winner": LeaderClaim(-1, 0, target), "reason": f"controlled handover to {target}"}
        self.yield_count = 0
        return True

    def drain_adapter(self, now: float) -> None:
        for ev in self.adapter.poll_events():
            self.handle_net_event(ev, now)
