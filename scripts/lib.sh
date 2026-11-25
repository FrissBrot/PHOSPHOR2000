#!/bin/bash
# Shared helpers for PHOSPHOR2000 scripts. Source, don't execute.
set -euo pipefail

PHOS_PREFIX=${PHOS_PREFIX:-/opt/phosphor2000}          # symlink -> current release
PHOS_RELEASES=${PHOS_RELEASES:-/opt/phosphor2000-releases}
PHOS_VENV=${PHOS_VENV:-/opt/phosphor2000-venv}           # shared by all releases
PHOS_ETC=${PHOS_ETC:-/etc/phosphor2000}
PHOS_STATE=${PHOS_STATE:-/var/lib/phosphor2000}
PHOS_BACKUP_DIR=${PHOS_BACKUP_DIR:-$PHOS_STATE/backup}
PHOS_DRY_RUN=${PHOS_DRY_RUN:-0}
MARK_BEGIN="# >>> phosphor2000 >>>"
MARK_END="# <<< phosphor2000 <<<"

c_ok()   { printf '\033[32m[ ok ]\033[0m %s\n' "$*"; }
c_info() { printf '\033[36m[info]\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m[warn]\033[0m %s\n' "$*" >&2; }
die()    { printf '\033[31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

run() {  # run a command, or print it in dry-run mode
  if [ "$PHOS_DRY_RUN" = "1" ]; then printf '[dry-run] %s\n' "$*"; else "$@"; fi
}

need_root() { [ "${PHOS_ALLOW_NONROOT:-0}" = "1" ] || [ "$(id -u)" -eq 0 ] || die "run as root (sudo)"; }

boot_dir() {
  if [ -n "${PHOS_BOOT_DIR:-}" ]; then echo "$PHOS_BOOT_DIR"; return; fi
  if [ -f /boot/firmware/config.txt ]; then echo /boot/firmware; else echo /boot; fi
}

os_codename() {
  local f=${PHOS_OS_RELEASE:-/etc/os-release}
  # shellcheck disable=SC1090
  ( . "$f" 2>/dev/null; echo "${VERSION_CODENAME:-unknown}" )
}

pi_model() { tr -d '\0' < "${PHOS_MODEL_FILE:-/proc/device-tree/model}" 2>/dev/null || echo "unknown"; }

# Backup a set of files into a new timestamped directory; prints the directory.
backup_files() {
  local tag=$1; shift
  local dir
  dir="$PHOS_BACKUP_DIR/$(date +%Y%m%d-%H%M%S-%N)-$tag"
  mkdir -p "$dir"
  local f
  for f in "$@"; do
    if [ -e "$f" ]; then
      mkdir -p "$dir$(dirname "$f")"
      cp -a "$f" "$dir$f"
      echo "$f" >> "$dir/MANIFEST"
    fi
  done
  touch "$dir/MANIFEST"
  # the very first backup is kept forever as the pre-PHOSPHOR state
  [ -d "$PHOS_BACKUP_DIR/original-$tag" ] || cp -a "$dir" "$PHOS_BACKUP_DIR/original-$tag"
  echo "$dir"
}

latest_backup() { ls -1d "$PHOS_BACKUP_DIR"/[0-9]*-"$1" 2>/dev/null | sort | tail -n1; }
original_backup() { [ -d "$PHOS_BACKUP_DIR/original-$1" ] && echo "$PHOS_BACKUP_DIR/original-$1"; }

restore_backup() {
  local dir=$1 f
  [ -f "$dir/MANIFEST" ] || die "no manifest in $dir"
  while read -r f; do
    [ -n "$f" ] || continue
    if [ -d "$dir$f" ]; then
      rm -rf "$f"; cp -a "$dir$f" "$f"
    else
      cp -a "$dir$f" "$f"
    fi
    c_ok "restored $f"
  done < "$dir/MANIFEST"
}

# Remove our marked block from a file (idempotent).
strip_block() {
  local f=$1
  [ -f "$f" ] || return 0
  sed -i "/^$MARK_BEGIN\$/,/^$MARK_END\$/d" "$f"
}

# Version id of a source tree: <commit date UTC YYYYMMDDTHHMM>-<short sha>, sortable as a string.
tree_version() {
  local src=$1 v
  if [ -f "$src/VERSION" ]; then cat "$src/VERSION"; return; fi
  v=$(cd "$src" && TZ=UTC git log -1 --date=format-local:%Y%m%dT%H%M --format=%cd-%h 2>/dev/null) || v=""
  [ -n "$v" ] || v="$(date -u +%Y%m%dT%H%M)-local"
  if (cd "$src" && [ -n "$(git status --porcelain 2>/dev/null)" ]); then v="$v+mod"; fi
  echo "$v"
}

# Atomically point $PHOS_PREFIX at a release directory (rename(2) of a symlink).
switch_release() {
  local rel=$1 root=${2:-}
  ln -sfn "$rel" "$root$PHOS_PREFIX.new"
  mv -Tf "$root$PHOS_PREFIX.new" "$root$PHOS_PREFIX"
}

# Install unit files, polkit rule and PAM file of a release (root prefix for overlay mode).
install_system_files() {
  local rel=$1 root=${2:-} u
  install -d -m 0755 "$root/etc/polkit-1/rules.d" "$root/etc/systemd/system" "$root/etc/pam.d"
  install -m 0644 "$rel/systemd/50-phosphor.rules" "$root/etc/polkit-1/rules.d/50-phosphor.rules"
  install -m 0644 "$rel/systemd/phosphor-kiosk.pam" "$root/etc/pam.d/phosphor-kiosk"
  for u in "$rel"/systemd/*.service "$rel"/systemd/*.target; do
    install -m 0644 "$u" "$root/etc/systemd/system/$(basename "$u")"
  done
}

# Keep the current, the previous and one more release; delete older ones.
prune_releases() {
  local root=${1:-} cur prev d
  cur=$(readlink -f "$root$PHOS_PREFIX" 2>/dev/null || true)
  prev=$(cat "$root$PHOS_RELEASES/PREVIOUS" 2>/dev/null || true)
  ls -1dt "$root$PHOS_RELEASES"/*/ 2>/dev/null | tail -n +4 | while read -r d; do
    d=${d%/}
    [ "$d" = "$cur" ] || [ "$root$prev" = "$d" ] || [ "$prev" = "$d" ] || rm -rf "$d"
  done
}
