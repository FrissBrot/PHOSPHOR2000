#!/bin/bash
# Remove PHOSPHOR2000 and restore the original system configuration.
#   sudo scripts/uninstall.sh            # keep /etc/phosphor2000 and state (offsets, logs)
#   sudo scripts/uninstall.sh --purge    # also delete config, secrets, state, user
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
need_root
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1
HERE=$(dirname "$0")

systemctl disable --now phosphor.target phosphor-core.service phosphor-audio.service \
  phosphor-input.service phosphor-renderer.service phosphor-firewall.service 2>/dev/null || true
"$HERE/configure-kiosk.sh" --remove || true
if [ -n "$(original_backup crt || true)" ]; then "$HERE/configure-crt.sh" --rollback; else "$HERE/configure-crt.sh" --remove || true; fi
if [ -n "$(original_backup network || true)" ]; then "$HERE/configure-network.sh" --rollback; fi
nft delete table inet phosphor 2>/dev/null || true
rm -f /etc/systemd/system/phosphor*.service /etc/systemd/system/phosphor-country@.service /etc/systemd/system/phosphor.target \
      /etc/polkit-1/rules.d/50-phosphor.rules /usr/local/sbin/phosphor-passwd
systemctl daemon-reload
if [ "$PURGE" = 1 ]; then
  rm -rf "$PHOS_ETC" "$PHOS_STATE" "$PHOS_PREFIX"
  userdel phosphor 2>/dev/null || true
  c_ok "purged"
else
  rm -rf "$PHOS_PREFIX"
  c_ok "removed; kept $PHOS_ETC and $PHOS_STATE (offsets, backups, logs)"
fi
c_info "reboot to return to the HDMI console"
