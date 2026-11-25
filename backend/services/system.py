"""Whitelisted system actions. No shell, no user-supplied argv.

The core runs as the unprivileged user ``phosphor`` with NoNewPrivileges.
scripts/install.sh installs a polkit rule (systemd/50-phosphor.rules) that
lets exactly this user restart exactly these units and reboot — nothing else.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("phos.system")

ACTIONS: dict[str, list[str]] = {
    "renderer": ["/usr/bin/systemctl", "--no-ask-password", "restart", "phosphor-renderer.service"],
    "audio": ["/usr/bin/systemctl", "--no-ask-password", "restart", "phosphor-audio.service"],
    "core": ["/usr/bin/systemctl", "--no-ask-password", "restart", "phosphor-core.service"],
    "reboot": ["/usr/bin/systemctl", "--no-ask-password", "reboot"],
}


COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


def set_wifi_country(cc: str, dry_run: bool | None = None) -> bool:
    """Start phosphor-country@CC.service (root oneshot, allowed by polkit for this exact pattern)."""
    if not COUNTRY_RE.match(cc):
        raise ValueError("country must be two upper-case letters")
    argv = ["/usr/bin/systemctl", "--no-ask-password", "start", f"phosphor-country@{cc}.service"]
    if dry_run is None:
        dry_run = os.environ.get("PHOSPHOR_DRY_RUN") == "1" or not Path("/etc/systemd/system/phosphor-country@.service").exists()
    if dry_run:
        log.warning("dry-run: would execute %s", argv)
        return True
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                              env={"PATH": "/usr/bin:/bin"}).returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("set country failed: %s", exc)
        return False


def overlay_root_active() -> bool:
    """Root filesystem is a RAM overlay (raspi-config overlay mode, configure-robustness.sh --readonly)."""
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            f = line.split()
            if len(f) > 2 and f[1] == "/" and f[2] == "overlay":
                return True
    except OSError:
        pass
    return False


def request_persist() -> bool:
    """Ask the root oneshot phosphor-persist.service to copy state to the read-only lower root."""
    argv = ["/usr/bin/systemctl", "--no-ask-password", "--no-block", "start", "phosphor-persist.service"]
    if os.environ.get("PHOSPHOR_DRY_RUN") == "1":
        log.warning("dry-run: would execute %s", argv)
        return True
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env={"PATH": "/usr/bin:/bin"})
        return True
    except OSError as exc:
        log.error("persist request failed: %s", exc)
        return False


def request_update() -> bool:
    """Start the root oneshot phosphor-update.service (reads /run/phosphor2000/update-request.json)."""
    argv = ["/usr/bin/systemctl", "--no-ask-password", "--no-block", "start", "phosphor-update.service"]
    if os.environ.get("PHOSPHOR_DRY_RUN") == "1":
        log.warning("dry-run: would execute %s", argv)
        return True
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                              env={"PATH": "/usr/bin:/bin"}).returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("update request failed: %s", exc)
        return False


def run_action(what: str, dry_run: bool | None = None) -> bool:
    argv = ACTIONS.get(what)
    if argv is None:
        raise ValueError("unknown action")
    if dry_run is None:
        dry_run = os.environ.get("PHOSPHOR_DRY_RUN") == "1"
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
