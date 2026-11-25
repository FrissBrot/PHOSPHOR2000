"""Discrete-event simulator for a PHOSPHOR cluster.

Runs the *real* ``ClusterNode`` logic (election, clock sync, replication,
scheduler) against simulated radios, Wi-Fi latency/loss, independent drifting
clocks with arbitrary monotonic origins, crashes, partitions and audio.
Everything is deterministic for a given seed. Results from this module are
SIMULATED — they validate logic, not radio timing on real Pi 3 hardware.
"""
from __future__ import annotations

import argparse
import heapq
import itertools
import json
import math
import random
from dataclasses import dataclass, field
from typing import Callable

from backend.cluster.messages import Codec, CodecError, derive_key
from backend.cluster.node import ClusterNode
from backend.config import AppConfig, load_config
from backend.network.base import EventKind, NetEvent, NetworkAdapter, RadioMode, RadioStatus, ScanEntry


@dataclass
class SimClock:
    origin: float          # local monotonic value at true time 0 (arbitrary, differs per node)
    drift_ppm: float

    def local(self, true_t: float) -> float:
        return self.origin + true_t * (1 + self.drift_ppm * 1e-6)

    def true(self, local_t: float) -> float:
        return (local_t - self.origin) / (1 + self.drift_ppm * 1e-6)


@dataclass
class RadioTiming:
    scan: float = 3.0
    hotspot: float = 6.0
    connect: float = 4.0
    connect_fail: float = 10.0
    link_loss_detect: float = 2.5


class SimAdapter(NetworkAdapter):
    def __init__(self, world: "SimWorld", nid: str) -> None:
        self.w, self.nid = world, nid
        self.mode = RadioMode.IDLE
        self.ap_bssid: str | None = None      # station: bssid we are associated with
        self.events: list[NetEvent] = []
        self.op_token = 0
        self.mac = f"02:00:00:00:00:{int(nid[-2:]):02X}"
        self.fail_hotspot = False

    def _schedule(self, delay: float, fn: Callable[[], None]) -> None:
        self.op_token += 1
        tok = self.op_token

        def run() -> None:
            if tok == self.op_token and self.w.alive(self.nid):
                fn()
        self.w.at(self.w.now + delay, run)

    def request_scan(self) -> None:
        def done() -> None:
            res = [ScanEntry(self.w.cfg_ssid, ad.mac, 70) for nid, ad in self.w.adapters.items()
                   if nid != self.nid and self.w.alive(nid) and ad.mode == RadioMode.AP and self.w.in_range(self.nid, nid)]
            self.events.append(NetEvent(EventKind.SCAN_RESULT, res))
        # scans don't cancel mode operations: use a separate timer
        self.w.at(self.w.now + self.w.timing.scan, lambda: self.w.alive(self.nid) and done())

    def start_hotspot(self) -> None:
        self._leave()
        self.mode = RadioMode.BUSY

        def done() -> None:
            if self.fail_hotspot:
                self.mode = RadioMode.IDLE
                self.events.append(NetEvent(EventKind.HOTSPOT_FAILED, ok=False, error="sim failure"))
                return
            self.mode = RadioMode.AP
            self.events.append(NetEvent(EventKind.HOTSPOT_UP, {"bssid": self.mac}))
        self._schedule(self.w.timing.hotspot, done)

    def connect(self, bssid: str | None = None) -> None:
        self._leave()
        self.mode = RadioMode.BUSY
        target = self.w.find_ap(self.nid, bssid)
        if target is None:
            def fail() -> None:
                self.mode = RadioMode.IDLE
                self.events.append(NetEvent(EventKind.CONNECT_FAILED, ok=False, error="no such AP"))
            self._schedule(self.w.timing.connect_fail, fail)
            return

        def done() -> None:
            ap = self.w.adapters[target]
            if ap.mode != RadioMode.AP or not self.w.in_range(self.nid, target):
                self.mode = RadioMode.IDLE
                self.events.append(NetEvent(EventKind.CONNECT_FAILED, ok=False, error="AP vanished"))
                return
            self.mode = RadioMode.STATION
            self.ap_bssid = ap.mac
            self.events.append(NetEvent(EventKind.CONNECTED, {"bssid": ap.mac, "ip": "192.168.50.x"}))
        self._schedule(self.w.timing.connect, done)

    def _leave(self) -> None:
        if self.mode == RadioMode.AP:
            self.w.ap_down(self.nid)
        self.mode = RadioMode.IDLE
        self.ap_bssid = None

    def stop(self) -> None:
        self.op_token += 1
        self._leave()
        self.events.append(NetEvent(EventKind.STOPPED))

    def link_lost(self) -> None:
        if self.mode == RadioMode.STATION:
            self.mode = RadioMode.IDLE
            self.ap_bssid = None
            self.events.append(NetEvent(EventKind.LINK_LOST, {}))

    def status(self) -> RadioStatus:
        return RadioStatus(self.mode, self.mac if self.mode == RadioMode.AP else self.ap_bssid, None, self.mac)

    def poll_events(self) -> list[NetEvent]:
        ev, self.events = self.events, []
        return ev


@dataclass
class SimMusic:
    bpm: float = 128.0
    phase0: float = 0.37           # true time of beat 0
    onset_noise_ms: float = 4.0

    def beat_time(self, k: int) -> float:
        return self.phase0 + k * 60.0 / self.bpm


@dataclass
class Handle:
    nid: str
    node: ClusterNode
    clock: SimClock
    codec: Codec
    alive: bool = True
    boot_id: str = "boot-0"


class SimWorld:
    def __init__(self, n: int = 3, seed: int = 1, base_cfg: AppConfig | None = None,
                 latency_ms: tuple[float, float] = (2.0, 6.0), loss: float = 0.01,
                 spike_prob: float = 0.04, timing: RadioTiming | None = None) -> None:
        self.rng = random.Random(seed)
        self.now = 0.0
        self.q: list = []
        self.seq = itertools.count()
        self.timing = timing or RadioTiming()
        self.latency_ms, self.loss, self.spike_prob = latency_ms, loss, spike_prob
        self.base_cfg = base_cfg or load_config(overrides={})
        self.cfg_ssid = self.base_cfg.network.ssid
        self.key = derive_key("sim-key")
        self.handles: dict[str, Handle] = {}
        self.adapters: dict[str, SimAdapter] = {}
        self.groups: list[set[str]] | None = None      # radio partitions
        self.music: SimMusic | None = None
        self.audio_nodes: set[str] = set()
        self.delivered = 0
        self.dropped = 0
        self.log: list[str] = []
        for i in range(1, n + 1):
            self.add_node(f"pi-0{i}")

    # ---- scheduling ----------------------------------------------------
    def at(self, t: float, fn: Callable[[], None]) -> None:
        heapq.heappush(self.q, (t, next(self.seq), fn))

    def run_until(self, t_end: float) -> None:
        while self.q and self.q[0][0] <= t_end:
            t, _, fn = heapq.heappop(self.q)
            self.now = t
            fn()
        self.now = t_end

    def run_for(self, dt: float) -> None:
        self.run_until(self.now + dt)

    # ---- topology ------------------------------------------------------
    def alive(self, nid: str) -> bool:
        h = self.handles.get(nid)
        return bool(h and h.alive)

    def in_range(self, a: str, b: str) -> bool:
        if self.groups is None:
            return True
        return any(a in g and b in g for g in self.groups)

    def partition(self, *groups: list[str]) -> None:
        self.groups = [set(g) for g in groups]
        # stations that lost range to their AP lose link
        for nid, ad in self.adapters.items():
            if ad.mode == RadioMode.STATION:
                ap = self.ap_owner(ad.ap_bssid)
                if ap and not self.in_range(nid, ap):
                    self.at(self.now + self.timing.link_loss_detect, ad.link_lost)

    def heal(self) -> None:
        self.groups = None

    def ap_owner(self, bssid: str | None) -> str | None:
        for nid, ad in self.adapters.items():
            if ad.mac == bssid and ad.mode == RadioMode.AP and self.alive(nid):
                return nid
        return None

    def find_ap(self, nid: str, bssid: str | None) -> str | None:
        cands = [o for o, ad in self.adapters.items() if o != nid and self.alive(o) and ad.mode == RadioMode.AP
                 and self.in_range(nid, o)]
        if bssid:
            cands = [o for o in cands if self.adapters[o].mac == bssid]
        return sorted(cands)[0] if cands else None

    def ap_down(self, owner: str) -> None:
        mac = self.adapters[owner].mac
        for nid, ad in self.adapters.items():
            if ad.mode == RadioMode.STATION and ad.ap_bssid == mac:
                self.at(self.now + self.timing.link_loss_detect, ad.link_lost)

    def segment_of(self, nid: str) -> str | None:
        ad = self.adapters[nid]
        if ad.mode == RadioMode.AP:
            return nid
        if ad.mode == RadioMode.STATION:
            return self.ap_owner(ad.ap_bssid)
        return None

    # ---- nodes ---------------------------------------------------------
    def node_cfg(self, nid: str) -> AppConfig:
        cfg = self.base_cfg.model_copy(deep=True)
        cfg.node.id = nid
        cfg.node.priority = cfg.node.priority_table.get(nid, 50)
        return cfg

    def add_node(self, nid: str, cache: dict | None = None, new_boot: bool = True,
                 start_delay: float = 0.0) -> Handle:
        old = self.handles.get(nid)
        if old is not None and not new_boot:
            clock = old.clock
            boot_id = old.boot_id
        else:
            clock = SimClock(origin=self.rng.uniform(1_000, 900_000), drift_ppm=self.rng.uniform(-40, 40))
            boot_id = f"boot-{self.rng.random():.6f}"
        ad = self.adapters.get(nid) or SimAdapter(self, nid)
        ad.mode, ad.ap_bssid, ad.events, ad.op_token = RadioMode.IDLE, None, [], ad.op_token + 1
        self.adapters[nid] = ad
        cfg = self.node_cfg(nid)
        node = ClusterNode(cfg, ad, lambda m, to, _n=nid: self.send(_n, m, to),
                           rng=random.Random(self.rng.random()), has_audio=nid in self.audio_nodes,
                           wall_clock=lambda: 1.8e9 + self.now)
        h = Handle(nid, node, clock, Codec(self.key, nid), True, boot_id)
        if cache:
            node.restore_cache(cache, boot_id, clock.local(self.now))
        self.handles[nid] = h
        self.at(self.now + start_delay, lambda: self._tick(h))
        return h

    def crash(self, nid: str) -> dict:
        h = self.handles[nid]
        blob = h.node.cache_blob(h.boot_id, h.clock.local(self.now))
        ad = self.adapters[nid]
        if ad.mode == RadioMode.AP:
            self.ap_down(nid)
        ad.mode, ad.ap_bssid = RadioMode.IDLE, None
        ad.op_token += 1
        h.alive = False
        return blob

    def _tick(self, h: Handle) -> None:
        if not h.alive or self.handles.get(h.nid) is not h:
            return
        h.node.tick(h.clock.local(self.now))
        self.at(self.now + 0.05 + self.rng.uniform(0, 0.004), lambda: self._tick(h))

    # ---- messaging -----------------------------------------------------
    def send(self, src: str, msg: dict, to: str | None) -> None:
        h = self.handles.get(src)
        if not h or not h.alive:
            return
        seg = self.segment_of(src)
        if seg is None:
            return
        data = h.codec.encode(msg)
        for nid, oh in self.handles.items():
            if nid == src or not oh.alive or self.segment_of(nid) != seg:
                continue
            if to is not None and nid != to:
                continue
            if self.rng.random() < self.loss:
                self.dropped += 1
                continue
            lat = self.latency_ms[0] + self.rng.expovariate(1.0 / self.latency_ms[1])
            if self.rng.random() < self.spike_prob:
                lat += self.rng.uniform(20, 120)
            # if the source sends to the AP and the AP relays, latency roughly doubles for station->station
            if nid != seg and src != seg:
                lat *= 1.8
            self.at(self.now + lat / 1000.0, lambda oh=oh, data=data, seg=seg: self._deliver(oh, data, seg))

    def _deliver(self, h: Handle, data: bytes, seg: str) -> None:
        if not h.alive or self.segment_of(h.nid) != seg:
            self.dropped += 1
            return
        try:
            msg = h.codec.decode(data)
        except CodecError:
            self.dropped += 1
            return
        self.delivered += 1
        h.node.on_message(msg, h.clock.local(self.now))

    # ---- audio ---------------------------------------------------------
    def start_music(self, bpm: float, nodes: list[str], phase0: float = 0.37) -> None:
        self.music = SimMusic(bpm=bpm, phase0=phase0)
        self.audio_nodes = set(nodes)
        for n in nodes:
            if n in self.handles:
                self.handles[n].node.has_audio = True
        k0 = max(0, math.ceil((self.now - phase0) * bpm / 60.0))
        self.at(self.music.beat_time(k0), lambda: self._beat(k0))

    def stop_music(self) -> None:
        self.music = None

    def _beat(self, k: int) -> None:
        m = self.music
        if m is None:
            return
        for nid in self.audio_nodes:
            h = self.handles.get(nid)
            if not h or not h.alive:
                continue
            noisy = m.beat_time(k) + self.rng.gauss(0, m.onset_noise_ms / 1000)
            # The audio engine reports the *next* predicted beat in local monotonic time.
            est = {"live": True, "bpm": m.bpm, "beat_local": h.clock.local(noisy + 60.0 / m.bpm),
                   "confidence": 0.9, "phase_unc_ms": m.onset_noise_ms, "state": "LIVE"}
            h.node.ingest_local_audio(est, h.clock.local(self.now))
        self.at(m.beat_time(k + 1), lambda: self._beat(k + 1))

    # ---- metrics -------------------------------------------------------
    def leaders(self) -> list[str]:
        return [n for n, h in self.handles.items() if h.alive and h.node.is_leader()]

    def states(self) -> dict[str, str]:
        return {n: h.node.state.value for n, h in self.handles.items() if h.alive}

    def true_time_of_beat(self, nid: str, beat: float) -> float:
        """True (simulation) time at which node `nid` would render `beat`."""
        h = self.handles[nid]
        c = h.node.timeline.time_of_beat(beat)
        return h.clock.true(h.node.sync.to_local(c))

    def beat_spread_ms(self, nodes: list[str] | None = None, ahead_beats: float = 8.0) -> float:
        nodes = nodes or [n for n, h in self.handles.items() if h.alive]
        ref = self.handles[nodes[0]]
        c_now = ref.node.cluster_now(ref.clock.local(self.now))
        beat = math.ceil(ref.node.timeline.beat_at(c_now) + ahead_beats)
        ts = [self.true_time_of_beat(n, beat) for n in nodes]
        return (max(ts) - min(ts)) * 1000.0

    def music_phase_error_ms(self, nid: str, ahead_beats: float = 4.0) -> float:
        """Distance between node's predicted beat and the true music beat grid."""
        assert self.music is not None
        h = self.handles[nid]
        c_now = h.node.cluster_now(h.clock.local(self.now))
        beat = math.ceil(h.node.timeline.beat_at(c_now) + ahead_beats)
        t = self.true_time_of_beat(nid, beat)
        period = 60.0 / self.music.bpm
        ph = ((t - self.music.phase0) / period) % 1.0
        if ph > 0.5:
            ph -= 1.0
        return ph * period * 1000.0


def main() -> None:  # pragma: no cover - demo CLI
    ap = argparse.ArgumentParser(description="PHOSPHOR2000 cluster simulation")
    ap.add_argument("--nodes", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--scenario", choices=["boot", "failover", "partition"], default="failover")
    a = ap.parse_args()
    w = SimWorld(a.nodes, a.seed)
    w.start_music(128, ["pi-02"]) if a.nodes > 1 else None
    report = {}
    w.run_for(60)
    report["after_boot"] = {"states": w.states(), "leaders": w.leaders(), "spread_ms": round(w.beat_spread_ms(), 2)}
    if a.scenario == "failover":
        lead = w.leaders()[0]
        w.crash(lead)
        t0 = w.now
        while w.now - t0 < 120 and not w.leaders():
            w.run_for(0.5)
        report["failover"] = {"crashed": lead, "new_leader": w.leaders(), "seconds": round(w.now - t0, 1)}
        w.run_for(30)
        report["after_failover"] = {"states": w.states(), "spread_ms": round(w.beat_spread_ms(), 2)}
    elif a.scenario == "partition":
        ids = sorted(w.handles)
        w.partition(ids[:1], ids[1:])
        w.run_for(90)
        report["partitioned"] = {"states": w.states(), "leaders": w.leaders()}
        w.heal()
        w.run_for(240)
        report["healed"] = {"states": w.states(), "leaders": w.leaders()}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
