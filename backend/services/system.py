"""Whitelisted system actions. No shell, no user-supplied argv.

The core runs as the unprivileged user ``cyberspace``. scripts/install.sh adds
a sudoers drop-in that allows exactly the argv lists below (and nothing else).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("cs2k.system")

ACTIONS: dict[str, list[str]] = {
    "renderer": ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "cyberspace-renderer.service"],
    "audio": ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "cyberspace-audio.service"],
    "core": ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "cyberspace-core.service"],
    "reboot": ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "reboot"],
}


def run_action(what: str, dry_run: bool | None = None) -> bool:
    argv = ACTIONS.get(what)
    if argv is None:
        raise ValueError("unknown action")
    if dry_run is None:
        dry_run = os.environ.get("CYBERSPACE_DRY_RUN") == "1"
    if dry_run:
        log.warning("dry-run: would execute %s", argv)
        return True
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env={"PATH": "/usr/bin:/bin"})
        return True
    except OSError as exc:
        log.error("action %s failed: %s", what, exc)
        return False


def read_metrics() -> dict:
    """CPU load, memory, temperature and throttling, from /proc and /sys only."""
    m: dict = {}
    try:
        m["load1"] = float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])
        m["mem_total_mb"] = round(info["MemTotal"] / 1024)
        m["mem_used_mb"] = round((info["MemTotal"] - info.get("MemAvailable", 0)) / 1024)
    except (OSError, ValueError, KeyError):
        pass
    try:
        m["temp_c"] = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        pass
    vc = shutil.which("vcgencmd")
    if vc:
        try:
            out = subprocess.run([vc, "get_throttled"], capture_output=True, text=True, timeout=2).stdout
            m["throttled"] = out.strip().split("=")[-1]
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        m["uptime_s"] = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError):
        pass
    return m


class CpuSampler:
    def __init__(self) -> None:
        self.prev: tuple[int, int] | None = None

    def sample(self) -> float | None:
        try:
            f = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        except (OSError, ValueError):
            return None
        idle, total = f[3] + f[4], sum(f)
        res = None
        if self.prev:
            di, dt = idle - self.prev[0], total - self.prev[1]
            res = round(100.0 * (1 - di / dt), 1) if dt > 0 else None
        self.prev = (idle, total)
        return res
