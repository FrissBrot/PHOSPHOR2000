#!/bin/bash
# Read-only health report for one Pi. Secrets are never printed.
#   sudo scripts/diagnostics.sh [--json-only]
PHOS_PREFIX=${PHOS_PREFIX:-/opt/phosphor2000}
HERE=$(dirname "$0")
sec() { printf '\n\033[32m=== %s ===\033[0m\n' "$*"; }

sec "system"
tr -d '\0' < /proc/device-tree/model 2>/dev/null; echo
grep -E 'PRETTY_NAME|VERSION_CODENAME' /etc/os-release
uname -a
uptime
command -v vcgencmd >/dev/null && { vcgencmd measure_temp; vcgencmd get_throttled; }
free -m
df -h / | tail -n1

sec "display (composite)"
"$HERE/configure-crt.sh" --status 2>/dev/null || true

sec "network"
"$HERE/configure-network.sh" --status 2>/dev/null || true
ip -br addr
for f in /boot/firmware/phosphor-safe-mode /boot/phosphor-safe-mode /etc/phosphor2000/safe-mode; do
  [ -e "$f" ] && echo "SAFE MODE FLAG PRESENT: $f (radio not managed)"
done

sec "services"
for u in phosphor-core phosphor-audio phosphor-input phosphor-renderer phosphor-firewall; do
  printf '%-24s %s\n' "$u" "$(systemctl is-active $u 2>/dev/null)"
done
ss -lntup 2>/dev/null | grep -E ':8080|:47800' || true

sec "core health"
curl -fsS --max-time 2 http://127.0.0.1:8080/api/health && echo || echo "core not reachable"

sec "audio devices"
arecord -l 2>/dev/null || echo "arecord not available"
grep -l . /proc/asound/card*/usbid 2>/dev/null | sed 's/^/usb card: /'

sec "input devices"
grep -E '^N: |^H: ' /proc/bus/input/devices | paste - - | grep -i kbd || echo "no keyboard"

sec "configuration (secrets masked)"
cat /etc/phosphor2000/node.yaml 2>/dev/null
[ -f /etc/phosphor2000/secrets.yaml ] && sed -E 's/:.*/: ***/' /etc/phosphor2000/secrets.yaml
ls -l /etc/phosphor2000/ 2>/dev/null

sec "recent errors"
journalctl -u 'phosphor-*' -p warning -n 40 --no-pager 2>/dev/null || true
