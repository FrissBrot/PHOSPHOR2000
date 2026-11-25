# Troubleshooting

Always start with `sudo /opt/cyberspace2000/scripts/diagnostics.sh` and
`journalctl -u 'cyberspace-*' -f`.

| symptom | check / fix |
|---|---|
| CRT black, HDMI also black | composite cable pinout (video must be on the sleeve); `configure-crt.sh --status` must list `Composite-1`; on Pi 3 composite disables HDMI by design |
| picture rolls / black-and-white | wrong norm: `configure-crt.sh --norm PAL` (or NTSC) and reboot |
| edges cut off | increase `display.overscan` in the dashboard/config (shared, per cluster) |
| "CONNECTING TO LOCAL CORE" forever | `systemctl status cyberspace-core`; port 8080 in use? `ss -lntp` |
| kiosk not starting | `journalctl -u cyberspace-renderer`; `getty@tty1` must be masked; `cage` and `chromium` installed |
| no `CYBERSPACE-2000` network | `configure-network.sh --status`: country set? `rfkill` unblocked? any node LEADER? `nmcli connection up cyberspace-ap` by hand shows the NM error |
| node stuck in DEGRADED | hotspot/connect failed 3–6× (see reason in log); check `dnsmasq-base` installed, wlan0 exists, country set |
| two leaders for a long time | out of range of each other's APs — nobody can scan the other side; move nodes or wait for the lone-leader rescan; see network-recovery.md |
| admin page unreachable | you are on the AP? URL `http://192.168.50.1:8080/admin/`; with `--firewall` only the cluster subnet may connect |
| "no admin password configured" | `sudo cyberspace-passwd` on that Pi |
| commands say "forwarded to leader" | you are on a follower's dashboard; it works but the leader at 192.168.50.1 shows the authoritative state |
| audio stays ACQUIRING | input level: `arecord -D plughw:1 -f S16_LE -r 48000 -d 5 /tmp/t.wav`; `audio.device` name; mixer level; very soft/beatless music has low confidence by design |
| BPM doubled/halved | usual for some genres; adjust `audio.min_bpm/max_bpm`, or set MANUAL BPM |
| beats consistently early/late vs. sound | photodiode/camera measurement, then `global_visual_offset_ms` (all) or calibration (per CRT); detector lag: `audio.beat_offset_ms` |
| fps < 20 | `get_throttled`, temperature, scene benchmark; try Chromium flags in `/etc/cyberspace2000/chromium-flags`; lower `fps_cap` |
| radio fights / connects to home Wi-Fi | re-run installer with `--disable-other-wifi` |
| locked out after network change | Ethernet + SSH; or create `cyberspace-safe-mode` on the boot partition; `configure-network.sh --rollback` |
| wrong date/time on screen | Config → *set cluster clock from this device* |
