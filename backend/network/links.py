"""Interface-level helpers for the three network modes.

* broadcast addresses of all IPv4 interfaces (cluster datagrams go out on every attached L2
  network: hotspot 192.168.50.255, fixed cluster net 10.20.0.255, venue DHCP subnet, ...;
  duplicates are dropped by the replay window)
* admin hotspot (ethernet mode: the leader opens the PHOSPHOR2000 AP for phones)
* mDNS alias ``phosphor.local`` for the leader (wlan / ethernet mode, needs avahi)
* internet reachability probe (for package/software updates)
"""
from __future__ import annotations

import json
import logging
import shutil
import socket
import subprocess
import threading
import time

log = logging.getLogger("phos.links")


def parse_ip_json(text: str) -> list[tuple[str, str, str]]:
    """[(ifname, address, broadcast)] for UP, non-loopback IPv4 addresses with a broadcast."""
    out = []
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return out
    for link in data:
        if "UP" not in link.get("flags", []) or link.get("ifname") == "lo":
            continue
        for a in link.get("addr_info", []):
            if a.get("family") == "inet" and a.get("broadcast"):
                out.append((link["ifname"], a["local"], a["broadcast"]))
    return out


class Interfaces:
    def __init__(self, fallback_broadcast: str, refresh_s: float = 10.0, runner=None,
                 only_iface: str | None = None) -> None:
        self.fallback = fallback_broadcast
        self.only_iface = only_iface
        self.refresh_s = refresh_s
        self.runner = runner or self._run
        self.addrs: list[tuple[str, str, str]] = []
        self.at = -1e9

    @staticmethod
    def _run() -> str:
        if not shutil.which("ip"):
            return "[]"
        try:
            return subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True, text=True,
                                  timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            return "[]"

    def refresh(self, now: float) -> None:
        if now - self.at >= self.refresh_s:
            self.at = now
            self.addrs = parse_ip_json(self.runner())

    def broadcasts(self) -> list[str]:
        """Cluster traffic stays on the cluster interface: a Pi that is also cabled to a home LAN
        must not talk PHOSPHOR on that LAN (other Pis there would look like a second cluster)."""
        if self.only_iface is None:
            return [self.fallback]
        b = {x[2] for x in self.addrs if x[0] == self.only_iface}
        return sorted(b) or [self.fallback]

    def address_of(self, ifname: str) -> str | None:
        for name, addr, _ in self.addrs:
            if name == ifname and not addr.startswith("10.20.0."):
                return addr
        for name, addr, _ in self.addrs:
            if name == ifname:
                return addr
        return None


class MdnsAlias:
    """Publish ``phosphor.local`` -> leader address via avahi (if installed)."""

    def __init__(self, name: str = "phosphor.local") -> None:
        self.name = name
        self.proc: subprocess.Popen | None = None
        self.addr: str | None = None

    def ensure(self, addr: str | None) -> None:
        if addr == self.addr and (self.proc is None or self.proc.poll() is None):
            return
        self.stop()
        exe = shutil.which("avahi-publish")
        if addr and exe:
            try:
                self.proc = subprocess.Popen([exe, "-a", "-R", self.name, addr], stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
                log.info("mDNS: %s -> %s", self.name, addr)
            except OSError:
                self.proc = None
        self.addr = addr

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None
        self.addr = None


class InternetProbe:
    """Can this Pi reach the package mirror? (TCP connect, in a thread, every `every` s)."""

    def __init__(self, host: str = "archive.raspberrypi.com", port: int = 80, every: float = 60.0) -> None:
        self.host, self.port, self.every = host, port, every
        self.ok: bool | None = None
        self.checked = -1e9
        self._busy = False

    def tick(self, now: float, wanted: bool) -> None:
        if not wanted or self._busy or now - self.checked < self.every:
            return
        self.checked = now
        self._busy = True

        def run() -> None:
            try:
                with socket.create_connection((self.host, self.port), timeout=4):
                    self.ok = True
            except OSError:
                self.ok = False
            finally:
                self._busy = False
        threading.Thread(target=run, daemon=True, name="inet-probe").start()

    def force(self) -> None:
        self.checked = -1e9
