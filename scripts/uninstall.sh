#!/bin/bash
# Remove CYBERSPACE 2000 and restore the original system configuration.
#   sudo scripts/uninstall.sh            # keep /etc/cyberspace2000 and state (offsets, logs)
#   sudo scripts/uninstall.sh --purge    # also delete config, secrets, state, user
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
need_root
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1
HERE=$(dirname "$0")

systemctl disable --now cyberspace.target cyberspace-core.service cyberspace-audio.service \
  cyberspace-input.service cyberspace-renderer.service cyberspace-firewall.service 2>/dev/null || true
"$HERE/configure-kiosk.sh" --remove || true
if [ -n "$(original_backup crt || true)" ]; then "$HERE/configure-crt.sh" --rollback; else "$HERE/configure-crt.sh" --remove || true; fi
if [ -n "$(original_backup network || true)" ]; then "$HERE/configure-network.sh" --rollback; fi
nft delete table inet cyberspace 2>/dev/null || true
rm -f /etc/systemd/system/cyberspace*.service /etc/systemd/system/cyberspace.target \
      /etc/polkit-1/rules.d/50-cyberspace.rules /usr/local/sbin/cyberspace-passwd
systemctl daemon-reload
if [ "$PURGE" = 1 ]; then
  rm -rf "$CS2K_ETC" "$CS2K_STATE" "$CS2K_PREFIX"
  userdel cyberspace 2>/dev/null || true
  c_ok "purged"
else
  rm -rf "$CS2K_PREFIX"
  c_ok "removed; kept $CS2K_ETC and $CS2K_STATE (offsets, backups, logs)"
fi
c_info "reboot to return to the HDMI console"
