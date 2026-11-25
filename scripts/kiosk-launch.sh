#!/bin/bash
# Started by cyberspace-renderer.service inside cage. Waits for the local core,
# clears Chromium's crash flag (no "restore pages" bubble) and execs Chromium.
set -u
PROFILE=/var/lib/cyberspace2000/chromium
URL="http://127.0.0.1:8080/"
mkdir -p "$PROFILE"
for i in $(seq 1 60); do
  if curl -fsS --max-time 1 "${URL}api/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
PREF="$PROFILE/Default/Preferences"
if [ -f "$PREF" ]; then
  sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"[^"]*"/"exit_type":"Normal"/' "$PREF" || true
fi
BROWSER=$(command -v chromium || command -v chromium-browser)
EXTRA=()
[ -f /etc/cyberspace2000/chromium-flags ] && mapfile -t EXTRA < <(grep -v '^\s*#' /etc/cyberspace2000/chromium-flags | grep -v '^\s*$')
exec "$BROWSER" \
  --kiosk "$URL" \
  --ozone-platform=wayland \
  --user-data-dir="$PROFILE" \
  --disk-cache-dir=/tmp/cs2k-chromium-cache \
  --noerrdialogs --disable-infobars --no-first-run --no-default-browser-check \
  --disable-session-crashed-bubble --disable-features=Translate,TranslateUI,MediaRouter,OptimizationHints \
  --check-for-update-interval=31536000 --disable-component-update --disable-background-networking \
  --disable-sync --metrics-recording-only --disable-breakpad --disable-domain-reliability \
  --autoplay-policy=no-user-gesture-required --password-store=basic \
  --overscroll-history-navigation=0 --disable-pinch \
  "${EXTRA[@]}"
