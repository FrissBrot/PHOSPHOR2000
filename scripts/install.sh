#!/bin/bash
# PHOSPHOR2000 installer for Raspberry Pi OS (Bookworm/Trixie, 64-bit Lite recommended).
#
# One-time, on any machine (e.g. the first Pi):
#   scripts/install.sh --generate-secrets cluster-secrets.yaml
#   -> copy that file (USB stick / scp) to every Pi. It holds the Wi-Fi PSK and the cluster key.
#
# On every Pi (from a checkout of this repository):
#   sudo scripts/install.sh --node-id pi-01 --country DE --secrets cluster-secrets.yaml
#
# Options:
#   --node-id pi-0N        persistent node id (priority from table: pi-01=100 ... pi-05=60)
#   --priority N           override priority
#   --country CC           initial Wi-Fi regulatory country (Pi OS blocks Wi-Fi until one is set);
#                          later changes: admin dashboard -> Config -> Wi-Fi country
#   --secrets FILE         cluster secrets (wifi_psk, cluster_key, admin_password_hash)
#   --norm PAL|NTSC        composite norm (default PAL)
#   --no-network           do not configure NetworkManager (manage: false = external network mode)
#   --no-crt               do not touch config.txt/cmdline.txt
#   --no-kiosk             do not install the Chromium kiosk
#   --firewall             restrict ports 8080/47800 to the cluster subnet (nftables table)
#   --disable-other-wifi   set autoconnect=no on other Wi-Fi profiles
#   --readonly             read-only root file system (RAM overlay) — maximal power-cut safety;
#                          hardware watchdog + write-minimised system are always configured
# Re-running is safe (idempotent); every modified system file is backed up first.
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
SRC=$(cd "$(dirname "$0")/.." && pwd)

NODE_ID="" PRIORITY="" COUNTRY="" SECRETS="" NORM=PAL
DO_NET=1 DO_CRT=1 DO_KIOSK=1 FIREWALL=0 OTHERWIFI="" READONLY=0
GEN=""
while [ $# -gt 0 ]; do
  case $1 in
    --generate-secrets) GEN=$2; shift ;;
    --node-id) NODE_ID=$2; shift ;;
    --priority) PRIORITY=$2; shift ;;
    --country) COUNTRY=$2; shift ;;
    --secrets) SECRETS=$2; shift ;;
    --norm) NORM=$2; shift ;;
    --no-network) DO_NET=0 ;;
    --no-crt) DO_CRT=0 ;;
    --no-kiosk) DO_KIOSK=0 ;;
    --firewall) FIREWALL=1 ;;
    --disable-other-wifi) OTHERWIFI=--disable-other-wifi ;;
    --readonly) READONLY=1 ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) die "unknown option $1" ;;
  esac
  shift
done

if [ -n "$GEN" ]; then
  [ -e "$GEN" ] && die "$GEN exists; refusing to overwrite"
  umask 077
  psk=$(python3 -c 'import secrets,string;a=string.ascii_letters+string.digits;print("".join(secrets.choice(a) for _ in range(20)))')
  key=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
  printf 'wifi_psk: "%s"\ncluster_key: "%s"\n' "$psk" "$key" > "$GEN"
  chmod 600 "$GEN"
  # one admin password for the whole cluster (can be changed later in the dashboard)
  if [ -t 0 ]; then
    hash=$(python3 - <<'PY'
import base64, getpass, hashlib, os, sys
pw = getpass.getpass("admin dashboard password for ALL Pis (min. 8 chars): ")
if len(pw) < 8 or pw != getpass.getpass("repeat: "):
    sys.exit("passwords too short or different")
salt = os.urandom(16)
h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
b = lambda x: base64.urlsafe_b64encode(x).decode().rstrip("=")
print(f"scrypt${2**14}$8$1${b(salt)}${b(h)}")
PY
) || die "no admin password set"
    printf 'admin_password_hash: "%s"\n' "$hash" >> "$GEN"
  fi
  c_ok "wrote $GEN (Wi-Fi password: $psk). Copy it to every Pi; keep it private."
  exit 0
fi

need_root
[[ $NODE_ID =~ ^pi-0[1-9]$ ]] || die "--node-id pi-01..pi-09 required"
if [ "$(findmnt -n -o FSTYPE / 2>/dev/null)" = overlay ]; then
  die "root file system is read-only (overlay): run scripts/configure-robustness.sh --readonly-off, reboot, then install"
fi
case $(os_codename) in bookworm|trixie) ;; *) c_warn "untested OS '$(os_codename)'; continuing" ;; esac
case $(pi_model) in *"Raspberry Pi 3"*) ;; *) c_warn "model '$(pi_model)' is not a Pi 3; timings in docs assume Pi 3" ;; esac

c_info "1/9 packages"
run apt-get update
run apt-get install -y --no-install-recommends python3 python3-venv python3-numpy libportaudio2 \
  network-manager dnsmasq-base rfkill iw alsa-utils curl rsync nftables sqlite3

c_info "2/9 user and directories"
id phosphor >/dev/null 2>&1 || run useradd --system --create-home --home-dir "$PHOS_STATE/home" \
  --shell /usr/sbin/nologin --groups audio,video,input,render phosphor
run usermod -aG audio,video,input,render phosphor
install -d -m 0755 "$PHOS_PREFIX"
install -d -m 0750 -o root -g phosphor "$PHOS_ETC"
install -d -m 0750 -o phosphor -g phosphor "$PHOS_STATE"
install -d -m 0700 -o root -g root "$PHOS_BACKUP_DIR"

VER=$(tree_version "$SRC")
REL="$PHOS_RELEASES/$VER"
c_info "3/9 application files -> $REL (release $VER)"
install -d -m 0755 "$PHOS_RELEASES"
# migrate the old single-directory layout (before releases existed)
if [ -d "$PHOS_PREFIX" ] && [ ! -L "$PHOS_PREFIX" ]; then
  [ -d "$PHOS_VENV" ] || { [ -d "$PHOS_PREFIX/venv" ] && mv "$PHOS_PREFIX/venv" "$PHOS_VENV"; }
  rm -rf "$PHOS_PREFIX"
fi
install -d -m 0755 "$REL"
run rsync -a --delete --exclude .git --exclude .venv --exclude venv --exclude __pycache__ --exclude node_modules \
  "$SRC/" "$REL/"
echo "$VER" > "$REL/VERSION"     # version shown per Pi in the dashboard
[ -d "$PHOS_VENV" ] || run python3 -m venv --system-site-packages "$PHOS_VENV"
run "$PHOS_VENV/bin/pip" install --disable-pip-version-check -q -r "$REL/requirements-pi.txt"
sha256sum "$REL/requirements-pi.txt" | cut -d' ' -f1 > "$PHOS_VENV/.requirements.sha256"
ln -sfn "$PHOS_VENV" "$REL/venv"
chmod +x "$REL"/scripts/*.sh
if [ -L "$PHOS_PREFIX" ] && [ "$(readlink -f "$PHOS_PREFIX")" != "$REL" ]; then
  readlink -f "$PHOS_PREFIX" > "$PHOS_RELEASES/PREVIOUS"
fi
switch_release "$REL"
prune_releases

c_info "4/9 node identity and secrets"
if [ ! -f "$PHOS_ETC/node.yaml" ] || ! grep -q "id: $NODE_ID" "$PHOS_ETC/node.yaml"; then
  [ -f "$PHOS_ETC/node.yaml" ] && cp -a "$PHOS_ETC/node.yaml" "$PHOS_BACKUP_DIR/node.yaml.$(date +%s)"
  {
    echo "# written by install.sh"
    echo "node:"
    echo "  id: $NODE_ID"
    [ -n "$PRIORITY" ] && echo "  priority: $PRIORITY"
    [ "$DO_NET" = 0 ] && printf 'network:\n  manage: false\n'
    # initial Wi-Fi country; afterwards it is changed in the admin dashboard and replicated
    [ -n "$COUNTRY" ] && printf 'site:\n  wifi_country: %s\n' "${COUNTRY^^}"
  } > "$PHOS_ETC/node.yaml"
  chmod 0640 "$PHOS_ETC/node.yaml"; chgrp phosphor "$PHOS_ETC/node.yaml"
fi
SECF="$PHOS_ETC/secrets.yaml"
touch "$SECF"; chown phosphor:phosphor "$SECF"; chmod 0600 "$SECF"
if [ -n "$SECRETS" ]; then
  [ -f "$SECRETS" ] || die "$SECRETS not found"
  "$PHOS_PREFIX/venv/bin/python" - "$SECRETS" "$SECF" <<'PY'
import sys, yaml
from pathlib import Path
src = yaml.safe_load(Path(sys.argv[1]).read_text()) or {}
from backend.api.auth import update_secrets
vals = {k: str(src[k]) for k in ("wifi_psk", "cluster_key", "admin_password_hash") if src.get(k)}
update_secrets(Path(sys.argv[2]), **vals)
PY
  chown phosphor:phosphor "$SECF"
fi
if ! grep -q '^session_secret:' "$SECF"; then
  ( cd "$PHOS_PREFIX" && venv/bin/python -c "import secrets,sys;from pathlib import Path;from backend.api.auth import update_secrets;update_secrets(Path(sys.argv[1]),session_secret=secrets.token_hex(32))" "$SECF" )
  chown phosphor:phosphor "$SECF"
fi
grep -q '^cluster_key:' "$SECF" || c_warn "no cluster_key: nodes use an INSECURE dev key. Use --secrets."
cat > /usr/local/sbin/phosphor-passwd <<EOF
#!/bin/sh
# set the PHOSPHOR admin dashboard password on this Pi
set -e
cd $PHOS_PREFIX
$PHOS_PREFIX/venv/bin/python -c "import sys; sys.argv=['phosphor-passwd', '$SECF']; from backend.api.auth import passwd_main; passwd_main()"
chown phosphor:phosphor $SECF && chmod 600 $SECF
systemctl try-restart phosphor-core.service
EOF
chmod 0755 /usr/local/sbin/phosphor-passwd
if ! grep -q '^admin_password_hash:' "$SECF"; then
  if [ -t 0 ]; then
    c_info "set the admin dashboard password (min. 8 characters)"
    /usr/local/sbin/phosphor-passwd
  else
    c_warn "no admin password set; later run: sudo phosphor-passwd"
  fi
fi

c_info "5/9 polkit + systemd units"
install_system_files "$PHOS_PREFIX"
run systemctl daemon-reload
run systemctl enable phosphor.target phosphor-core.service phosphor-audio.service phosphor-input.service
if [ "$FIREWALL" = 1 ]; then run systemctl enable --now phosphor-firewall.service; fi

c_info "6/9 network"
if [ "$DO_NET" = 1 ]; then
  PHOS_PYTHON="$PHOS_PREFIX/venv/bin/python" "$PHOS_PREFIX/scripts/configure-network.sh" ${COUNTRY:+--country "$COUNTRY"} $OTHERWIFI
else
  c_info "skipped (external network mode)"
fi

c_info "7/9 composite CRT output"
if [ "$DO_CRT" = 1 ]; then "$PHOS_PREFIX/scripts/configure-crt.sh" --norm "$NORM"; else c_info "skipped"; fi

c_info "8/9 kiosk"
if [ "$DO_KIOSK" = 1 ]; then "$PHOS_PREFIX/scripts/configure-kiosk.sh"; else c_info "skipped"; fi

c_info "8b/9 watchdog + power-cut protection"
if [ "$READONLY" = 1 ]; then "$PHOS_PREFIX/scripts/configure-robustness.sh" --readonly
else "$PHOS_PREFIX/scripts/configure-robustness.sh"; fi

c_info "9/9 start"
run systemctl restart phosphor-core.service phosphor-audio.service phosphor-input.service || true
c_ok "installed node $NODE_ID. Reboot now to activate composite output and the kiosk:  sudo reboot"
c_info "diagnostics: sudo $PHOS_PREFIX/scripts/diagnostics.sh"
