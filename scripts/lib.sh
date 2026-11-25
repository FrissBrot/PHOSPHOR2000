#!/bin/bash
# Shared helpers for CYBERSPACE 2000 scripts. Source, don't execute.
set -euo pipefail

CS2K_PREFIX=${CS2K_PREFIX:-/opt/cyberspace2000}
CS2K_ETC=${CS2K_ETC:-/etc/cyberspace2000}
CS2K_STATE=${CS2K_STATE:-/var/lib/cyberspace2000}
CS2K_BACKUP_DIR=${CS2K_BACKUP_DIR:-$CS2K_STATE/backup}
CS2K_DRY_RUN=${CS2K_DRY_RUN:-0}
MARK_BEGIN="# >>> cyberspace-2000 >>>"
MARK_END="# <<< cyberspace-2000 <<<"

c_ok()   { printf '\033[32m[ ok ]\033[0m %s\n' "$*"; }
c_info() { printf '\033[36m[info]\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m[warn]\033[0m %s\n' "$*" >&2; }
die()    { printf '\033[31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

run() {  # run a command, or print it in dry-run mode
  if [ "$CS2K_DRY_RUN" = "1" ]; then printf '[dry-run] %s\n' "$*"; else "$@"; fi
}

need_root() { [ "${CS2K_ALLOW_NONROOT:-0}" = "1" ] || [ "$(id -u)" -eq 0 ] || die "run as root (sudo)"; }

boot_dir() {
  if [ -n "${CS2K_BOOT_DIR:-}" ]; then echo "$CS2K_BOOT_DIR"; return; fi
  if [ -f /boot/firmware/config.txt ]; then echo /boot/firmware; else echo /boot; fi
}

os_codename() {
  local f=${CS2K_OS_RELEASE:-/etc/os-release}
  # shellcheck disable=SC1090
  ( . "$f" 2>/dev/null; echo "${VERSION_CODENAME:-unknown}" )
}

pi_model() { tr -d '\0' < "${CS2K_MODEL_FILE:-/proc/device-tree/model}" 2>/dev/null || echo "unknown"; }

# Backup a set of files into a new timestamped directory; prints the directory.
backup_files() {
  local tag=$1; shift
  local dir
  dir="$CS2K_BACKUP_DIR/$(date +%Y%m%d-%H%M%S-%N)-$tag"
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
  # the very first backup is kept forever as the pre-CYBERSPACE state
  [ -d "$CS2K_BACKUP_DIR/original-$tag" ] || cp -a "$dir" "$CS2K_BACKUP_DIR/original-$tag"
  echo "$dir"
}

latest_backup() { ls -1d "$CS2K_BACKUP_DIR"/[0-9]*-"$1" 2>/dev/null | sort | tail -n1; }
original_backup() { [ -d "$CS2K_BACKUP_DIR/original-$1" ] && echo "$CS2K_BACKUP_DIR/original-$1"; }

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
