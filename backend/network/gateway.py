"""Internet sharing from ANY Pi with a cable uplink.

* every Pi detects whether it has a default route over its Ethernet cable and whether the package
  mirror is reachable through it ("cable internet")
* the leader picks one gateway: itself if it has cable internet, else the highest-priority Pi that
  has it; the choice is sticky while that Pi keeps working (no flapping)
* the gateway Pi forwards + masquerades cluster traffic to its cable (root oneshot
  phosphor-gateway@on); in hotspot mode a leader-gateway needs nothing extra (the NetworkManager
  "shared" hotspot already NATs)
* every other Pi gets a default route (metric 700: its own cable would still win) via the gateway's
  cluster address and the gateway's upstream DNS servers, applied with ``nmcli device reapply``
  (no reconnect)
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess


def _run(argv: list[str], timeout: float = 4) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def cable_default_route(eth: str, runner=_run) -> str | None:
    """Router address if the default route goes over the Ethernet interface."""
    if not shutil.which("ip") and runner is _run:
        return None
    try:
        routes = json.loads(runner(["ip", "-j", "-4", "route", "show", "default"]) or "[]")
    except ValueError:
        return None
    for r in routes:
        if r.get("dev") == eth and r.get("gateway"):
            return r["gateway"]
    return None


def cable_dns(eth: str, router: str | None, runner=_run) -> list[str]:
    out = runner(["nmcli", "-g", "IP4.DNS", "device", "show", eth]) if (shutil.which("nmcli") or runner is not _run) else ""
    dns = [d.strip() for d in re.split(r"[|\s]+", out) if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", d.strip())]
    if not dns and router:
        dns = [router]            # most routers also answer DNS
    return dns[:3]


def choose_gateway(leader_id: str, candidates: dict[str, dict], current: str | None) -> str | None:
    """candidates: node -> {"priority", "ip", "dns"} for Pis with working cable internet."""
    if not candidates:
        return None
    if current in candidates:
        return current            # sticky: never move a working gateway
    if leader_id in candidates:
        return leader_id
    return max(candidates, key=lambda k: (candidates[k].get("priority", 0), k))


def route_plan(mode: str, is_leader: bool, me: str, gw: dict | None) -> tuple[str, str | None, list[str]]:
    """What this Pi configures on its cluster connection:
    ("none", None, [])            no internet via the cluster (or this Pi is the gateway itself)
    ("dhcp", None, [])            hotspot follower, gateway = hotspot leader: use the DHCP router
    ("static", ip, dns)           default route via another Pi's cluster address
    """
    if not gw or gw.get("node") == me:
        return ("none", None, [])
    if mode == "hotspot" and not is_leader and gw.get("leader"):
        return ("dhcp", None, [])
    return ("static", gw.get("ip"), list(gw.get("dns") or []))
