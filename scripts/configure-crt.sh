#!/bin/bash
# Configure PAL (or NTSC) composite output for a 4:3 CRT on Raspberry Pi 3 B/B+.
#
#   sudo scripts/configure-crt.sh               # apply (auto-detect OS / KMS)
#   sudo scripts/configure-crt.sh --status      # show what is configured and what the kernel sees
#   sudo scripts/configure-crt.sh --rollback    # restore config.txt/cmdline.txt as before the FIRST run
#   sudo scripts/configure-crt.sh --rollback-last  # restore the state before the most recent run
#   sudo scripts/configure-crt.sh --remove      # remove our settings without a backup
#   options: --norm PAL|NTSC  --mode kms|legacy
#
# Raspberry Pi OS Bookworm / Trixie (KMS, vc4 driver):
#   config.txt : dtoverlay=vc4-kms-v3d,composite      (+ enable_tvout=1 on Pi 4/400)
#   cmdline.txt: video=Composite-1:720x576@50ie vc4.tv_norm=PAL
# Legacy firmware video (Bullseye and older without full KMS):
#   config.txt : sdtv_mode=2 sdtv_aspect=1 hdmi_ignore_hotplug=1
# Enabling composite disables HDMI on Pi 0-3. ALWAYS verify on the real Pi with --status:
# the connector must appear as /sys/class/drm/card*-Composite-1 with status "connected".
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

NORM=PAL
MODE=auto
ACTION=apply
while [ $# -gt 0 ]; do
  case $1 in
    --norm) NORM=${2^^}; shift ;;
    --mode) MODE=$2; shift ;;
    --status) ACTION=status ;;
    --rollback) ACTION=rollback ;;
    --rollback-last) ACTION=rollback-last ;;
    --remove) ACTION=remove ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown option $1" ;;
  esac
  shift
done
[ "$NORM" = PAL ] || [ "$NORM" = NTSC ] || die "--norm must be PAL or NTSC"

BOOT=$(boot_dir)
CONFIG=$BOOT/config.txt
CMDLINE=$BOOT/cmdline.txt
CODENAME=$(os_codename)
MODEL=$(pi_model)

if [ "$MODE" = auto ]; then
  case $CODENAME in
    bookworm|trixie|forky) MODE=kms ;;
    bullseye|buster|stretch) MODE=legacy ;;
    *) MODE=kms; c_warn "unknown OS '$CODENAME', assuming KMS" ;;
  esac
fi

if [ "$NORM" = PAL ]; then
  KMS_MODE="720x576@50ie"; SDTV=2
else
  KMS_MODE="720x480@60ie"; SDTV=0
fi
CMD_TOKENS="video=Composite-1:$KMS_MODE vc4.tv_norm=$NORM consoleblank=0 vt.global_cursor_default=0 logo.nologo"

status() {
  echo "OS codename : $CODENAME"
  echo "Model       : $MODEL"
  echo "Boot dir    : $BOOT"
  echo "--- config.txt (cyberspace block + vc4 lines) ---"
  grep -nE "vc4-|sdtv_|enable_tvout|hdmi_ignore|cyberspace|cs2k" "$CONFIG" 2>/dev/null || echo "(none)"
  echo "--- cmdline.txt ---"
  cat "$CMDLINE" 2>/dev/null || echo "(missing)"
  echo "--- kernel DRM connectors ---"
  local c found=0
  for c in /sys/class/drm/card*-*; do
    [ -e "$c/status" ] || continue
    found=1
    printf '%-40s %-12s %s\n' "$(basename "$c")" "$(cat "$c/status")" "$(tr '\n' ' ' < "$c/modes" 2>/dev/null)"
  done
  [ $found = 1 ] || echo "(no DRM connectors: legacy firmware video or not a Pi)"
  if command -v tvservice >/dev/null 2>&1; then echo "--- tvservice ---"; tvservice -s 2>&1 || true; fi
}

apply() {
  need_root
  [ -f "$CONFIG" ] || die "$CONFIG not found (CS2K_BOOT_DIR?)"
  [ -f "$CMDLINE" ] || die "$CMDLINE not found"
  case $MODEL in *"Pi 5"*) die "Pi 5 composite needs a different setup; this project targets Pi 3" ;; esac
  local bk
  bk=$(backup_files crt "$CONFIG" "$CMDLINE")
  c_info "backup: $bk"
  strip_block "$CONFIG"
  # comment out other display driver overlays (restored by --remove / --rollback)
  sed -i -E 's/^(dtoverlay=vc4-(f)?kms-v3d.*)$/#cs2k# \1/' "$CONFIG"
  {
    echo "$MARK_BEGIN"
    echo "# composite $NORM 4:3 for CYBERSPACE 2000 ($MODE mode) — managed by configure-crt.sh"
    echo "[all]"
    if [ "$MODE" = kms ]; then
      echo "dtoverlay=vc4-kms-v3d,composite"
    else
      echo "sdtv_mode=$SDTV"
      echo "sdtv_aspect=1"
      echo "hdmi_ignore_hotplug=1"
    fi
    case $MODEL in *"Pi 4"*|*"Pi 400"*) echo "enable_tvout=1" ;; esac
    echo "$MARK_END"
  } >> "$CONFIG"
  # cmdline.txt is ONE line: drop our tokens, then append the current ones
  local line t
  line=$(head -n1 "$CMDLINE")
  for t in video=Composite-1 vc4.tv_norm consoleblank vt.global_cursor_default logo.nologo; do
    line=$(echo "$line" | tr ' ' '\n' | grep -v "^$t\([=:]\|\$\)" | tr '\n' ' ')
  done
  line=$(echo "$line" | tr -s ' ' | sed 's/ *$//')
  if [ "$MODE" = kms ]; then
    echo "$line $CMD_TOKENS" > "$CMDLINE"
  else
    echo "$line consoleblank=0 vt.global_cursor_default=0 logo.nologo" > "$CMDLINE"
  fi
  c_ok "composite $NORM configured ($MODE). Reboot required; verify with: $0 --status"
}

remove() {
  need_root
  strip_block "$CONFIG"
  sed -i -E 's/^#cs2k# //' "$CONFIG"
  local line t
  line=$(head -n1 "$CMDLINE")
  for t in video=Composite-1 vc4.tv_norm consoleblank vt.global_cursor_default logo.nologo; do
    line=$(echo "$line" | tr ' ' '\n' | grep -v "^$t\([=:]\|\$\)" | tr '\n' ' ')
  done
  echo "$line" | tr -s ' ' | sed 's/ *$//' > "$CMDLINE"
  c_ok "composite settings removed (reboot to return to HDMI)"
}

case $ACTION in
  status) status ;;
  apply) apply ;;
  remove) remove ;;
  rollback|rollback-last)
    need_root
    if [ "$ACTION" = rollback ]; then b=$(original_backup crt) || true; else b=$(latest_backup crt) || true; fi
    [ -n "${b:-}" ] || die "no CRT backup found in $CS2K_BACKUP_DIR"
    restore_backup "$b"
    ;;
esac
