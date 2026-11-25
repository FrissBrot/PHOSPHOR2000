"""Software version shown per Pi in the dashboard (written by install.sh into VERSION)."""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def software_version() -> str:
    f = ROOT / "VERSION"
    try:
        v = f.read_text().strip()
        if v:
            return v[:40]
    except OSError:
        pass
    try:  # development checkout
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=2)
        if out.returncode == 0:
            dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True,
                                   text=True, timeout=2).stdout.strip()
            return out.stdout.strip() + ("+dev" if dirty else "")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"
