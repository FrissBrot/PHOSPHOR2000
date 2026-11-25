#!/bin/bash
# Root oneshot (phosphor-netmode.service): switch the network mode as requested from the dashboard.
# Request: /run/phosphor2000/netmode-request.json  {mode, wlan_ssid, wlan_psk, rev, fallback}
# Before switching, the current mode is saved in a trial file; if the core does not see its
# cluster again within 3 minutes it requests the previous mode (automatic fallback).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/lib.sh"
REQ=${1:-/run/phosphor2000/netmode-request.json}
STATE=${PHOS_STATE:-/var/lib/phosphor2000}
PY="$PHOS_PREFIX/venv/bin/python"
cd "$PHOS_PREFIX"
eval "$("$PY" - "$REQ" "$STATE" <<'PY'
import json, shlex, sys, time
from pathlib import Path
from backend.config import NetworkConfig, Secrets, load_config
req = json.load(open(sys.argv[1]))
mode = req.get("mode")
assert mode in ("hotspot", "wlan", "ethernet"), "bad mode"
ssid = str(req.get("wlan_ssid") or "")
psk = str(req.get("wlan_psk") or "")
NetworkConfig(mode=mode, wlan_ssid=ssid or "x")
if psk:
    Secrets(wlan_psk=psk)
cur = load_config()
state = Path(sys.argv[2])
if not req.get("fallback"):
    trial = {"rev": int(req.get("rev", 0)), "deadline": time.time() + 180,
             "previous": {"mode": cur.network.mode, "wlan_ssid": cur.network.wlan_ssid, "wlan_psk": cur.secrets.wlan_psk},
             "peers_before": int(req.get("peers_before", 0)), "target": mode}
    p = state / "netmode-trial.json"
    p.write_text(json.dumps(trial))
    p.chmod(0o600)
print(f"MODE={shlex.quote(mode)} SSID={shlex.quote(ssid)}")
pskfile = Path("/run/phosphor2000/.wlanpsk")
if psk:
    pskfile.write_text(psk); pskfile.chmod(0o600)
    print(f"PSKFILE={shlex.quote(str(pskfile))}")
else:
    print("PSKFILE=''")
PY
)"
chown phosphor:phosphor "$STATE/netmode-trial.json" 2>/dev/null || true
args=(--mode "$MODE")
[ -n "$SSID" ] && args+=(--wlan-ssid "$SSID")
[ -n "$PSKFILE" ] && args+=(--wlan-psk-file "$PSKFILE")
"$PHOS_PREFIX/scripts/configure-network.sh" "${args[@]}" || true
rm -f /run/phosphor2000/.wlanpsk
systemctl restart phosphor-core.service
