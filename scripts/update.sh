#!/bin/bash
# PHOSPHOR2000 self-update (run as root by phosphor-update.service, requested from the dashboard).
#
#   update.sh --request /run/phosphor2000/update-request.json
#   update.sh --github                 # newest commit of the configured repo/branch (needs internet)
#   update.sh --peer URL --sha256 HEX --version VER   # bundle from another Pi in the cluster
#   update.sh --rollback               # back to the previous release
#
# Steps: download -> verify (sha256 from the authenticated cluster command, or HTTPS from GitHub)
# -> unpack into a new release directory -> python deps only if requirements changed (needs
# internet; otherwise abort, old version keeps running) -> install unit files -> atomic symlink
# switch -> restart services -> health check (90 s) -> automatic rollback if unhealthy.
# Read-only root (overlay): the release is written to the real SD card, then the Pi reboots.
# Progress is written to /run/phosphor2000/update-status.json for the dashboard.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
# run from a private copy: this file is replaced by the update itself
if [ -z "${PHOS_UPDATE_COPY:-}" ]; then
  tmp=$(mktemp /tmp/phos-update.XXXXXX.sh)
  cp "$0" "$tmp"; cp "$HERE/lib.sh" "${tmp%.sh}.lib.sh"
  PHOS_UPDATE_COPY=1 PHOS_UPDATE_LIB="${tmp%.sh}.lib.sh" exec bash "$tmp" "$@"
fi
# shellcheck source=lib.sh
. "${PHOS_UPDATE_LIB:-$HERE/lib.sh}"
set +e

RUN=${PHOS_RUN:-/run/phosphor2000}
STATUS=$RUN/update-status.json
HEALTH_URL=${PHOS_HEALTH_URL:-http://127.0.0.1:8080/api/health}
TEST_MODE=${PHOS_UPDATE_TEST:-0}      # tests: no systemctl, no unit install
PY=${PHOS_SYS_PYTHON:-python3}

status() {   # state message [version]
  install -d -m 0755 "$RUN"
  "$PY" - "$STATUS" "$1" "$2" "${3:-}" <<'PY'
import json, sys, time
path, state, msg, ver = sys.argv[1:5]
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump({"state": state, "message": msg, "version": ver, "at": time.time()}, f)
import os; os.chmod(tmp, 0o644); os.replace(tmp, path)
PY
  echo "[update] $1: $2"
}
fail() { status error "$1" "${2:-}"; exit 1; }

valid_version() { [[ $1 =~ ^[0-9]{8}T[0-9]{4}-[0-9a-f]{7,40}(\+mod)?$|^[0-9]{8}T[0-9]{4}-local(\+mod)?$ ]]; }

SOURCE="" URL="" SHA="" VER="" ROLLBACK=0
while [ $# -gt 0 ]; do
  case $1 in
    --request)
      REQ=$("$PY" - "$2" <<'PY'
import json, re, shlex, sys
r = json.load(open(sys.argv[1]))
src = r.get("source")
assert src in ("github", "peer", "rollback"), "bad source"
out = {"SOURCE": src}
if src == "peer":
    assert re.fullmatch(r"http://\d{1,3}(\.\d{1,3}){3}:\d{1,5}/api/update/bundle\?[A-Za-z0-9=&._%+-]+", r["url"]), "bad url"
    assert re.fullmatch(r"[0-9a-f]{64}", r["sha256"]), "bad sha"
    out.update(URL=r["url"], SHA=r["sha256"], VER=str(r["version"]))
for k, v in out.items():
    print(f"{k}={shlex.quote(v)}")
PY
) || fail "invalid update request"
      eval "$REQ"
      [ "$SOURCE" = rollback ] && ROLLBACK=1
      shift ;;
    --github) SOURCE=github ;;
    --peer) SOURCE=peer; URL=$2; shift ;;
    --sha256) SHA=$2; shift ;;
    --version) VER=$2; shift ;;
    --rollback) ROLLBACK=1 ;;
    *) fail "unknown option $1" ;;
  esac
  shift
done

# ---------------------------------------------------------------- targets
ROOT=""
if [ "$(findmnt -n -o FSTYPE / 2>/dev/null)" = overlay ] && [ "$TEST_MODE" != 1 ]; then
  for d in /media/root-ro /run/rootfsbase /overlay/lower; do mountpoint -q "$d" && ROOT=$d && break; done
  [ -n "$ROOT" ] || fail "read-only root: lower file system not found"
  mount -o remount,rw "$ROOT" || fail "cannot remount the SD card writable"
  trap 'sync; mount -o remount,ro "$ROOT"' EXIT
fi
CUR=$(readlink -f "$ROOT$PHOS_PREFIX" 2>/dev/null)
CUR=${CUR#"$ROOT"}
CUR_VER=$(cat "$ROOT$CUR/VERSION" 2>/dev/null || echo unknown)

restart_services() {
  [ "$TEST_MODE" = 1 ] && return 0
  systemctl daemon-reload
  systemctl restart phosphor-core.service phosphor-audio.service phosphor-input.service
  # the kiosk page reloads itself when it sees the new version (no black screen restart)
}

healthy() {   # wait until the core answers with the expected version
  local want=$1 i
  for i in $(seq 1 "${PHOS_HEALTH_TRIES:-45}"); do
    if "$PY" - "$HEALTH_URL" "$want" <<'PY' 2>/dev/null; then return 0; fi
import json, sys, urllib.request
d = json.load(urllib.request.urlopen(sys.argv[1], timeout=2))
sys.exit(0 if d.get("ok") and d.get("version") == sys.argv[2] else 1)
PY
    sleep "${PHOS_HEALTH_SLEEP:-2}"
  done
  return 1
}

activate() {   # release_dir(without ROOT) version
  local rel=$1 ver=$2
  if [ "$TEST_MODE" != 1 ]; then install_system_files "$ROOT$rel" "$ROOT"; fi
  [ -n "$CUR" ] && [ "$CUR" != "$rel" ] && echo "$CUR" > "$ROOT$PHOS_RELEASES/PREVIOUS"
  switch_release "$rel" "$ROOT"
  if [ -n "$ROOT" ]; then
    status rebooting "release $ver installed on the read-only system, rebooting" "$ver"
    sync; systemctl reboot; exit 0
  fi
  restart_services
}

# ---------------------------------------------------------------- rollback
if [ "$ROLLBACK" = 1 ]; then
  PREV=$(cat "$ROOT$PHOS_RELEASES/PREVIOUS" 2>/dev/null)
  [ -n "$PREV" ] && [ -d "$ROOT$PREV" ] || fail "no previous release to roll back to"
  PV=$(cat "$ROOT$PREV/VERSION")
  status installing "rolling back to $PV"
  activate "$PREV" "$PV"
  healthy "$PV" && status done "rolled back to $PV" "$PV" || fail "rollback to $PV not healthy" "$PV"
  exit 0
fi

# ---------------------------------------------------------------- download
WORK=$(mktemp -d /tmp/phos-update.XXXXXX)
trap 'rm -rf "$WORK"; [ -n "$ROOT" ] && { sync; mount -o remount,ro "$ROOT"; }' EXIT
case $SOURCE in
  github)
    eval "$(cd "$CUR" 2>/dev/null && "$PHOS_VENV/bin/python" -c '
import shlex
from backend.config import load_config
c = load_config().update_source
print(f"REPO={shlex.quote(c.repo)} BRANCH={shlex.quote(c.branch)}")' 2>/dev/null)"
    REPO=${PHOS_UPDATE_REPO:-${REPO:-FrissBrot/PHOSPHOR2000}}; BRANCH=${PHOS_UPDATE_BRANCH:-${BRANCH:-main}}
    status downloading "asking GitHub for $REPO@$BRANCH"
    META=$(curl -fsS --max-time 20 -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/$REPO/commits/$BRANCH") || fail "GitHub not reachable (internet?)"
    eval "$(echo "$META" | "$PY" -c '
import json, shlex, sys, datetime
d = json.load(sys.stdin)
sha = d["sha"]; dt = d["commit"]["committer"]["date"]
t = datetime.datetime.strptime(dt, "%Y-%m-%dT%H:%M:%SZ")
print(f"SHA_GIT={shlex.quote(sha)} VER={t:%Y%m%dT%H%M}-{sha[:7]}")')"
    curl -fsSL --max-time 300 -o "$WORK/src.tar.gz" "https://codeload.github.com/$REPO/tar.gz/$SHA_GIT" \
      || fail "download from GitHub failed"
    ;;
  peer)
    valid_version "$VER" || fail "invalid version '$VER'"
    status downloading "fetching $VER from cluster peer"
    curl -fsS --max-time 300 -o "$WORK/src.tar.gz" "$URL" || fail "download from peer failed"
    GOT=$(sha256sum "$WORK/src.tar.gz" | cut -d' ' -f1)
    [ "$GOT" = "$SHA" ] || fail "checksum mismatch: bundle rejected"
    ;;
  *) fail "no update source given" ;;
esac
valid_version "$VER" || fail "invalid version '$VER'"
if [ "$VER" = "$CUR_VER" ]; then status done "already on $VER" "$VER"; exit 0; fi

# ---------------------------------------------------------------- unpack
REL="$PHOS_RELEASES/$VER"
status installing "unpacking $VER"
mkdir -p "$WORK/x"
tar -xzf "$WORK/src.tar.gz" -C "$WORK/x" --strip-components=1 --no-same-owner || fail "bundle is not a valid archive"
[ -f "$WORK/x/backend/services/core.py" ] && [ -f "$WORK/x/requirements-pi.txt" ] || fail "bundle is not a PHOSPHOR2000 release"
echo "$VER" > "$WORK/x/VERSION"
rm -rf "$WORK/x/.git" "$WORK/x/venv"
install -d -m 0755 "$ROOT$PHOS_RELEASES"
rm -rf "$ROOT$REL"; mv "$WORK/x" "$ROOT$REL"
chmod +x "$ROOT$REL"/scripts/*.sh

# python dependencies: shared venv, re-installed only when requirements changed
NEWREQ=$(sha256sum "$ROOT$REL/requirements-pi.txt" | cut -d' ' -f1)
OLDREQ=$(cat "$ROOT$PHOS_VENV/.requirements.sha256" 2>/dev/null)
if [ "$NEWREQ" != "$OLDREQ" ] && [ "$TEST_MODE" != 1 ]; then
  status installing "installing new Python dependencies (needs internet)"
  "$ROOT$PHOS_VENV/bin/pip" install --disable-pip-version-check -q -r "$ROOT$REL/requirements-pi.txt" \
    || { rm -rf "$ROOT$REL"; fail "new dependencies could not be installed (no internet?) - kept $CUR_VER"; }
  echo "$NEWREQ" > "$ROOT$PHOS_VENV/.requirements.sha256"
fi
ln -sfn "$PHOS_VENV" "$ROOT$REL/venv"

# ---------------------------------------------------------------- switch + verify
status installing "activating $VER"
activate "$REL" "$VER"
if healthy "$VER"; then
  prune_releases "$ROOT"
  status done "updated $CUR_VER -> $VER" "$VER"
  exit 0
fi
# unhealthy: automatic rollback
status rolling_back "$VER did not come up healthy, rolling back to $CUR_VER" "$VER"
if [ -n "$CUR" ] && [ -d "$CUR" ]; then
  [ "$TEST_MODE" != 1 ] && install_system_files "$CUR"
  switch_release "$CUR"
  restart_services
  healthy "$CUR_VER" && fail "update to $VER failed health check; rolled back to $CUR_VER" "$CUR_VER"
fi
fail "update to $VER failed and rollback is not healthy - check the Pi" "$VER"
