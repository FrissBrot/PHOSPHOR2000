#!/bin/bash
# Set the Wi-Fi regulatory country now (iw) and for the next boots (kernel cmdline).
# Run as root by phosphor-country@CC.service; triggered from the admin dashboard.
#   set-wifi-country.sh CH
set -euo pipefail
CC=${1:-}
[[ $CC =~ ^[A-Z]{2}$ ]] || { echo "invalid country '$CC'" >&2; exit 2; }
CUR=$(iw reg get 2>/dev/null | awk '/^country/ {sub(":","",$2); print $2; exit}')
if [ "$CUR" != "$CC" ]; then
  iw reg set "$CC"
  echo "regulatory domain $CUR -> $CC"
fi
rfkill unblock wifi 2>/dev/null || true
BOOT=${PHOS_BOOT_DIR:-$([ -f /boot/firmware/cmdline.txt ] && echo /boot/firmware || echo /boot)}
CMD=$BOOT/cmdline.txt
if [ -f "$CMD" ] && ! grep -q "cfg80211.ieee80211_regdom=$CC\b" "$CMD"; then
  line=$(head -n1 "$CMD" | tr ' ' '\n' | grep -v '^cfg80211.ieee80211_regdom=' | tr '\n' ' ' | sed 's/ *$//')
  echo "$line cfg80211.ieee80211_regdom=$CC" > "$CMD"
  echo "persisted in $CMD"
fi
