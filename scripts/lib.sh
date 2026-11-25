#!/bin/bash
# Shared helpers for PHOSPHOR2000 scripts. Source, don't execute.
set -euo pipefail

PHOS_PREFIX=${PHOS_PREFIX:-/opt/phosphor2000}
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
