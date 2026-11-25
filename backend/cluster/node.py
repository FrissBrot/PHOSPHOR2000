"""One PHOSPHOR node: election + clock sync + replicated musical state.

The class is pure logic driven by ``tick(local_now)`` and ``on_message(...)``
with an injected ``send`` function and radio adapter, so the *same code* runs
in the asyncio service (backend/services/core.py) and in the discrete-event
simulator (backend/cluster/simulation.py).

Clock domains:
  local  - this machine's CLOCK_MONOTONIC (seconds). Never sent for comparison.
  cluster- virtual timebase, see clocksync.py. All replicated times use it.
  wall   - cluster + wall_offset (for the on-screen date/time display only).
"""
from __future__ import annotations

import logging
import math
import re
import random
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from backend.cluster.election import AUDIO_BONUS, Election, NodeState, Transition
from backend.cluster.scheduler import Override, ScheduleState, default_schedule, plan, quantum_beats
from backend.config import AppConfig, PLAYABLE_SCENES, ScenesConfig, SharedConfig
from backend.network.base import NetworkAdapter
from backend.sync.calibration import TapFilter, analyse_taps
from backend.sync.clocksync import ClockSync
from backend.sync.timeline import TapTempo, Timeline, TimingSource, reconcile

log = logging.getLogger("phos.node")

Send = Callable[[dict, str | None], None]

AUDIO_STALE_S = 3.0
FEATURES_STALE_S = 0.6
STATE_INTERVAL = 2.0
STATUS_INTERVAL = 1.0
CAL_HISTORY_MAX = 50
HASH_RE = r"^scrypt\$\d+\$\d+\$\d+\$[A-Za-z0-9_-]+\$[A-Za-z0-9_-]+$"
LIVE_DEADBAND_MS = 3.0
LIVE_PHASE_GAIN = 0.35
LIVE_TEMPO_GAIN = 0.5


@dataclass
class CalSession:
    active: bool = False
    mode: str = "single"           # single | all | click
    target: str | None = None
    bpm: float = 120.0
    origin: float = 0.0            # cluster time of flash 0 (uncorrected)
    taps: list[float] = field(default_factory=list)
    trial_offset_ms: float | None = None
    started_offset_ms: float = 0.0
    result: dict | None = None
    rev: int = 0

    def to_dict(self) -> dict:
        return {"active": self.active, "mode": self.mode, "target": self.target, "bpm": self.bpm,
                "origin": self.origin, "tap_count": len(self.taps), "tap_times": [round(t, 5) for t in self.taps], "trial_offset_ms": self.trial_offset_ms,
                "started_offset_ms": self.started_offset_ms, "result": self.result, "rev": self.rev}

    @classmethod
    def from_dict(cls, d: dict | None) -> "CalSession":
        if not d:
            return cls()
        return cls(active=bool(d.get("active")), mode=d.get("mode", "single"), target=d.get("target"),
                   bpm=float(d.get("bpm", 120)), origin=float(d.get("origin", 0)),
                   taps=[float(t) for t in (d.get("tap_times") or [])][-128:],
                   trial_offset_ms=d.get("trial_offset_ms"), started_offset_ms=float(d.get("started_offset_ms", 0)),
                   result=d.get("result"), rev=int(d.get("rev", 0)))


class CommandError(ValueError):
    pass


class ClusterNode:
    def __init__(self, cfg: AppConfig, adapter: NetworkAdapter, send: Send, rng: random.Random | None = None,
                 has_audio: bool = False, wall_clock: Callable[[], float] = time.time,
                 on_transition: Callable[[Transition], None] | None = None) -> None:
        self.cfg = cfg
        self.id = cfg.node.id
        self.send = send
        self.rng = rng or random.Random()
        self.wall_clock = wall_clock
        self.has_audio = has_audio
        self.election = Election(self.id, cfg.node.priority + (AUDIO_BONUS if has_audio else 0), cfg.network,
                                 adapter, self._send_election,
                                 rng=self.rng, priority_table=cfg.node.priority_table,
                                 on_transition=self._on_transition)
        self._ext_transition = on_transition
        sc = cfg.sync
        self.sync = ClockSync(window=sc.window, max_slew_ppm=sc.max_slew_ppm, step_threshold=sc.step_threshold,
                              outlier_delay_factor=sc.outlier_delay_factor)
        self.shared: SharedConfig = cfg.shared()
        self.shared_rev = 0
        self.offset_versions: dict[str, list] = {}    # node -> [wall_ms, by]
        self.timeline = Timeline(bpm=cfg.timeline.default_bpm, beats_per_bar=cfg.timeline.beats_per_bar,
                                 source=TimingSource.PREDICTED)
        self.schedule: ScheduleState = default_schedule(self.shared.scenes, cfg.node.priority_table.keys() or [self.id],
                                                        beats_per_bar=cfg.timeline.beats_per_bar)
        self.manual = False
        self.tap = TapTempo()
        self.cal = CalSession(bpm=self.shared.calibration.bpm)
        self.cal_filter: TapFilter | None = None
        self.wall_offset = 0.0
        self.wall_offset_set = False
        self.leader_epoch_applied: tuple[str, int] | None = None
        self.last_state_rx: float | None = None      # local
        self.last_state_sent = -1e9
        self.last_status_sent = -1e9
        self.last_beacon = -1e9
        self.next_ping = 0.0
        self.burst_left = 0
        self.ping_leader: str | None = None
        self.peer_status: dict[str, dict] = {}        # leader: node -> status (+ local rx time)
        self.peer_ips: dict[str, str] = {}
        self.audio_estimates: dict[str, dict] = {}    # leader: src -> last estimate (+ local rx)
        self.audio_src: str | None = None
        self.features: dict | None = None             # latest spectrum/levels (cluster-stamped)
        self.features_rx: float | None = None
        self.local_audio: dict | None = None          # from local audio service
        self.renderer: dict = {}                      # fps etc. from local browser
        self.reconcile_info: dict = {"mode": "none", "delta_beats": 0.0}
        self.started_local: float | None = None
        self.log_lines: list[dict] = []
        self.dirty = True                             # needs cache write
        self.restored_from_cache = False
        self.version = "unknown"
        self.duplicate: dict | None = None            # another device claims our node id
        # cluster-wide admin password (scrypt hash) with last-writer-wins version [wall_ms, origin]
        self.admin: dict = {"hash": cfg.secrets.admin_password_hash, "ver": [0, self.id]}
        self.cal_history: list[dict] = []            # replicated calibration history (newest last, max 50)
        self.extra: dict = {}                         # replicated leader-owned state (update rollout)

    # ================================================================ helpers
    def _on_transition(self, tr: Transition) -> None:
        self._log("info", f"state {tr.old} -> {tr.new}: {tr.reason}")
        if self._ext_transition:
            self._ext_transition(tr)

    def _log(self, level: str, text: str) -> None:
        self.log_lines.append({"wall": self.wall_clock(), "level": level, "node": self.id, "msg": text})
        self.log_lines = self.log_lines[-500:]
        getattr(log, level if level in ("info", "warning", "error") else "info")("%s: %s", self.id, text)

    def _send_election(self, msg: dict, to: str | None) -> None:
        self.send(msg, to)

    def set_has_audio(self, has_audio: bool, local: float) -> None:
        """An audio input device appeared/disappeared: audio nodes always lead."""
        if has_audio != self.has_audio:
            self._log("warning", f"audio input {'present' if has_audio else 'gone'}: "
                                 f"{'claiming' if has_audio else 'releasing'} audio leader priority")
        self.has_audio = has_audio
        self.election.set_priority(self.cfg.node.priority + (AUDIO_BONUS if has_audio else 0), local)

    @property
    def state(self) -> NodeState:
        return self.election.state

    def is_leader(self) -> bool:
        return self.election.is_leader()

    def cluster_now(self, local: float) -> float:
        return self.sync.to_cluster(local)

    def visual_offset_ms(self, node_id: str | None = None) -> float:
        nid = node_id or self.id
        base = self.shared.timeline.global_visual_offset_ms
        if self.cal.active and self.cal.target == nid and self.cal.trial_offset_ms is not None:
            return base + self.cal.trial_offset_ms
        return base + self.shared.calibration.offsets_ms.get(nid, 0.0)

    # ================================================================== tick
    def tick(self, local: float) -> None:
        if self.started_local is None:
            self.started_local = local
        if self.shared.site.wifi_country != self.applied_country:
            self._apply_site()
        el = self.election
        was_leader = el.is_leader()
        prev_leader = el.current_leader_id()
        el.drain_adapter(local)
        el.tick(local)
        self._roles_changed(local, was_leader, prev_leader)
        now_c = self.cluster_now(local)
        self._maintain_timeline(local, now_c)
        if local - self.last_beacon >= self.cfg.network.beacon_interval and el.state != NodeState.BOOTING:
            self.last_beacon = local
            self.send(self._beacon(local, now_c), None)
        if el.is_leader():
            if local - self.last_state_sent >= STATE_INTERVAL:
                self._broadcast_state(local)
        else:
            self._ping_tick(local)
            if el.connected() and local - self.last_status_sent >= STATUS_INTERVAL and el.current_leader_id():
                self.last_status_sent = local
                self.send({"t": "status", **self.status(local)}, el.current_leader_id())
        if el.state == NodeState.RECOVERING and self.last_state_rx is not None and \
                self.leader_epoch_applied and self.leader_epoch_applied[0] == el.current_leader_id():
            el.recovery_complete(local)

    def _roles_changed(self, local: float, was_leader: bool, prev_leader: str | None) -> None:
        el = self.election
        if el.is_leader() and not was_leader:
            self.sync.freeze(local)
            now_c = self.cluster_now(local)
            if not self.wall_offset_set:
                self.wall_offset = self.wall_clock() - now_c
            # continue the replicated musical state seamlessly under a new epoch
            src = TimingSource.MANUAL if self.manual else TimingSource.PREDICTED
            base = self.timeline.rebased(now_c)
            if self.timeline.revision == 0 and not self.restored_from_cache:
                # brand-new cluster: start counting at beat 0 on the next bar
                base = replace(base, anchor_beat=0.0, anchor_time=now_c)
            self.timeline = replace(base, epoch=el.my_claim.epoch if el.my_claim else 0,
                                    revision=self.timeline.revision + 1, source=src)
            self.leader_epoch_applied = (self.id, self.timeline.epoch)
            self._log("warning", f"became LEADER epoch {self.timeline.epoch}; timeline continues at beat "
                                 f"{self.timeline.beat_at(now_c):.2f}")
            self._broadcast_state(local)
            self.dirty = True
        elif was_leader and not el.is_leader():
            self.sync.unfreeze()
            self._log("warning", "no longer leader")
        cur = el.current_leader_id()
        if not el.is_leader() and cur and cur != self.ping_leader:
            self.ping_leader = cur
            self.sync.reset_samples()
            self.burst_left = self.cfg.sync.burst_count
            self.next_ping = local

    def _ping_tick(self, local: float) -> None:
        el = self.election
        leader = el.current_leader_id()
        if not leader or not el.connected() or el.state == NodeState.SUSPECTED_FAILURE:
            return
        if local >= self.next_ping:
            self.send(self.sync.make_ping(local), leader)
            if self.burst_left > 0:
                self.burst_left -= 1
                self.next_ping = local + 0.15
            else:
                self.next_ping = local + self.cfg.sync.ping_interval * (0.9 + 0.2 * self.rng.random())

    def _maintain_timeline(self, local: float, now_c: float) -> None:
        tl = self.timeline
        # fold finished slews to keep numbers small and the state canonical
        if tl.slew_delta and now_c > max(tl.slew_start, tl.slew_end) + 1.0:
            self.timeline = tl.rebased(now_c)
        if not self.is_leader():
            return
        # leader: LIVE -> PREDICTED when the audio source goes stale
        if self.timeline.source == TimingSource.LIVE:
            fresh = self.timeline.live_at is not None and now_c - self.timeline.live_at < AUDIO_STALE_S
            if not fresh:
                self.timeline = replace(self.timeline.rebased(now_c), source=TimingSource.PREDICTED,
                                        revision=self.timeline.revision + 1,
                                        confidence=self.timeline.confidence * 0.5)
                self._log("warning", "audio stale: timeline now PREDICTED (free-running at "
                                     f"{self.timeline.bpm:.2f} BPM)")
                self._broadcast_state(local)

    # =============================================================== beacons
    def _beacon(self, local: float, now_c: float) -> dict:
        b = {"t": "beacon", **self.election.beacon_fields(local)}
        b.update({"ver": self.version, "tl_rev": self.timeline.revision, "shared_rev": self.shared_rev,
                  "has_audio": self.has_audio,
                  "audio_live": bool(self.local_audio and self.local_audio.get("live"))})
        return b

    # ============================================================ replication
    def snapshot(self) -> dict:
        return {
            "epoch": self.timeline.epoch,
            "timeline": self.timeline.to_dict(),
            "schedule": self.schedule.to_dict(),
            "shared": self.shared.model_dump(),
            "shared_rev": self.shared_rev,
            "offset_versions": self.offset_versions,
            "wall_offset": self.wall_offset,
            "manual": self.manual,
            "cal": self.cal.to_dict(),
            "audio_src": self.audio_src,
            "admin": self.admin,
            "cal_history": self.cal_history[-CAL_HISTORY_MAX:],
            "extra": self.extra,
        }

    def _broadcast_state(self, local: float) -> None:
        self.last_state_sent = local
        snap = self.snapshot()
        # the calibration history is the biggest part: only when it changed, else every 30 s
        # (keeps the 2-s state datagram to ~1 kB = one Wi-Fi frame)
        # (sent with the next 3 broadcasts after a change, so a lost datagram does not matter)
        hkey = (len(self.cal_history), self.cal_history[-1]["id"] if self.cal_history else None)
        key, last, left = self._hist_sent
        if hkey != key:
            key, last, left = hkey, local, 3
        if left > 0 or local - last >= 30.0:
            self._hist_sent = (key, local, max(0, left - 1))
        else:
            snap.pop("cal_history")
        self.send({"t": "state", **snap}, None)

    _hist_sent: tuple = (None, -1e9, 0)

    def _apply_state(self, m: dict, local: float) -> None:
        el = self.election
        sender = m.get("_from")
        if sender != el.current_leader_id():
            return
        now_c = self.cluster_now(local)
        self.last_state_rx = local
        target = Timeline.from_dict(m["timeline"])
        key = (sender, int(m.get("epoch", 0)))
        if target.revision != self.timeline.revision or key != self.leader_epoch_applied:
            if self.sync.synced:
                r = reconcile(self.timeline, target, now_c, self.shared.timeline.slew_threshold_beats,
                              self.shared.timeline.slew_duration_beats)
                self.timeline = replace(r.timeline, revision=target.revision, epoch=target.epoch,
                                        source=target.source, confidence=target.confidence,
                                        live_at=target.live_at, seed=target.seed)
                if r.mode != "none":
                    self.reconcile_info = {"mode": r.mode, "delta_beats": r.delta_beats, "at_cluster": now_c}
                    if abs(r.delta_beats) > 0.01:
                        self._log("info", f"timeline reconcile {r.mode}: {r.delta_beats * 1000 * target.period():.1f} ms"
                                          f" ({r.delta_beats:+.3f} beats)")
            # else: wait for the first clock-sync sample before adopting phase
        sched = ScheduleState.from_dict(m["schedule"])
        if sched.revision != self.schedule.revision or key != self.leader_epoch_applied:
            self.schedule = sched
            self.dirty = True
        incoming_rev = int(m.get("shared_rev", 0))
        if incoming_rev != self.shared_rev or key != self.leader_epoch_applied:
            try:
                new_shared = SharedConfig.model_validate(m["shared"])
            except Exception as exc:  # never adopt invalid configuration
                self._log("error", f"rejected invalid shared config from {sender}: {exc}")
            else:
                if key != self.leader_epoch_applied:
                    self._report_offsets_to(sender)
                self.shared, self.shared_rev = new_shared, incoming_rev
                self.offset_versions = dict(m.get("offset_versions", {}))
                self.dirty = True
        self.wall_offset = float(m.get("wall_offset", self.wall_offset))
        self.manual = bool(m.get("manual", False))
        self.cal = CalSession.from_dict(m.get("cal"))
        self.cal_filter = None            # rebuilt from the replicated taps if we become leader
        self.audio_src = m.get("audio_src")
        self._merge_admin(m.get("admin"), local)
        self._merge_cal_history(m.get("cal_history") or [])
        self.extra = dict(m.get("extra") or {})
        if self.sync.synced:
            self.leader_epoch_applied = key

    def _report_offsets_to(self, leader: str) -> None:
        """After joining a (possibly different) leader, offer our calibration so newer entries survive merges."""
        self.send({"t": "offsets_report", "offsets": self.shared.calibration.offsets_ms,
                   "versions": self.offset_versions, "admin": self.admin,
                   "cal_history": self.cal_history[-CAL_HISTORY_MAX:]}, leader)

    # ---- admin password (cluster-wide, last writer wins) ----
    def _merge_admin(self, rec: dict | None, local: float) -> bool:
        if not rec or not isinstance(rec.get("hash"), str) or not re.fullmatch(HASH_RE, rec["hash"]):
            return False
        ver = list(rec.get("ver") or [0, ""])
        if ver > list(self.admin.get("ver") or [0, ""]):
            self.admin = {"hash": rec["hash"], "ver": ver}
            self.dirty = True
            self._log("info", "admin password updated (cluster-wide)")
            return True
        return False

    def admin_key_material(self) -> str:
        """Session key material: cluster key + password version -> one login for every Pi."""
        return f"{self.cfg.secrets.cluster_key}|{self.admin.get('ver')}|{self.admin.get('hash', '')[-16:]}"

    # ---- calibration history (cluster-wide union) ----
    def _merge_cal_history(self, entries: list) -> bool:
        known = {e.get("id") for e in self.cal_history}
        new = [e for e in entries if isinstance(e, dict) and e.get("id") and e["id"] not in known]
        if not new:
            return False
        self.cal_history = sorted(self.cal_history + new, key=lambda e: (e.get("wall", 0), e["id"]))[-CAL_HISTORY_MAX:]
        if self.on_cal_history:
            self.on_cal_history(new)
        return True

    def _merge_offsets(self, offsets: dict, versions: dict, local: float) -> None:
        changed = False
        cal = self.shared.calibration
        merged = dict(cal.offsets_ms)
        for nid, ms in offsets.items():
            ver = versions.get(nid)
            mine = self.offset_versions.get(nid)
            if ver and (mine is None or list(ver) > list(mine)):
                if -500 <= float(ms) <= 500:
                    merged[nid] = float(ms)
                    self.offset_versions[nid] = list(ver)
                    changed = True
        if changed:
            self._set_shared(replace_calibration(self.shared, offsets_ms=merged), local, "offsets merged")

    # =============================================================== messages
    def alerts(self, local: float) -> list[str]:
        out = []
        if self.duplicate and local - self.duplicate["at"] < 60:
            out.append(f"DUPLICATE NODE ID {self.id} ({self.duplicate.get('ip') or '?'})")
        return out

    def on_message(self, m: dict, local: float, src_ip: str | None = None) -> None:
        t = m.get("t")
        sender = m.get("_from")
        if sender == self.id:
            # Same id, different process boot: a second device was installed with our node id.
            first = self.duplicate is None or local - self.duplicate["at"] > 60
            self.duplicate = {"at": local, "ip": src_ip}
            if first:
                self._log("error", f"DUPLICATE NODE ID: another device ({src_ip}) also calls itself {self.id}; "
                                   "fix /etc/phosphor2000/node.yaml on one of them")
            return
        if src_ip and sender:
            self.peer_ips[sender] = src_ip
        if t == "ping":
            if self.is_leader():
                recv_c = self.cluster_now(local)                     # kernel receive stamp
                send_local = self.now_fn() if self.now_fn else local   # actual send moment
                self.send(ClockSync.answer_ping(m, recv_c, self.cluster_now(max(send_local, local))), sender)
            return
        if t == "pong":
            if sender == self.election.current_leader_id():
                self.sync.on_pong(m, local)
            return
        if t == "state":
            self._apply_state(m, local)
            return
        if t == "status":
            if self.is_leader():
                self.peer_status[sender] = {**{k: v for k, v in m.items() if not k.startswith("_")}, "_rx": local}
            return
        if t == "beat":
            if self.is_leader():
                self._ingest_beat(m, local)
            return
        if t == "features":
            self.features = {k: v for k, v in m.items() if not k.startswith("_")}
            self.features_rx = local
            return
        if t == "cmd":
            if self.is_leader():
                try:
                    self.command(m["name"], m.get("args", {}), local, origin=sender)
                except CommandError as exc:
                    self._log("warning", f"forwarded command {m.get('name')} from {sender} rejected: {exc}")
            return
        if t == "offsets_report":
            if self.is_leader():
                self._merge_offsets(m.get("offsets", {}), m.get("versions", {}), local)
                changed = self._merge_admin(m.get("admin"), local)
                changed = self._merge_cal_history(m.get("cal_history") or []) or changed
                if changed:
                    self._broadcast_state(local)
            return
        if t == "log":
            if self.on_log_lines:
                self.on_log_lines(sender, m.get("lines") or [])
            return
        if t == "cal_tap":
            if self.is_leader():
                self._cal_tap(float(m["cluster"]), int(m.get("value", 1)), local)
            return
        if t in ("update_cmd", "update_rollback"):
            if self.on_update_message:
                self.on_update_message(m, local)
            return
        if t == "restart_req":
            if m.get("to") == self.id and self.on_restart_request:
                self.on_restart_request(m.get("what", "renderer"))
            return
        self.election.on_message(m, local)

    on_restart_request: Callable[[str], None] | None = None
    now_fn: Callable[[], float] | None = None   # real runtime: time.monotonic (simulation: None)
    on_wifi_country: Callable[[str], bool] | None = None
    on_update_message: Callable[[dict, float], None] | None = None
    on_log_lines: Callable[[str, list], None] | None = None
    on_cal_history: Callable[[list], None] | None = None
    applied_country: str | None = None

    def _apply_site(self) -> None:
        """Apply replicated site settings locally (idempotent)."""
        cc = self.shared.site.wifi_country
        if cc and cc != self.applied_country and self.on_wifi_country:
            ok = self.on_wifi_country(cc)
            self._log("info" if ok else "error", f"Wi-Fi country {cc} {'applied' if ok else 'could not be applied'}")
            if ok:
                self.applied_country = cc

    # ================================================================== audio
    def ingest_local_audio(self, est: dict, local: float) -> None:
        """From the local audio service; times are *local monotonic* of this Pi."""
        self.local_audio = {**est, "_rx": local}
        if not est.get("live"):
            return
        if not self.sync.synced and not self.is_leader():
            return  # cannot convert to cluster time yet
        m = {"t": "beat", "src": self.id, "prio": self.cfg.node.priority, "bpm": est["bpm"],
             "beat_time": self.cluster_now(est["beat_local"]), "confidence": est.get("confidence", 0.0),
             "phase_unc_ms": est.get("phase_unc_ms"), "sync_unc_ms": self.sync.stats(local).uncertainty_ms}
        if self.is_leader():
            self._ingest_beat(m, local)
        elif self.election.current_leader_id():
            self.send(m, self.election.current_leader_id())

    def ingest_local_features(self, feat: dict, local: float) -> None:
        msg = {"t": "features", **feat, "cluster": self.cluster_now(feat.get("local", local)), "src": self.id}
        msg.pop("local", None)
        self.features = {k: v for k, v in msg.items() if k != "t"}
        self.features_rx = local
        if self.election.connected():
            self.send(msg, None)

    def _ingest_beat(self, m: dict, local: float) -> None:
        src = m.get("src") or m.get("_from")
        self.audio_estimates[src] = {**m, "_rx": local}
        # choose source: keep current while fresh, else highest priority fresh
        fresh = {k: v for k, v in self.audio_estimates.items() if local - v["_rx"] < AUDIO_STALE_S}
        if self.audio_src not in fresh:
            self.audio_src = max(fresh, key=lambda k: (fresh[k].get("prio", 0), k)) if fresh else None
        if src != self.audio_src or self.manual:
            return
        conf = float(m.get("confidence", 0))
        if conf < 0.25:
            return
        now_c = self.cluster_now(local)
        tl = self.timeline
        bpm = float(m["bpm"])
        if not (self.shared.timeline.min_bpm <= bpm <= self.shared.timeline.max_bpm):
            return
        bt = float(m["beat_time"])
        k = round(tl.beat_at(bt))
        target = Timeline(bpm=bpm, anchor_time=bt, anchor_beat=float(k), beats_per_bar=tl.beats_per_bar)
        delta = target.beat_at(now_c) - tl.beat_at(now_c)       # within ±0.5 beat by choice of k
        err_ms = delta * tl.period() * 1000.0
        locked = tl.source == TimingSource.LIVE
        # Second smoothing stage on top of the audio PLL: deadband + partial correction keeps
        # the replicated timeline quiet (few revisions) while tracking real tempo changes.
        if locked and abs(err_ms) < LIVE_DEADBAND_MS and abs(bpm - tl.bpm) < 0.02:
            self.timeline = replace(tl, live_at=now_c, confidence=conf)
            return
        if locked and abs(delta) < self.shared.timeline.slew_threshold_beats and abs(bpm - tl.bpm) < 2.0:
            new_bpm = tl.bpm + LIVE_TEMPO_GAIN * (bpm - tl.bpm)
            target = tl.with_tempo(now_c, new_bpm).shifted(now_c, LIVE_PHASE_GAIN * delta)
        r = reconcile(tl, replace(target, source=TimingSource.LIVE), now_c, self.shared.timeline.slew_threshold_beats,
                      self.shared.timeline.slew_duration_beats)
        self.timeline = replace(r.timeline, revision=tl.revision + 1, epoch=tl.epoch, source=TimingSource.LIVE,
                                confidence=conf, live_at=now_c, seed=tl.seed, beats_per_bar=tl.beats_per_bar)
        self.reconcile_info = {"mode": r.mode, "delta_beats": r.delta_beats, "at_cluster": now_c}
        if local - self.last_state_sent >= 0.25 or r.mode == "deferred" or not locked:
            self._broadcast_state(local)

    # =============================================================== commands
    def command(self, name: str, args: dict[str, Any], local: float, origin: str | None = None) -> dict:
        """Admin/keyboard commands. Followers forward to the leader."""
        if name in LOCAL_COMMANDS:
            return getattr(self, "_cmd_" + name)(args, local)
        if not self.is_leader():
            leader = self.election.current_leader_id()
            if not leader:
                raise CommandError("no leader reachable; command not applied")
            self.send({"t": "cmd", "name": name, "args": args}, leader)
            return {"forwarded_to": leader}
        fn = getattr(self, "_cmd_" + name, None)
        if fn is None or name not in LEADER_COMMANDS:
            raise CommandError(f"unknown command {name}")
        res = fn(args, local)
        self._broadcast_state(local)
        self.dirty = True
        self._log("info", f"command {name} {args if len(str(args)) < 200 else ''} by {origin or 'local'}")
        return res or {"ok": True}

    def _bump_timeline(self, tl: Timeline) -> None:
        self.timeline = replace(tl, revision=self.timeline.revision + 1, epoch=self.timeline.epoch)

    def _cmd_set_bpm(self, a: dict, local: float) -> dict:
        bpm = float(a["bpm"])
        if not (self.shared.timeline.min_bpm <= bpm <= self.shared.timeline.max_bpm):
            raise CommandError("bpm out of range")
        now_c = self.cluster_now(local)
        self.manual = True
        self._bump_timeline(self.timeline.with_tempo(now_c, bpm, source=TimingSource.MANUAL, confidence=1.0))
        return {"bpm": bpm}

    def _cmd_tap(self, a: dict, local: float) -> dict:
        # tap times come from the operator's device; only intervals are used
        t = float(a.get("t", local))
        bpm = self.tap.tap(t)
        if bpm and self.shared.timeline.min_bpm <= bpm <= self.shared.timeline.max_bpm:
            now_c = self.cluster_now(local)
            self.manual = True
            # tap also sets phase: the last tap is a beat (latency of the operator device ignored)
            tl = self.timeline.with_tempo(now_c, round(bpm, 2), source=TimingSource.MANUAL, confidence=0.8)
            if a.get("phase", True):
                b = tl.beat_at(now_c)
                tl = tl.shifted(now_c, round(b) - b)
            self._bump_timeline(tl)
        return {"bpm": bpm, "taps": len(self.tap.taps)}

    def _cmd_nudge(self, a: dict, local: float) -> dict:
        ms = float(a["ms"])
        if abs(ms) > 500:
            raise CommandError("nudge limited to ±500 ms")
        now_c = self.cluster_now(local)
        # +ms = beats happen later (timeline delayed)
        self._bump_timeline(self.timeline.shifted(now_c, -ms / 1000.0 / self.timeline.period()))
        return {"nudged_ms": ms}

    def _cmd_downbeat(self, a: dict, local: float) -> dict:
        now_c = self.cluster_now(local)
        b = self.timeline.beat_at(now_c)
        bpb = self.timeline.beats_per_bar
        nearest = round(b)
        shift = (math.ceil(nearest / bpb) * bpb - nearest) % bpb
        self._bump_timeline(self.timeline.shifted(now_c, shift))
        return {"shift_beats": shift}

    def _cmd_manual(self, a: dict, local: float) -> dict:
        self.manual = bool(a["enabled"])
        now_c = self.cluster_now(local)
        src = TimingSource.MANUAL if self.manual else TimingSource.PREDICTED
        self._bump_timeline(replace(self.timeline.rebased(now_c), source=src))
        return {"manual": self.manual}

    def _quant_start(self, quantize: str | None, local: float) -> float:
        q = quantum_beats(quantize or self.shared.scenes.quantize, self.timeline.beats_per_bar)
        now_c = self.cluster_now(local)
        b, _ = self.timeline.next_grid_time(now_c, q, lead=self.shared.timeline.schedule_lead_s)
        return b

    def _cmd_scene(self, a: dict, local: float) -> dict:
        scene = a["scene"]
        if scene not in PLAYABLE_SCENES:
            raise CommandError("unknown scene")
        targets = a.get("targets")
        if targets is not None:
            targets = [str(x) for x in targets]
        start = self._quant_start(a.get("quantize"), local)
        dur = a.get("bars")
        end = start + float(dur) * self.timeline.beats_per_bar if dur else None
        self.schedule.add_override(Override(scene, start, end, targets), self.timeline.beat_at(self.cluster_now(local)))
        return {"start_beat": start, "end_beat": end}

    def _cmd_clear_scene(self, a: dict, local: float) -> dict:
        start = self._quant_start(a.get("quantize"), local)
        self.schedule.clear_overrides(start, a.get("targets"))
        return {"at_beat": start}

    def _set_shared(self, new: SharedConfig, local: float, why: str) -> None:
        old_scenes = self.shared.scenes
        self.shared = new
        self.shared_rev += 1
        if new.scenes != old_scenes:
            start = self._quant_start("bar", local)
            self.schedule.set_scenes(new.scenes, start)
        self.dirty = True
        self._log("info", f"shared config rev {self.shared_rev}: {why}")

    def _cmd_set_shared(self, a: dict, local: float) -> dict:
        merged = self.shared.model_dump()
        for section, values in a.get("patch", {}).items():
            if section not in merged or not isinstance(values, dict):
                raise CommandError(f"unknown section {section}")
            if section == "calibration" and "offsets_ms" in values:
                raise CommandError("use calibration commands to change offsets")
            merged[section].update(values)
        try:
            new = SharedConfig.model_validate(merged)
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self._set_shared(new, local, "admin patch " + ",".join(a.get("patch", {}).keys()))
        return {"shared_rev": self.shared_rev}

    def _cmd_import_shared(self, a: dict, local: float) -> dict:
        try:
            new = SharedConfig.model_validate(a["shared"])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        wall_ms = int(self.wall_clock() * 1000)
        for nid in new.calibration.offsets_ms:
            self.offset_versions[nid] = [wall_ms, self.id]
        self._set_shared(new, local, "import")
        return {"shared_rev": self.shared_rev}

    def _cmd_handover(self, a: dict, local: float) -> dict:
        target = str(a["target"])
        audio_online = [pid for pid, p in self.election.peers.items()
                        if p.priority >= AUDIO_BONUS and local - p.last_seen < 10]
        if self.has_audio:
            audio_online.insert(0, self.id)
        tp = self.election.peers.get(target)
        if audio_online and (tp is None or tp.priority < AUDIO_BONUS):
            raise CommandError(f"audio node {audio_online[0]} is online and always leads; "
                               "a handover to a node without audio would be reverted")
        ok = self.election.request_handover(target, local)
        if not ok:
            raise CommandError("handover target not reachable or not eligible")
        return {"handover_to": a["target"]}

    def _cmd_set_admin_hash(self, a: dict, local: float) -> dict:
        h = str(a["hash"])
        if not re.fullmatch(HASH_RE, h):
            raise CommandError("invalid password hash")
        self.admin = {"hash": h, "ver": [int(self.wall_clock() * 1000), self.id]}
        self.dirty = True
        self._log("warning", "admin password changed (all sessions on all Pis logged out)")
        return {"ok": True}

    def _cmd_set_wall_time(self, a: dict, local: float) -> dict:
        wall = float(a["unix"])
        if not 946684800 <= wall <= 4102444800:
            raise CommandError("implausible time")
        self.wall_offset = wall - self.cluster_now(local)
        self.wall_offset_set = True
        return {"wall_offset": self.wall_offset}

    def _cmd_restart(self, a: dict, local: float) -> dict:
        target, what = str(a["target"]), str(a.get("what", "renderer"))
        if what not in ("renderer", "core", "audio", "reboot"):
            raise CommandError("invalid restart target")
        if target == self.id:
            if self.on_restart_request:
                self.on_restart_request(what)
        else:
            self.send({"t": "restart_req", "to": target, "what": what}, target)
        return {"restart": what, "target": target}

    # ------------------------------------------------------------ calibration
    def _cmd_cal_start(self, a: dict, local: float) -> dict:
        mode = a.get("mode", "single")
        if mode not in ("single", "all", "click"):
            raise CommandError("bad mode")
        target = a.get("target")
        if mode == "single" and not target:
            raise CommandError("target required")
        bpm = float(a.get("bpm", self.shared.calibration.bpm))
        if not 40 <= bpm <= 180:
            raise CommandError("calibration bpm must be 40..180 (flash rate <= 3 Hz)")
        now_c = self.cluster_now(local)
        cur = self.shared.calibration.offsets_ms.get(target, 0.0) if target else 0.0
        self.cal = CalSession(active=True, mode=mode, target=target, bpm=bpm,
                              origin=math.ceil(now_c + 0.5), trial_offset_ms=cur if target else None,
                              started_offset_ms=cur, rev=self.cal.rev + 1)
        self.cal_filter = TapFilter(60.0 / bpm, self.shared.calibration.double_press_fraction)
        return self.cal.to_dict()

    def _cal_tap(self, cluster_t: float, value: int, local: float) -> None:
        if self.cal.active and self.cal.mode == "single" and self.cal_filter is None:
            # session continues after a leader change: rebuild the filter from the replicated taps
            self.cal_filter = TapFilter(60.0 / self.cal.bpm, self.shared.calibration.double_press_fraction)
            self.cal_filter.accepted = list(self.cal.taps)
            self.cal_filter.last = self.cal.taps[-1] if self.cal.taps else None
        if not self.cal.active or self.cal.mode != "single" or self.cal_filter is None:
            return
        if not self.cal_filter.offer(cluster_t, value):
            return
        maxn = self.shared.calibration.max_taps
        self.cal.taps = self.cal_filter.accepted[-maxn:]
        self._cal_analyse()
        self.cal.rev += 1
        self._broadcast_state(local)

    def _cal_analyse(self) -> None:
        # The flash the operator sees is shown at scheduled + *applied* (trial) offset, so analyse
        # against the uncorrected grid and compare with the offset that was in effect.
        applied = self.cal.trial_offset_ms or 0.0
        res = analyse_taps(self.cal.taps, self.cal.origin, self.cal.bpm, applied,
                           self.shared.calibration.min_taps, self.shared.calibration.outlier_ms)
        self.cal.result = res.to_dict()

    def _cmd_cal_tap(self, a: dict, local: float) -> dict:
        """Tap from the admin browser or a keyboard on the leader itself (cluster time)."""
        self._cal_tap(float(a["cluster"]), int(a.get("value", 1)), local)
        return {"taps": len(self.cal.taps)}

    def _cmd_cal_adjust(self, a: dict, local: float) -> dict:
        if not self.cal.active or not self.cal.target:
            raise CommandError("no single-CRT calibration active")
        if "delta_ms" in a:
            d = float(a["delta_ms"])
            if abs(d) > 100:
                raise CommandError("step too large")
            new = (self.cal.trial_offset_ms or 0.0) + d
        elif a.get("apply_suggestion") and self.cal.result and self.cal.result.get("suggested_offset_ms") is not None:
            new = float(self.cal.result["suggested_offset_ms"])
        else:
            new = float(a["offset_ms"])
        self.cal.trial_offset_ms = max(-500.0, min(500.0, new))
        # taps recorded with the old offset are no longer comparable
        self.cal.taps = []
        if self.cal_filter:
            self.cal_filter = TapFilter(self.cal_filter.period, self.cal_filter.double_press_fraction)
        self.cal.result = None
        self.cal.rev += 1
        return {"trial_offset_ms": self.cal.trial_offset_ms}

    def _cmd_cal_save(self, a: dict, local: float) -> dict:
        if not self.cal.active or not self.cal.target or self.cal.trial_offset_ms is None:
            raise CommandError("nothing to save")
        nid, val = self.cal.target, round(self.cal.trial_offset_ms, 2)
        offs = dict(self.shared.calibration.offsets_ms)
        offs[nid] = val
        self.offset_versions[nid] = [int(self.wall_clock() * 1000), self.id]
        self._set_shared(replace_calibration(self.shared, offsets_ms=offs), local, f"calibration saved {nid}={val} ms")
        self.cal.started_offset_ms = val
        self.cal.trial_offset_ms = val
        self.cal.rev += 1
        r = self.cal.result or {}
        wall = self.wall_clock()
        self._merge_cal_history([{
            "id": f"{int(wall * 1000)}-{nid}", "wall": wall, "node": nid, "offset_ms": val, "by": self.id,
            "detail": {k: r.get(k) for k in ("n_used", "n_rejected", "mean_ms", "circ_std_ms", "std_err_ms", "confidence")},
        }])
        return {"saved": {nid: val}}

    def _cmd_cal_cancel(self, a: dict, local: float) -> dict:
        self.cal = CalSession(rev=self.cal.rev + 1, bpm=self.cal.bpm)
        self.cal_filter = None
        return {"cancelled": True}

    def _cmd_cal_reset(self, a: dict, local: float) -> dict:
        nid = a.get("target") or self.cal.target
        if not nid:
            raise CommandError("target required")
        offs = dict(self.shared.calibration.offsets_ms)
        offs.pop(nid, None)
        self.offset_versions[nid] = [int(self.wall_clock() * 1000), self.id]
        self._set_shared(replace_calibration(self.shared, offsets_ms=offs), local, f"calibration reset {nid}")
        if self.cal.active and self.cal.target == nid:
            self.cal.trial_offset_ms = 0.0
            self.cal.taps = []
            self.cal.result = None
            self.cal.rev += 1
        return {"reset": nid}

    def _cmd_cal_select(self, a: dict, local: float) -> dict:
        return self._cmd_cal_start({"mode": "single", "target": a["target"], "bpm": a.get("bpm")} if a.get("bpm")
                                   else {"mode": "single", "target": a["target"]}, local)

    # ----- local commands (keyboard on any node) -----
    def _cmd_key_tap(self, a: dict, local: float) -> dict:
        """Keyboard event from the local input service, ``local`` = evdev monotonic time."""
        ev_local = float(a.get("local", local))
        cluster_t = self.cluster_now(ev_local)
        if self.is_leader():
            self._cal_tap(cluster_t, int(a.get("value", 1)), local)
        elif self.election.current_leader_id():
            self.send({"t": "cal_tap", "cluster": cluster_t, "value": int(a.get("value", 1))},
                      self.election.current_leader_id())
        return {"cluster": cluster_t}

    def _cmd_renderer_report(self, a: dict, local: float) -> dict:
        self.renderer = {k: a[k] for k in ("fps", "frame_ms", "scene", "dropped", "browser_offset_ms",
                                           "browser_rtt_ms", "ua", "refresh_hz", "beat_frames",
                                           "event_err_ms", "event_err_p95_ms") if k in a}
        self.renderer["_rx"] = local
        return {}

    # ================================================================ outputs
    def status(self, local: float) -> dict:
        st = self.sync.stats(local)
        now_c = self.cluster_now(local)
        seg = self.current_segment(now_c)
        la = self.local_audio or {}
        return {
            "id": self.id, "priority": self.cfg.node.priority, "effective_priority": self.election.priority,
            "state": self.state.value,
            "leader": self.election.current_leader_id(), "epoch": self.timeline.epoch,
            "uptime": local - (self.started_local or local),
            "sync": {"offset_ms": None if self.is_leader() else round((st.offset - 0) * 1000, 3),
                     "rtt_ms": _r(st.rtt_ms), "jitter_ms": _r(st.jitter_ms),
                     "uncertainty_ms": _r(st.uncertainty_ms), "skew_ppm": _r(st.skew_ppm),
                     "samples": st.samples, "last_sample_age": _r(st.last_sample_age), "steps": st.steps,
                     "synced": st.synced},
            "timeline": {"bpm": round(self.timeline.bpm, 3), "beat": round(self.timeline.beat_at(now_c), 3),
                         "source": self.timeline.source.value, "confidence": round(self.timeline.confidence, 3),
                         "revision": self.timeline.revision, "reconcile": self.reconcile_info},
            "scene": seg.scene if seg else None,
            "visual_offset_ms": self.visual_offset_ms(),
            "wifi_country": self.applied_country,
            "version": self.version,
            "alerts": self.alerts(local),
            "audio": {"has_device": self.has_audio, "live": bool(la.get("live")), "bpm": la.get("bpm"),
                      "confidence": la.get("confidence"), "rms": la.get("rms"), "state": la.get("state"),
                      "age": _r(local - la["_rx"]) if la else None},
            "renderer": {k: v for k, v in self.renderer.items() if not k.startswith("_")},
            "renderer_age": _r(local - self.renderer["_rx"]) if "_rx" in self.renderer else None,
            "radio": self.election.adapter.status().mode.value,
        }

    def current_segment(self, now_c: float):
        b = self.timeline.beat_at(now_c)
        segs = plan(self.schedule, self.id, b, b + 0.25)
        return segs[0] if segs else None

    def render_state(self, local: float, ahead_beats: float = 256.0) -> dict:
        """Everything the browser renderer needs; it extrapolates locally from this."""
        now_c = self.cluster_now(local)
        b = self.timeline.beat_at(now_c)
        segs = plan(self.schedule, self.id, b - 8, b + ahead_beats)
        st = self.sync.stats(local)
        feat_age = None if self.features_rx is None else local - self.features_rx
        sh = self.shared
        cal = self.cal.to_dict()
        cal["taps"] = len(self.cal.taps)
        return {
            "t": "render_state",
            "node": self.id, "state": self.state.value, "leader": self.election.current_leader_id(),
            "cluster_now": now_c,
            "clock_step_epoch": self.sync.step_epoch,
            "timeline": self.timeline.to_dict(),
            "segments": [s.to_dict() for s in segs],
            "schedule_rev": self.schedule.revision,
            "visual_offset_ms": self.visual_offset_ms(),
            "display_latency_ms": sh.timeline.display_latency_ms,
            "wall_offset": self.wall_offset,
            "sync": {"uncertainty_ms": _r(st.uncertainty_ms), "jitter_ms": _r(st.jitter_ms), "synced": st.synced},
            "audio_fresh": feat_age is not None and feat_age < FEATURES_STALE_S,
            "scenes": {"intensity": sh.scenes.intensity, "countdown_target": sh.scenes.countdown_target,
                       "countdown_label": sh.scenes.countdown_label, "phosphor": sh.scenes.phosphor},
            "safety": sh.safety.model_dump(),
            "display": sh.display.model_dump(),
            "cal": cal,
            "version": self.version,
            "alerts": self.alerts(local),
        }

    # ================================================================== cache
    def cache_blob(self, boot_id: str, local: float) -> dict:
        return {"v": 1, "boot_id": boot_id, "saved_local": local, "snapshot": self.snapshot(),
                "sync": {"base": self.sync.base, "t0": self.sync.t0, "skew": self.sync.skew,
                         "synced": self.sync.synced},
                "epoch_seen": self.election.epoch_seen,
                "peers": {k: {"priority": p.priority} for k, p in self.election.peers.items()},
                "yield_count": self.election.yield_count,
                "admin": self.admin, "cal_history": self.cal_history[-CAL_HISTORY_MAX:]}

    def restore_cache(self, blob: dict, boot_id: str, local: float) -> None:
        """Restore replicated state after a process restart or reboot."""
        snap = blob.get("snapshot") or {}
        try:
            self.shared = SharedConfig.model_validate(snap["shared"])
            self.shared_rev = int(snap.get("shared_rev", 0))
            self.offset_versions = dict(snap.get("offset_versions", {}))
            self.schedule = ScheduleState.from_dict(snap["schedule"])
            tl = Timeline.from_dict(snap["timeline"])
        except Exception as exc:
            self._log("error", f"cache unusable, starting from defaults: {exc}")
            return
        self.election.epoch_seen = max(self.election.epoch_seen, int(blob.get("epoch_seen", 0)))
        self._merge_admin(blob.get("admin"), local)
        self._merge_cal_history(blob.get("cal_history") or [])
        self.manual = bool(snap.get("manual", False))
        same_boot = blob.get("boot_id") == boot_id
        if same_boot and blob.get("sync", {}).get("synced"):
            s = blob["sync"]
            self.sync.base, self.sync.t0, self.sync.skew, self.sync.synced = s["base"], s["t0"], s["skew"], True
            self.timeline = replace(tl, source=TimingSource.MANUAL if self.manual else TimingSource.PREDICTED)
            self.wall_offset = float(snap.get("wall_offset", 0.0))
            self._log("info", "restored cache from this boot: clock offset and beat phase continue exactly")
        else:
            # After a reboot monotonic time restarted; keep tempo/schedule/offsets, beat position
            # continues from the saved beat (phase unknown until we rejoin a leader).
            saved_beat = tl.beat_at(tl.anchor_time) if not blob.get("saved_local") else \
                tl.beat_at(blob.get("saved_local", 0) + blob.get("sync", {}).get("base", 0))
            self.timeline = Timeline(bpm=tl.bpm, anchor_time=self.cluster_now(local),
                                     anchor_beat=float(math.ceil(saved_beat / tl.beats_per_bar) * tl.beats_per_bar),
                                     beats_per_bar=tl.beats_per_bar, confidence=0.0,
                                     source=TimingSource.MANUAL if self.manual else TimingSource.PREDICTED,
                                     revision=tl.revision, epoch=tl.epoch, seed=tl.seed)
            self.wall_offset = self.wall_clock() - self.cluster_now(local)
            self._log("info", "restored cache from previous boot: tempo, schedule, seeds and offsets kept")
        self.restored_from_cache = True
        self.dirty = False


LEADER_COMMANDS = {"set_admin_hash", "set_bpm", "tap", "nudge", "downbeat", "manual", "scene", "clear_scene", "set_shared",
                   "import_shared", "handover", "set_wall_time", "restart", "cal_start", "cal_tap", "cal_adjust",
                   "cal_save", "cal_cancel", "cal_reset", "cal_select"}
LOCAL_COMMANDS = {"key_tap", "renderer_report"}


def replace_calibration(shared: SharedConfig, **kw) -> SharedConfig:
    cal = shared.calibration.model_copy(update=kw)
    return shared.model_copy(update={"calibration": cal})


def _r(x: float | None, nd: int = 3) -> float | None:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return round(x, nd)
