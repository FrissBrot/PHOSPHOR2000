# PHOSPHOR2000 (PHOSPHOR2000)

A distributed, offline, retro-green visual system for **3–5 Raspberry Pi 3 B/B+**, each driving a
**4:3 PAL CRT over composite**. Every Pi renders the same deterministic Canvas 2D animations — the
twelve "Cyberspace 2000" CRT visuals for the Mattliball bar (Boot, Dance.exe, Spectrum Analyzer,
Neon Grid + Cube, Data Rain, Y2K Countdown, Mattliball 2027, Guggä-Register, Stollä-Tunnel,
Motto.db, Bar-Netzwerk, Bis es hell wird; see [docs/scenes.md](docs/scenes.md)) — locked to a **distributed musical timeline** that follows the DJ mixer via a USB
line-in. No router, no internet, no permanent master: the Pis form their own Wi-Fi
(`PHOSPHOR2000`, admin at `http://192.168.50.1:8080/admin/`) and hand the hotspot over when
the leader disappears.

> **Status:** software complete with 158 automated tests (unit, simulated cluster integration,
> desktop-browser integration). **Not yet validated on real Pi/CRT hardware** — see
> [docs/test-report.md](docs/test-report.md) and the checklist in [docs/hardware.md](docs/hardware.md).

## Physical constraints (designed around, not hidden)

* **One Wi-Fi radio per Pi** → the network drops for ~20–45 s whenever the hotspot moves. CRTs keep
  rendering from local prediction.
* **Partitions can create competing leaders.** Reconciled deterministically (older epoch wins,
  then priority) via scans and a scout node; edits in the losing partition are lost (calibration
  offsets are merged). This is not consensus.
* **Unconnected clocks drift** (tens of ppm). Skew is estimated and uncertainty is shown on screen.
* **The Pi with the audio input is always the master.** If it fails, another Pi takes over but can
  only free-run the last tempo (PREDICTED); live BPM failover needs a second audio feed.
* **rAF ≠ photons.** Display latency is configurable and must be measured (camera/photodiode);
  Enter-key tapping is an alignment aid with human bias, not a latency measurement.

## Quick start

* Deploy on Pis: **[docs/installation.md](docs/installation.md)** (step-by-step for 3–5 Pis).
* Try it on a desktop (no Pi needed):
  ```bash
  uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
  PHOSPHOR_ETC=/tmp/phos/etc PHOSPHOR_STATE=/tmp/phos/state PHOSPHOR_RUN=/tmp/phos/run \
    .venv/bin/python -m backend.services.core --node pi-01 --no-network
  # renderer: http://127.0.0.1:8080/   demo without core: /?demo=1&scene=rain
  # admin:    http://127.0.0.1:8080/admin/ (set a password first, see below)
  PHOSPHOR_ADMIN_PASSWORD=changeme123 .venv/bin/python -c \
    "import sys;sys.argv=['x','/tmp/phos/etc/secrets.yaml'];from backend.api.auth import passwd_main;passwd_main()"
  ```
* Simulate a cluster: `python -m backend.cluster.simulation --scenario failover|partition|boot`
* Tests: `.venv/bin/python -m pytest -q`

## Documentation

| doc | content |
|---|---|
| [scenes](docs/scenes.md) | the twelve visuals, timing rules, look settings |
| [architecture](docs/architecture.md) | processes, clock domains, replicated state, code map |
| [installation](docs/installation.md) | SD card → cluster, updating, rollback, ports & security |
| [network-recovery](docs/network-recovery.md) | network states, election, split brain, limits, recovery |
| [synchronization](docs/synchronization.md) | audio→beat→cluster clock→frame, offsets, calibration, camera/photodiode |
| [hardware](docs/hardware.md) | risks and the real-hardware validation checklist |
| [network-modes](docs/network-modes.md) | hotspot / existing Wi-Fi / Ethernet switch, internet sharing, OS package updates |
| [updates-and-robustness](docs/updates-and-robustness.md) | updates from the dashboard, watchdogs, power-cut protection |
| [troubleshooting](docs/troubleshooting.md) | symptoms → fixes |
| [test-report](docs/test-report.md) | what was tested how (simulated vs hardware) |

## Repository layout

```
backend/   api/ (FastAPI, auth) cluster/ (election, node, scheduler, simulator) network/ (nmcli)
           sync/ (timeline, clock sync, calibration) audio/ (beat tracker, capture) storage/ services/
frontend/  index.html, css/, js/engine (timeline, clock, renderer), js/scenes (6 + calibration), js/services
admin/     dashboard (vanilla JS, no CDN)
config/    default.yaml
scripts/   install.sh configure-network.sh configure-crt.sh configure-kiosk.sh uninstall.sh diagnostics.sh
systemd/   units, polkit rule, PAM, nftables, NetworkManager drop-in
tests/     pytest + node --test (JS parity)
tools/     sim_report.py
```

Security: WPA2-PSK (CCMP) hotspot, HMAC-authenticated cluster traffic, scrypt-hashed admin password,
session cookies + CSRF header, validated commands only (no shell), polkit-limited privileges, no
cloud, no telemetry, flash-rate (≤ 3 Hz) and luminance limits plus a reduced-flashing mode.
