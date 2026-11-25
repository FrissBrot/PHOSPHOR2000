#!/bin/bash
# Prepare NetworkManager for PHOSPHOR2000 AP/station switching on wlan0.
#
#   sudo scripts/configure-network.sh --country DE        # apply
#   sudo scripts/configure-network.sh --status
#   sudo scripts/configure-network.sh --rollback           # restore NM connections as before first run
#   options: --disable-other-wifi  (set autoconnect=no on other Wi-Fi profiles; reversible via rollback)
#
# What it does (idempotent):
#   * backs up /etc/NetworkManager/system-connections and conf.d (first backup kept forever)
#   * sets the Wi-Fi regulatory country (required by Pi OS before wlan0 may transmit)
#   * writes two keyfiles (0600): phosphor-ap (WPA2-PSK/CCMP AP, 192.168.50.1/24, shared = DHCP)
#     and phosphor-sta (station). Both autoconnect=no: only phosphor-core switches them.
#   * disables Wi-Fi power saving on wlan0 (jitter)
# It never touches Ethernet: keep eth0 for SSH as the recovery path.
# Emergency: create /boot/firmware/phosphor-safe-mode on the SD card (any PC) and the
# core will not touch the radio at all after the next boot.
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

COUNTRY=""
ACTION=apply
DISABLE_OTHER=0
NM_DIR=${PHOS_NM_DIR:-/etc/NetworkManager}
while [ $# -gt 0 ]; do
  case $1 in
    --country) COUNTRY=${2^^}; shift ;;
    --status) ACTION=status ;;
    --rollback) ACTION=rollback ;;
    --disable-other-wifi) DISABLE_OTHER=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) die "unknown option $1" ;;
  esac
  shift
done

PY=${PHOS_PYTHON:-$PHOS_PREFIX/venv/bin/python}
[ -x "$PY" ] || PY=python3

status() {
  nmcli general status || true
  echo "--- devices ---"; nmcli -f DEVICE,TYPE,STATE,CONNECTION device || true
  echo "--- connections ---"; nmcli -f NAME,TYPE,AUTOCONNECT,DEVICE connection show || true
  echo "--- phosphor profiles (secrets hidden) ---"
  for c in phosphor-ap phosphor-sta; do
    nmcli -f connection.id,802-11-wireless.mode,802-11-wireless.ssid,802-11-wireless.band,802-11-wireless.channel,ipv4.method,ipv4.addresses,wifi-sec.key-mgmt,wifi-sec.proto,connection.autoconnect connection show "$c" 2>/dev/null || echo "$c: missing"
  done
  echo "--- rfkill ---"; rfkill list 2>/dev/null || true
  echo "--- regulatory ---"; iw reg get 2>/dev/null | head -n 3 || true
}

apply() {
  need_root
  command -v nmcli >/dev/null || die "NetworkManager (nmcli) not installed"
  systemctl is-active --quiet NetworkManager || die "NetworkManager is not running (Pi OS Bookworm+ uses it by default)"
  [ -f "$PHOS_ETC/secrets.yaml" ] || die "$PHOS_ETC/secrets.yaml missing (run install.sh first)"
  local bk
  bk=$(backup_files network "$NM_DIR/system-connections" "$NM_DIR/conf.d")
  c_info "backup: $bk"

  if [ -n "$COUNTRY" ]; then
    [[ $COUNTRY =~ ^[A-Z]{2}$ ]] || die "--country must be a 2-letter ISO code (e.g. DE)"
    if command -v raspi-config >/dev/null; then
      run raspi-config nonint do_wifi_country "$COUNTRY"
    else
      run iw reg set "$COUNTRY" || true
    fi
    c_ok "Wi-Fi country $COUNTRY"
  else
    c_warn "no --country given: Wi-Fi may stay blocked (rfkill) until a country is set"
  fi
  run rfkill unblock wifi || true

  install -d -m 0755 "$NM_DIR/conf.d"
  install -m 0644 "$PHOS_PREFIX/systemd/nm-phosphor.conf" "$NM_DIR/conf.d/90-phosphor.conf"

  # keyfiles from the typed config (PSK never on a command line)
  ( cd "$PHOS_PREFIX" && PHOSPHOR_ETC="$PHOS_ETC" run "$PY" -m backend.network.nmcli --dir "$NM_DIR/system-connections" )
  run nmcli connection reload
  c_ok "profiles phosphor-ap / phosphor-sta installed (autoconnect off)"

  if [ "$DISABLE_OTHER" = 1 ]; then
    local name
    while IFS=: read -r name type; do
      [ "$type" = 802-11-wireless ] || continue
      case $name in phosphor-ap|phosphor-sta) continue ;; esac
      run nmcli connection modify "$name" connection.autoconnect no
      c_info "autoconnect disabled for '$name'"
    done < <(nmcli -t -f NAME,TYPE connection show)
  else
    if nmcli -t -f NAME,TYPE,AUTOCONNECT connection show | grep -q ':802-11-wireless:yes$'; then
      c_warn "other Wi-Fi profiles have autoconnect=yes and may fight for wlan0; consider --disable-other-wifi"
    fi
  fi
  run systemctl reload NetworkManager || true
}

case $ACTION in
  status) status ;;
  apply) apply ;;
  rollback)
    need_root
    b=$(original_backup network) || true
    [ -n "${b:-}" ] || die "no network backup found"
    run nmcli connection down phosphor-ap 2>/dev/null || true
    run nmcli connection down phosphor-sta 2>/dev/null || true
    restore_backup "$b"
    rm -f "$NM_DIR/system-connections/phosphor-ap.nmconnection" "$NM_DIR/system-connections/phosphor-sta.nmconnection" \
          "$NM_DIR/conf.d/90-phosphor.conf"
    run nmcli connection reload || true
    c_ok "network configuration restored"
    ;;
esac
