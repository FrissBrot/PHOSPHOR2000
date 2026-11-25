# Updates and robustness

## Software updates (dashboard → Updates)

Layout on every Pi: `/opt/phosphor2000` is a symlink to the active release in
`/opt/phosphor2000-releases/<version>`; the Python environment `/opt/phosphor2000-venv` is shared.
A version looks like `20261125T1207-85ac3f6` (commit time UTC + commit id) and sorts by date.

**Typical flow (the cluster is offline):**

1. Give *one* Pi internet: Ethernet cable to a router, or temporarily at home.
2. Dashboard → Updates → **update this Pi from GitHub**. That Pi downloads the newest commit of
   `FrissBrot/PHOSPHOR2000` (branch `main`; configurable only in `/etc/phosphor2000/node.yaml`
   under `update_source:` — deliberately not from the dashboard, because it decides which code
   root installs).
3. Back in the cluster: **roll out newest version to all Pis**. The leader takes the newest release
   that exists on any Pi, asks that Pi for a manifest signed with the cluster key, then orders the
   Pis one after the other (least important first, leader last) to download the bundle from it and
   verify its SHA-256. A Pi that does not come back with the new version within 7 minutes stops the
   rollout.

**On each Pi** (`scripts/update.sh`, root oneshot `phosphor-update.service`, started via a polkit
rule that allows exactly this unit): download → checksum / archive / release check → unpack to a
new release directory → Python packages only if `requirements-pi.txt` changed (that needs internet;
without it the update is aborted and the old version keeps running) → unit files → atomic symlink
switch → restart core/audio/input (~10 s; the CRT keeps rendering, the kiosk page reloads itself
in 1–2 s when it sees the new version) → health check (core answers with the new version within
90 s) → **automatic rollback** otherwise. *roll back* in the version table returns any Pi to its
previous release.

**Automatic updates** (off by default): the leader checks GitHub every few hours (if it has
internet), updates itself and rolls out to the cluster — but only after no LIVE music has been
detected for the configured minutes. Nothing updates in the middle of a set.

Security: bundles and manifests are only served to requests signed with the cluster key; the
checksum travels in HMAC-authenticated cluster datagrams; the update request file is validated
by the root script (only cluster addresses, fixed URL path, 64-hex checksums, version format).
Whoever has the admin password or the cluster key can deploy software — keep both private.

## Hang and crash protection

| what | how |
|---|---|
| core crashes | systemd restarts it after 2 s, forever |
| core hangs (event loop stuck) | `Type=notify` + `WatchdogSec=30`: the 20 Hz loop sends heartbeats; no heartbeat → systemd kills and restarts it |
| core restarts while leader/follower | resumes immediately: the hotspot / Wi-Fi association stays up, clock offset and beat phase come from the cache |
| audio service hangs | `WatchdogSec=20` heartbeat from the analysis thread |
| USB audio stalls silently | no samples for 5 s → stream reopened |
| kiosk crashes | systemd restarts cage/Chromium after 3 s |
| kiosk hangs / "Aw, Snap" / draws < 1 fps | the core misses the browser's 2-s reports → restarts the renderer (45 s silent, 30 s frozen; backoff 2→15 min if it keeps failing) |
| kernel or systemd hang | BCM2835 hardware watchdog fed by systemd (`RuntimeWatchdogSec=14s`) resets the Pi |
| two Pis with the same id | detected from its datagrams; warning on the CRT and in the dashboard |
| different software versions | version column in the dashboard (≠ marks Pis that differ from the leader) |

## Power cuts and the SD card

Always configured by the installer (`configure-robustness.sh`):
* journal only in RAM, no swap file, Chromium profile and cache in RAM (`/run`)
* event log in RAM; only calibration history, settings and offsets are written — atomically
  (write, fsync, rename) and only when they change

Optional, maximum protection: `sudo /opt/phosphor2000/scripts/configure-robustness.sh --readonly`
(or `install.sh --readonly`). The root file system becomes a RAM overlay (Raspberry Pi OS overlay
mode); nothing is written to the card during operation. When settings/offsets change, the core
asks `phosphor-persist.service` to copy them to the real card (remounted writable for ~100 ms).
Updates also work in this mode: the release is written to the real card and the Pi reboots.
To install by hand: `--readonly-off`, reboot, install, `--readonly`.
`--status` shows overlay, watchdog and swap state; `--rollback` removes everything.

Not verified on hardware yet: the overlay lower mount point (`/media/root-ro` on Bookworm's
overlayroot) — `configure-robustness.sh --status` and a test of "save an offset, pull the plug,
boot" belong in the hardware checklist.
