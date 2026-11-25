#!/bin/bash
# Party-proofing: hardware watchdog + SD-card protection against power cuts.
#
#   sudo scripts/configure-robustness.sh               # watchdog + write-minimised system (default)
#   sudo scripts/configure-robustness.sh --readonly    # additionally: read-only root (RAM overlay)
#   sudo scripts/configure-robustness.sh --readonly-off
#   sudo scripts/configure-robustness.sh --status
#   sudo scripts/configure-robustness.sh --rollback    # remove everything this script changed
#
# Watchdog: the Pi's hardware watchdog (bcm2835-wdt) is fed by systemd. If the kernel or systemd
#   hangs, the SoC resets itself after ~14 s and the Pi boots straight back into the show.
# Write-minimised: journal in RAM, no swap file, Chromium profile in RAM (/run). During the show the
#   SD card is only written when a configuration/calibration change is saved (atomic + fsync).
# --readonly: Raspberry Pi OS overlay mode: the root file system becomes a RAM overlay, nothing is
#   ever written to the card during operation. Saved settings/offsets are copied to the real card by
#   phosphor-persist.service (brief remount rw, ~100 ms) only when they change. Software updates
#   require --readonly-off + reboot first.
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
MARK_BEGIN="# >>> phosphor2000-robustness >>>"
MARK_END="# <<< phosphor2000-robustness <<<"

ACTION=apply
case ${1:-} in
  --readonly) ACTION=readonly ;;
  --readonly-off) ACTION=readonly_off ;;
  --status) ACTION=status ;;
  --rollback) ACTION=rollback ;;
  ""|--apply) ;;
  -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
  *) die "unknown option $1" ;;
esac
BOOT=$(boot_dir)
CONFIG=$BOOT/config.txt
SYSD=${PHOS_SYSTEMD_DIR:-/etc/systemd}

strip() { [ -f "$1" ] && sed -i "/^$MARK_BEGIN\$/,/^$MARK_END\$/d" "$1"; return 0; }

apply() {
  need_root
  local bk
  bk=$(backup_files robustness "$CONFIG")
  c_info "backup: $bk"
  # hardware watchdog
  strip "$CONFIG"
  printf '%s\n[all]\ndtparam=watchdog=on\n%s\n' "$MARK_BEGIN" "$MARK_END" >> "$CONFIG"
  install -d "$SYSD/system.conf.d" "$SYSD/journald.conf.d"
  printf '[Manager]\n# feed the BCM2835 hardware watchdog; reset the Pi if systemd/kernel hang\nRuntimeWatchdogSec=14s\nRebootWatchdogSec=2min\n' \
    > "$SYSD/system.conf.d/90-phosphor-watchdog.conf"
  # journal in RAM only
  printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=32M\n' > "$SYSD/journald.conf.d/90-phosphor.conf"
  # no swap on the SD card (Bookworm: dphys-swapfile; Trixie: rpi-swap / systemd swap units)
  for u in dphys-swapfile.service rpi-swap.service; do run systemctl disable --now "$u" 2>/dev/null || true; done
  run swapoff -a 2>/dev/null || true
  run systemctl daemon-reload || true
  run systemctl restart systemd-journald || true
  c_ok "hardware watchdog (active after reboot), volatile journal, swap off"
}

readonly_on() {
  need_root
  apply
  install -m 0644 "$PHOS_PREFIX/systemd/phosphor-persist.service" /etc/systemd/system/phosphor-persist.service
  run systemctl daemon-reload
  if command -v raspi-config >/dev/null; then
    run raspi-config nonint do_overlayfs 0     # 0 = enable (raspi-config convention)
    c_ok "read-only root enabled — takes effect after reboot; boot partition stays writable"
  else
    die "raspi-config not found: read-only overlay mode needs Raspberry Pi OS"
  fi
}

readonly_off() {
  need_root
  command -v raspi-config >/dev/null || die "raspi-config not found"
  run raspi-config nonint do_overlayfs 1       # 1 = disable
  c_ok "read-only root disabled — reboot, then update/install, then --readonly again"
}

status() {
  echo "root fs: $(findmnt -n -o FSTYPE /)  (overlay = read-only mode active)"
  echo "watchdog: $(cat /sys/class/watchdog/watchdog0/state 2>/dev/null || echo 'no /dev/watchdog0 (reboot after enabling?)')"
  systemctl show -p RuntimeWatchdogUSec 2>/dev/null
  echo "journal: $(grep -h Storage "$SYSD"/journald.conf.d/*.conf 2>/dev/null || echo default)"
  echo "swap: $(swapon --noheadings 2>/dev/null | wc -l) active"
  grep -A3 "$MARK_BEGIN" "$CONFIG" 2>/dev/null || true
}

rollback() {
  need_root
  strip "$CONFIG"
  rm -f "$SYSD/system.conf.d/90-phosphor-watchdog.conf" "$SYSD/journald.conf.d/90-phosphor.conf" \
        /etc/systemd/system/phosphor-persist.service
  command -v raspi-config >/dev/null && run raspi-config nonint do_overlayfs 1 || true
  run systemctl enable dphys-swapfile.service 2>/dev/null || true
  run systemctl daemon-reload || true
  c_ok "robustness settings removed (reboot)"
}

case $ACTION in
  apply) apply ;; readonly) readonly_on ;; readonly_off) readonly_off ;; status) status ;; rollback) rollback ;;
esac
