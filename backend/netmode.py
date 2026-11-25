"""Cluster-wide network mode changes from the dashboard, with automatic fallback.

The leader publishes ``site.net_mode`` (+ Wi-Fi SSID, password sealed with the cluster key) with
a new ``net_rev``. Every Pi applies it 15 s later (so that all Pis received it before the
network changes) through the root oneshot ``phosphor-netmode.service``. That script saves the
previous mode in ``netmode-trial.json``. After the switch the core must see its cluster again
within 3 minutes — otherwise it requests the previous mode (fallback) and reports the failed
revision; the leader then cancels the request so nothing loops.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from backend.cluster.messages import CodecError, unseal

if TYPE_CHECKING:
    from backend.services.core import Core

log = logging.getLogger("phos.netmode")
APPLY_DELAY = 15.0


class NetModeManager:
    def __init__(self, core: "Core", state_dir: Path, run_dir: Path) -> None:
        self.core = core
        self.node = core.node
        self.state_dir = state_dir
        self.run_dir = run_dir
        self.applied_rev = self._read("netmode-applied.json").get("rev", 0)
        self.failed_rev = self._read("netmode-failed.json").get("rev", 0)
        self.due: float | None = None
        self.start_unit = None            # injectable for tests
        self.requests: list[dict] = []

    def _read(self, name: str) -> dict:
        try:
            return json.loads((self.state_dir / name).read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, name: str, data: dict) -> None:
        p = self.state_dir / name
        tmp = p.with_name(name + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(p)

    def _request(self, req: dict) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        p = self.run_dir / "netmode-request.json"
        tmp = p.with_name("netmode-request.json.tmp")
        tmp.touch(mode=0o600)
        tmp.write_text(json.dumps(req))
        tmp.replace(p)
        self.requests.append({k: v for k, v in req.items() if k != "wlan_psk"})
        if self.start_unit:
            self.start_unit()
            return
        from backend.services.system import start_unit
        start_unit("phosphor-netmode.service")

    def status(self) -> dict:
        trial = self._read("netmode-trial.json")
        return {"mode": self.core.cfg.network.mode, "applied_rev": self.applied_rev, "failed_rev": self.failed_rev,
                "trial": {"target": trial.get("target"), "deadline": trial.get("deadline")} if trial else None}

    def tick(self, now: float) -> None:
        site = self.node.shared.site
        # 1. a new request from the leader
        if site.net_mode and site.net_rev > self.applied_rev and site.net_rev != self.failed_rev:
            if self.due is None:
                self.due = now + APPLY_DELAY
                self.node._log("warning", f"network mode change to '{site.net_mode}' in {APPLY_DELAY:.0f} s")
            elif now >= self.due:
                self.due = None
                self._apply(site)
        # 2. trial after a switch: did we find the cluster again?
        trial = self._read("netmode-trial.json")
        if trial:
            peers = [p for p in self.node.election.peers.values() if now - p.last_seen < 10]
            has_addr = bool(self.core.ifaces.addrs)
            ok = bool(peers) or (trial.get("peers_before", 0) == 0 and has_addr)
            if ok:
                (self.state_dir / "netmode-trial.json").unlink(missing_ok=True)
                self.node._log("info", f"network mode '{self.core.cfg.network.mode}' confirmed")
            elif time.time() > float(trial.get("deadline", 0)):
                (self.state_dir / "netmode-trial.json").unlink(missing_ok=True)
                self.failed_rev = int(trial.get("rev", 0))
                self._write("netmode-failed.json", {"rev": self.failed_rev})
                prev = trial.get("previous") or {}
                self.node._log("error", f"network mode '{trial.get('target')}' did not reach the cluster within "
                                        f"3 min: falling back to '{prev.get('mode')}'")
                self._request({"mode": prev.get("mode", "hotspot"), "wlan_ssid": prev.get("wlan_ssid", ""),
                               "wlan_psk": prev.get("wlan_psk", ""), "rev": self.failed_rev, "fallback": True})
        # 3. leader: cancel a request that failed somewhere (prevents switching loops)
        if self.node.is_leader() and site.net_mode:
            failed = [pid for pid, st in self.node.peer_status.items() if st.get("net_failed_rev") == site.net_rev]
            if failed or self.failed_rev == site.net_rev:
                self.node.command("cancel_network_mode", {"reason": ", ".join(failed) or self.node.id}, now)

    def _apply(self, site) -> None:
        cfg = self.core.cfg.network
        psk = ""
        if site.net_wlan_psk_sealed:
            try:
                psk = unseal(self.core.cfg.secrets.cluster_key, site.net_wlan_psk_sealed)
            except (CodecError, ValueError):
                self.node._log("error", "network mode request: Wi-Fi password could not be decrypted")
                return
        self.applied_rev = site.net_rev
        self._write("netmode-applied.json", {"rev": self.applied_rev})
        same = site.net_mode == cfg.mode and (site.net_mode != "wlan" or
                                               (site.net_wlan_ssid == cfg.wlan_ssid and not psk))
        if same:
            return
        peers_before = sum(1 for p in self.node.election.peers.values() if time.monotonic() - p.last_seen < 10)
        self._request({"mode": site.net_mode, "wlan_ssid": site.net_wlan_ssid, "wlan_psk": psk,
                       "rev": site.net_rev, "peers_before": peers_before})
