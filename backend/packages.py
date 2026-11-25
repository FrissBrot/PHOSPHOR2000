"""OS package updates (apt) from the dashboard: one Pi, or all Pis one after the other.

Each Pi runs ``phosphor-apt.service`` (root oneshot) and reports progress through its status
message, so the leader can roll through the cluster (least important first, leader last) and
the dashboard shows per Pi: running / done / error, number of packages, reboot recommended.
Only while no LIVE music was detected for the configured idle time (same rule as software
updates), unless the admin forces it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.services.core import Core

STEP_TIMEOUT = 3600.0


class PackageUpdater:
    def __init__(self, core: "Core", run_dir: Path) -> None:
        self.core = core
        self.node = core.node
        self.run_dir = run_dir
        self.start_unit = None
        self.requests = 0

    def job(self) -> dict:
        try:
            return json.loads((self.run_dir / "apt-status.json").read_text())
        except (OSError, ValueError):
            return {}

    def busy(self) -> bool:
        j = self.job()
        return j.get("state") == "running" and time.time() - j.get("at", 0) < STEP_TIMEOUT

    def run_local(self) -> None:
        if self.busy():
            raise RuntimeError("package update already running on this Pi")
        self.requests += 1
        self.node._log("warning", "OS package update started")
        if self.start_unit:
            self.start_unit()
            return
        from backend.services.system import start_unit
        start_unit("phosphor-apt.service")

    @property
    def rollout(self) -> dict | None:
        return self.node.extra.get("apt_rollout")

    @rollout.setter
    def rollout(self, v: dict | None) -> None:
        self.node.extra["apt_rollout"] = v

    def on_message(self, m: dict, local: float) -> None:
        if m.get("_from") != self.node.election.current_leader_id() or m.get("to") != self.node.id:
            return
        try:
            self.run_local()
        except RuntimeError as exc:
            self.node._log("warning", str(exc))

    def start_all(self, now: float, force: bool = False) -> dict:
        if not self.node.is_leader():
            raise RuntimeError("only the leader can update all Pis; open the dashboard at 192.168.50.1")
        if self.rollout and self.rollout.get("state") == "running":
            raise RuntimeError("a package rollout is already running")
        if not force and not self.core.updater.idle(now):
            raise RuntimeError("live music detected - package updates only in a music pause (or force)")
        prios = {pid: p.priority for pid, p in self.node.election.peers.items() if now - p.last_seen < 10}
        queue = sorted(prios, key=lambda k: (prios[k], k)) + [self.node.id]
        self.rollout = {"state": "running", "queue": queue, "done": [], "failed": [], "current": None,
                        "since": None, "message": "starting"}
        return self.rollout

    def tick(self, now: float) -> None:
        r = self.rollout
        if not r or r.get("state") != "running" or not self.node.is_leader():
            return
        c = self.node.cluster_now(now)
        cur = r["current"]
        if cur is not None:
            st = self._apt_status(cur)
            if st and st.get("state") in ("done", "error") and st.get("at", 0) >= r["since_wall"]:
                (r["done"] if st["state"] == "done" else r["failed"]).append(cur)
                r["current"] = None
                cur = None
            elif c - r["since"] > STEP_TIMEOUT:
                r["failed"].append(cur)
                r["current"] = None
                cur = None
            else:
                return
        if not r["queue"]:
            r.update(state="done", message=f"done: {', '.join(r['done']) or '-'}"
                                            f"{'; failed: ' + ', '.join(r['failed']) if r['failed'] else ''}")
            return
        nxt = r["queue"].pop(0)
        r.update(current=nxt, since=c, since_wall=time.time() - 5, message=f"updating packages on {nxt}")
        if nxt == self.node.id:
            try:
                self.run_local()
            except RuntimeError:
                pass
        else:
            self.node.send({"t": "apt_cmd", "to": nxt}, nxt)

    def _apt_status(self, nid: str) -> dict | None:
        if nid == self.node.id:
            return self.job()
        st = self.node.peer_status.get(nid) or {}
        return st.get("apt")
