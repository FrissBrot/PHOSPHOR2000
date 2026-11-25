# Hardware notes, risks and validation checklist

**Nothing in this repository has been tested on real Raspberry Pi / CRT hardware yet.** All
numbers in docs/test-report.md are from simulation or desktop Chromium. This checklist is how to
produce the real numbers. Record results in a copy of the table at the end.

## Known physical risks

| risk | consequence | mitigation in this design |
|---|---|---|
| single Wi-Fi radio | network drops for 20–45 s on leader loss / handover | renderers fully local; timeline predicted; cache |
| competing hotspots after partitions | two leaders, diverging programmes | scans + scouting + deterministic claim order; documented limits |
| crystal drift (±20–50 ppm) | 1–3 ms per minute between isolated Pis | skew estimation (30 min baseline), uncertainty shown |
| no RTC on Pi 3 | wrong date after cold start offline | set cluster clock from phone; optional RTC HAT |
| audio only on one Pi | if that Pi dies, no new tempo detection | PREDICTED free-run; second USB interface on another Pi gives redundancy |
| browser frame timing | rAF ≠ photon; GC pauses; dropped frames | present-time prediction, 25 fps cadence, configurable latency, calibration |
| PAL interlace / scan position | 20 ms field period, top/bottom of screen differ | calibrate at the screen height that matters; large flashes |
| Pi 3 thermal throttling | lower fps, jitter | heatsink, monitor `get_throttled` in dashboard |
| undervoltage | random resets, SD corruption | proper PSU; read-mostly cache writes |
| composite cable pinout | no/dim picture | use a Pi-specific TRRS cable |
| Wi-Fi power save | 10–100 ms packet delays | disabled via NetworkManager conf |
| Chromium memory on 1 GB | OOM under long runs | Lite OS, no desktop, single tab; systemd restarts renderer |

## Validation checklist (3 Pis minimum)

Set-up: 3 Pis + 3 CRTs side by side, audio Pi = pi-02, keyboard on pi-01, phone on the AP,
laptop on Ethernet with SSH to all three (`journalctl -fu cyberspace-core`).

### H1 Composite output
- [ ] `configure-crt.sh --status` shows `Composite-1 connected`, mode 720x576i (PAL)
- [ ] whole 640×480 safe area visible on every CRT; adjust `display.overscan` if text is cut
- [ ] no cursor, no console text, no blanking after 30 min idle

### H2 Frame rate / load (per Pi, each scene, 5 min)
- [ ] dashboard fps ≥ 24 in every scene (renderer reports fps and draw time)
- [ ] `top`/dashboard: CPU %, RAM used, temperature, `get_throttled = 0x0`
- [ ] if fps < 24: try `--enable-gpu-rasterization` or `--disable-gpu` in `/etc/cyberspace2000/chromium-flags`, reduce `fps_cap`, report which scene

### H3 Failover duration
- [ ] pull power of the leader; stopwatch/video from power-off until all followers show FOLLOWER
  of the new leader in their journals (`state ... -> FOLLOWER`). Repeat 10×. Record min/median/max.
- [ ] old leader re-powered: joins as follower, leader does **not** change
- [ ] controlled handover from the dashboard: duration, picture never stops
- [ ] all CRTs keep animating and the beat stays visually aligned during the outage

### H4 Split brain
- [ ] carry one Pi out of range (or shield it) for 3 min → it becomes leader of its own AP;
  bring it back → within a few minutes exactly one leader; log shows `yielding` with reason

### H5 Sync jitter
- [ ] dashboard: per follower `jitter` and `±unc` (expect a few ms on a quiet channel)
- [ ] `Flash ALL CRTs` at 60 BPM, film at 240 fps: spread of first-lit frame across CRTs
  (same screen height) over 30 flashes → mean and SD in ms
- [ ] repeat while a phone streams video on the AP (load) and after 10 min without network
  (unplug leader, film the remaining two)

### H6 Audio
- [ ] `arecord -l` shows the USB interface; dashboard audio state LIVE within 10 s of music
- [ ] BPM matches the DJ mixer display (±0.1) for 100, 120, 128, 140, 160 BPM tracks
- [ ] **audio latency**: photodiode method (synchronization.md §7) — audio onset → photon delay,
  mean and SD over 50 beats, on each CRT
- [ ] silence 5 s → PREDICTED, music back → LIVE without phase jump > 1 bar
- [ ] unplug USB audio → NO_DEVICE, replug → recovers without restart
- [ ] break/ambient track: confidence drops, no wild tempo jumps

### H7 Calibration
- [ ] keyboard on pi-01: keys 1–3 select each CRT, flash visible only on the selected CRT
- [ ] 16–32 Enter taps → phase, SD, SE plausible; ±1/5/20 adjust visible immediately without restart
- [ ] save → offset survives: leader failover, `systemctl restart cyberspace-renderer`, reboot of that Pi
- [ ] camera check after calibration (all-CRT flash)

### H8 Robustness
- [ ] `systemctl restart cyberspace-core` on a follower: picture continues, rejoins in seconds
- [ ] kill Chromium (`pkill chromium`): back in < 10 s on the same scene/beat
- [ ] 8 h soak test: memory stable, no stuck states (`journalctl -p warning`)

### Results table

| item | Pi/CRT | value | date | notes |
|---|---|---|---|---|
| failover (s) min/med/max | | | | |
| sync spread camera (ms) mean/SD | | | | |
| audio→photon latency (ms) mean/SD | | | | |
| fps per scene | | | | |
| CPU % / RAM MB / temp °C | | | | |
