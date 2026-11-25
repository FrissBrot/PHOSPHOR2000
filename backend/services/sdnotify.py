"""Minimal sd_notify (no dependency): READY=1 and WATCHDOG=1 for systemd Type=notify units.

With ``WatchdogSec=`` in the unit, systemd kills and restarts the service when the heartbeat
stops — this catches *hangs* (deadlocked event loop, stuck thread), not only crashes.
"""
from __future__ import annotations

import os
import socket


def notify(state: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(state.encode(), addr)
        return True
    except OSError:
        return False
