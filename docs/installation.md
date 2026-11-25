# Installation and deployment guide (3–5 Raspberry Pi 3)

## What you need

Per CRT: Raspberry Pi 3 B or B+ (B+ preferred: better Wi-Fi), good 5 V / 2.5 A supply, 16–32 GB
A1 microSD, **3.5 mm TRRS → RCA composite cable wired for the Raspberry Pi pinout**
(Tip = left audio, Ring 1 = right audio, Ring 2 = ground, Sleeve = video; many camcorder cables
swap ground/video and give a dark or no picture), CRT with composite input.
Once: a USB audio interface with line-in for the Pi that gets the DJ mixer feed (a second one on
another Pi gives live-audio redundancy), a USB keyboard for calibration, a phone/laptop for the
dashboard. Optional: Ethernet cable for maintenance.

## 1. Prepare SD cards (each Pi)

1. Raspberry Pi Imager → **Raspberry Pi OS Lite (64-bit)**, Bookworm or Trixie.
2. In the Imager's OS customisation: hostname `phos-pi-01` (…05), user + password, enable SSH,
   locale/timezone. Optionally configure your normal Wi-Fi for the first setup *or* use Ethernet.
3. Boot each Pi once on HDMI or via SSH; `sudo apt update && sudo apt full-upgrade -y && sudo reboot`.

## 2. Create the cluster secrets (once)

On any Linux/macOS machine or the first Pi, from a checkout of this repository:

```bash
git clone https://github.com/FrissBrot/PHOSPHOR2000.git && cd PHOSPHOR2000
scripts/install.sh --generate-secrets cluster-secrets.yaml
```

This asks for the **admin dashboard password (one for all Pis)**, prints the Wi-Fi password for
`PHOSPHOR2000` and writes `cluster-secrets.yaml` (WPA2 passphrase, 256-bit cluster key used to
authenticate all node traffic, admin password hash). Copy the file to
every Pi (scp / USB stick) and keep it private. All Pis **must** use the same file.

## 3. Install on every Pi

```bash
git clone https://github.com/FrissBrot/PHOSPHOR2000.git && cd PHOSPHOR2000
sudo scripts/install.sh --node-id pi-01 --country DE --secrets ~/cluster-secrets.yaml
```

Use `pi-01` for the Pi you most want as leader (priority 100), then `pi-02` (90) … `pi-05` (60).
The Pi with the USB audio interface **automatically becomes the leader** (master), whatever its
id; see network-recovery.md.
The admin password comes from `cluster-secrets.yaml`; change it later in the dashboard
(Config → Admin password) — the change is replicated to every Pi. `sudo phosphor-passwd` on a Pi
also works and is taken over by the whole cluster.

`--country` is the initial Wi-Fi regulatory domain (2 letters, e.g. `CH`): Pi OS keeps Wi-Fi
blocked until one is set, so it is needed once at install. Later changes: dashboard → Config →
*Wi-Fi country* → applied on all Pis immediately and kept after reboot. You are asked for the admin dashboard
password (≥ 8 chars) on the first Pi where it is not yet set; set it on every Pi
(`sudo phosphor-passwd`) — the dashboard you reach is whichever Pi is currently leader.

The installer (idempotent; re-run it any time):
1. installs packages (`python3-venv`, `libportaudio2`, `network-manager`, `dnsmasq-base`, `cage`,
   `chromium`, `fonts-dejavu-core`, `nftables`, …)
2. creates the system user `phosphor` and `/opt/phosphor2000`, `/etc/phosphor2000`,
   `/var/lib/phosphor2000`; Python venv with pinned dependencies (`requirements-pi.txt`)
3. writes `node.yaml` and `secrets.yaml` (0600)
4. installs systemd units and a polkit rule (only: NetworkManager control, restart of the
   phosphor units, reboot — no sudo, no shell)
5. `configure-network.sh`: backups, country, AP + station keyfiles (`autoconnect=no`),
   Wi-Fi power save off
6. `configure-crt.sh`: backups, composite PAL 720×576i 4:3, console blanking off
7. `configure-kiosk.sh`: cage + Chromium on tty1, getty@tty1 masked
8. `configure-robustness.sh`: hardware watchdog, journal/Chromium in RAM, no swap
   (`--readonly` for a read-only root file system)

Options: `--network hotspot|wlan|ethernet` (see network-modes.md; `--wlan-ssid`, `--wlan-psk-file`
for an existing Wi-Fi), `--no-network` (never touch networking, configure it yourself), `--no-crt`, `--no-kiosk`, `--firewall`,
`--disable-other-wifi` (recommended if the Pi has a home Wi-Fi profile with autoconnect),
`--norm NTSC`, `--priority N`.

**The master (audio Pi) with or without a screen** — all three work:

| master has | install with | notes |
|---|---|---|
| a CRT (composite) | defaults | like every other Pi |
| no screen | `--no-kiosk --no-crt` | saves ~1 core of CPU on the busiest Pi; dashboard, audio, sync, keyboard calibration all work (the HUD with tap results is shown on the CRT being calibrated) |
| an HDMI monitor | `--no-crt` | HDMI stays active; the 4:3 picture is letterboxed automatically |

Change later without reinstalling: `sudo /opt/phosphor2000/scripts/configure-kiosk.sh --remove`
(screen off) or `sudo /opt/phosphor2000/scripts/configure-kiosk.sh` (screen on);
`configure-crt.sh --rollback` gives HDMI back, `configure-crt.sh` switches to composite. Reboot after
changing the video output.

Then: `sudo reboot`. HDMI goes dark (composite takes over on Pi 3). After ~30–60 s the CRT shows
the boot scene.

## 4. Verify each Pi

```bash
sudo /opt/phosphor2000/scripts/diagnostics.sh | less
```

Check: `Composite-1 connected` with mode `720x576i`, services active, `core health` ok, audio
card listed on the audio Pi, keyboard listed where plugged.

## 5. First cluster start

1. Power all Pis at once (or in any order). Within ~25–40 s exactly one becomes leader (normally
   pi-01) and starts the AP; the others join.
2. Connect your phone to Wi-Fi `PHOSPHOR2000` and open **http://192.168.50.1:8080/admin/**.
3. Cluster tab: all nodes FOLLOWER/LEADER, sync uncertainty a few ms, fps ≈ 25.
4. Config tab → *set cluster clock from this device* (Pi 3 has no RTC; otherwise the clock scene
   shows the last saved time).
5. Timing tab: start music on the DJ mixer → source becomes LIVE within ~5–10 s. Use
   *this beat = downbeat* once to align bar starts with the music's "1".
6. Calibration tab (or USB keyboard, see synchronization.md): calibrate each CRT, then compare
   all with *Flash ALL CRTs* and a camera.

## 6. Updating

From the dashboard (Updates tab): one Pi with internet fetches the new version from GitHub, then
*roll out* copies it to all other Pis inside the offline cluster, one by one, with health check
and automatic rollback. Details: [updates-and-robustness.md](updates-and-robustness.md).
Re-running `install.sh` from a fresh checkout also works and keeps secrets and offsets.

## 7. Uninstall / rollback

* `sudo scripts/configure-crt.sh --rollback` — composite settings back to the original
* `sudo scripts/configure-network.sh --rollback` — NetworkManager back to the original
* `sudo scripts/uninstall.sh [--purge]` — everything (keeps offsets/logs unless `--purge`)

## Ports and security summary

| port | who | protection |
|---|---|---|
| 8080/tcp | admin dashboard + local renderer | password (scrypt), HttpOnly SameSite=Strict session cookie, CSRF header, rate limit; renderer WS only from 127.0.0.1; optional nftables restriction to 192.168.50.0/24 |
| 47800/udp | node ↔ node | HMAC-SHA256 with the cluster key, replay protection; optional nftables restriction |
| 67/udp, 53 | DHCP/DNS on the active AP | NetworkManager `shared` mode |

No cloud services, no telemetry; Chromium update checks, metrics, sync and crash reporting
are disabled by flags.
