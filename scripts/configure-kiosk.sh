#!/bin/bash
# Chromium kiosk via the cage Wayland compositor on tty1: no desktop, no cursor,
# no screen blanking, restarted by systemd. Idempotent.
#
#   sudo scripts/configure-kiosk.sh            # install/enable
#   sudo scripts/configure-kiosk.sh --remove   # disable kiosk, give tty1 back to getty
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"

ACTION=apply
[ "${1:-}" = "--remove" ] && ACTION=remove

apply() {
  need_root
  local browser_pkg=chromium
  if ! apt-cache show chromium >/dev/null 2>&1; then browser_pkg=chromium-browser; fi
  run apt-get install -y --no-install-recommends cage "$browser_pkg" fonts-dejavu-core curl
  install -m 0644 "$CS2K_PREFIX/systemd/cyberspace-kiosk.pam" /etc/pam.d/cyberspace-kiosk
  install -d -o cyberspace -g cyberspace -m 0750 "$CS2K_STATE/chromium"
  # cage needs DRM/input access through logind; the seat user needs these groups too
  run usermod -aG video,render,input cyberspace
  # tty1 belongs to the kiosk now
  run systemctl disable --now getty@tty1.service || true
  run systemctl mask getty@tty1.service
  # blanking: kernel console (cmdline consoleblank=0 by configure-crt.sh) + logind idle off
  install -d /etc/systemd/logind.conf.d
  printf '[Login]\nIdleAction=ignore\nHandlePowerKey=reboot\n' > /etc/systemd/logind.conf.d/90-cyberspace.conf
  if [ ! -f "$CS2K_ETC/chromium-flags" ]; then
    cat > "$CS2K_ETC/chromium-flags" <<'EOF'
# Extra Chromium flags for the kiosk, one per line. Benchmark before changing!
# --enable-gpu-rasterization
# --disable-gpu
EOF
  fi
  run systemctl daemon-reload
  run systemctl enable cyberspace-renderer.service
  c_ok "kiosk enabled (cage + $browser_pkg on tty1)"
}

remove() {
  need_root
  run systemctl disable --now cyberspace-renderer.service || true
  run systemctl unmask getty@tty1.service || true
  run systemctl enable --now getty@tty1.service || true
  rm -f /etc/pam.d/cyberspace-kiosk /etc/systemd/logind.conf.d/90-cyberspace.conf
  c_ok "kiosk removed, console login restored on tty1"
}

$ACTION
