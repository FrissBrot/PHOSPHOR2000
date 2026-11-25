#!/bin/bash
# Root oneshot (phosphor-apt.service): Raspberry Pi OS package updates, requested from the dashboard.
# Needs internet (Ethernet on this Pi, or the leader's shared internet in hotspot mode).
# Status for the dashboard: /run/phosphor2000/apt-status.json
set -uo pipefail
RUN=${PHOS_RUN:-/run/phosphor2000}
STATUS=$RUN/apt-status.json
export DEBIAN_FRONTEND=noninteractive
status() {   # state message [upgraded] [reboot]
  python3 - "$STATUS" "$1" "$2" "${3:-0}" "${4:-false}" <<'PY'
import json, os, sys, time
p, state, msg, n, reboot = sys.argv[1:6]
tmp = p + ".tmp"
json.dump({"state": state, "message": msg, "upgraded": int(n), "reboot_required": reboot == "true",
           "at": time.time()}, open(tmp, "w"))
os.chmod(tmp, 0o644); os.replace(tmp, p)
PY
  echo "[apt] $1: $2"
}
install -d -m 0755 "$RUN"
if [ "$(findmnt -n -o FSTYPE / 2>/dev/null)" = overlay ]; then
  status error "read-only system: package updates would be lost - run configure-robustness.sh --readonly-off first"
  exit 1
fi
status running "apt-get update"
if ! timeout 300 apt-get update -q >/tmp/phos-apt.log 2>&1; then
  status error "package lists could not be downloaded (no internet?)"; exit 1
fi
PKGS=$(apt-get -s -q upgrade 2>/dev/null | awk '/^Inst /{print $2}')
N=$(echo -n "$PKGS" | grep -c . || true)
if [ "$N" = 0 ]; then status done "system is up to date" 0 false; exit 0; fi
status running "upgrading $N packages"
if ! timeout 3600 apt-get -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade \
     >>/tmp/phos-apt.log 2>&1; then
  status error "apt-get upgrade failed (see /tmp/phos-apt.log)" "$N"; exit 1
fi
apt-get -y -q autoclean >/dev/null 2>&1 || true
REBOOT=false
if [ -e /run/reboot-required ] || echo "$PKGS" | grep -Eq '^(linux-image|raspberrypi-kernel|raspi-firmware|raspberrypi-bootloader|libc6|systemd|udev|firmware-brcm80211)'; then
  REBOOT=true
fi
status done "$N packages upgraded$([ "$REBOOT" = true ] && echo ' - reboot recommended')" "$N" "$REBOOT"
