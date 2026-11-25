#!/bin/bash
# Prepare NetworkManager for PHOSPHOR2000 AP/station switching on wlan0.
#
#   sudo scripts/configure-network.sh --country DE        # apply
#   sudo scripts/configure-network.sh --status
#   sudo scripts/configure-network.sh --rollback           # restore NM connections as before first run
#   options: --disable-other-wifi  (set autoconnect=no on other Wi-Fi profiles; reversible via rollback)
#            --mode hotspot|wlan|ethernet   network mode (default: keep the configured one)
#            --wlan-ssid SSID --wlan-psk-file FILE   existing Wi-Fi for mode "wlan"
#
# Modes:
#   hotspot  : leader runs the PHOSPHOR2000 AP (192.168.50.1), the others join it
#   wlan     : every Pi joins an existing Wi-Fi (DHCP + fixed cluster address 10.20.0.1x)
#   ethernet : every Pi on one switch (DHCP + fixed 10.20.0.1x); the leader also runs the admin AP
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
MODE=""
WLAN_SSID=""
WLAN_PSK_FILE=""
ACTION=apply
DISABLE_OTHER=0
NM_DIR=${PHOS_NM_DIR:-/etc/NetworkManager}
while [ $# -gt 0 ]; do
  case $1 in
    --country) COUNTRY=${2^^}; shift ;;
    --status) ACTION=status ;;
    --rollback) ACTION=rollback ;;
    --disable-other-wifi) DISABLE_OTHER=1 ;;
    --mode) MODE=$2; shift ;;
    --wlan-ssid) WLAN_SSID=$2; shift ;;
    --wlan-psk-file) WLAN_PSK_FILE=$2; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
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

  # network mode (node.yaml / secrets.yaml), validated by the typed config
  if [ -n "$MODE" ] || [ -n "$WLAN_SSID" ] || [ -n "$WLAN_PSK_FILE" ]; then
    case ${MODE:-hotspot} in hotspot|wlan|ethernet) ;; *) die "--mode must be hotspot, wlan or ethernet" ;; esac
    ( cd "$PHOS_PREFIX" && PHOSPHOR_ETC="$PHOS_ETC" "$PY" - "$MODE" "$WLAN_SSID" "$WLAN_PSK_FILE" <<'PY'
import sys
from pathlib import Path
from backend.api.auth import update_secrets
from backend.config import ETC_DIR, NetworkConfig, Secrets, update_node_yaml
mode, ssid, pskfile = sys.argv[1:4]
net = {}
if mode:
    net["mode"] = mode
if ssid:
    net["wlan_ssid"] = ssid
NetworkConfig(**net)                       # validation (raises on bad values)
if net:
    update_node_yaml(ETC_DIR / "node.yaml", {"network": net})
if pskfile:
    psk = Path(pskfile).read_text().strip()
    Secrets(wlan_psk=psk)                  # validation
    update_secrets(ETC_DIR / "secrets.yaml", wlan_psk=psk)
PY
    ) || die "invalid network mode settings"
    chgrp phosphor "$PHOS_ETC/node.yaml" 2>/dev/null || true
    chown phosphor:phosphor "$PHOS_ETC/secrets.yaml" 2>/dev/null || true
  fi
  CUR_MODE=$(cd "$PHOS_PREFIX" && PHOSPHOR_ETC="$PHOS_ETC" "$PY" -c 'from backend.config import load_config; print(load_config().network.mode)' 2>/dev/null || echo hotspot)

  # keyfiles from the typed config (PSK never on a command line)
  ( cd "$PHOS_PREFIX" && PHOSPHOR_ETC="$PHOS_ETC" run "$PY" -m backend.network.nmcli --dir "$NM_DIR/system-connections" )
  run nmcli connection reload
  c_ok "profiles installed for network mode '$CUR_MODE'"
  case $CUR_MODE in
    hotspot)
      run nmcli connection down phosphor-wlan 2>/dev/null || true
      run nmcli connection down phosphor-eth 2>/dev/null || true ;;
    wlan)
      run nmcli connection down phosphor-ap 2>/dev/null || true
      run nmcli connection down phosphor-sta 2>/dev/null || true
      run nmcli connection up phosphor-wlan || c_warn "Wi-Fi not reachable right now; NetworkManager keeps trying" ;;
    ethernet)
      run nmcli connection down phosphor-sta 2>/dev/null || true
      run nmcli connection down phosphor-wlan 2>/dev/null || true
      run nmcli connection up phosphor-eth || c_warn "no cable/link right now; NetworkManager keeps trying" ;;
  esac

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
