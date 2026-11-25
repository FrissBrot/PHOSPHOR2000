#!/bin/bash
# Run as root by phosphor-persist.service when the root file system is a read-only RAM overlay:
# copy the PHOSPHOR2000 state (settings, offsets, calibration history) to the real SD card.
# The lower file system is writable for ~100 ms only.
set -euo pipefail
STATE=/var/lib/phosphor2000
[ "$(findmnt -n -o FSTYPE /)" = overlay ] || { echo "root is not an overlay: nothing to do"; exit 0; }
LOWER=""
for d in /media/root-ro /run/rootfsbase /overlay/lower; do
  if mountpoint -q "$d" 2>/dev/null; then LOWER=$d; break; fi
done
[ -n "$LOWER" ] || { echo "lower root not found" >&2; exit 1; }
exec 9>/run/phosphor-persist.lock
flock 9
mount -o remount,rw "$LOWER"
trap 'sync; mount -o remount,ro "$LOWER"' EXIT
install -d -o phosphor -g phosphor -m 0750 "$LOWER$STATE"
for f in state.json calibrations.sqlite3; do
  if [ -f "$STATE/$f" ]; then
    cp -a "$STATE/$f" "$LOWER$STATE/$f.tmp"
    sync "$LOWER$STATE/$f.tmp"
    mv -f "$LOWER$STATE/$f.tmp" "$LOWER$STATE/$f"
  fi
done
echo "persisted to $LOWER$STATE"
