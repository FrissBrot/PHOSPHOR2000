# Architecture

CYBERSPACE 2000 runs **identical software on every Pi** (3–5 × Raspberry Pi 3 B/B+, each driving a
4:3 PAL CRT over composite). There is no permanent master: whichever node currently owns the
Wi-Fi hotspot is the *leader* and the authority for replicated state. Every node renders locally,
so the network is an optimisation for synchronisation and control, never a dependency of the picture.

```
                 ┌──────────────────────── one Raspberry Pi ────────────────────────┐
 USB line-in ──► │ cyberspace-audio  (PortAudio callback → bounded queue → NumPy     │
 (optional)      │                    beat tracker: onsets, tempo, Beat-PLL)         │
                 │        │ unix datagram (/run/cyberspace2000/core.sock)            │
 USB keyboard ─► │ cyberspace-input  (evdev, kernel CLOCK_MONOTONIC timestamps)      │
 (optional)      │        ▼                                                          │
                 │ cyberspace-core   asyncio: 20 Hz tick                             │
                 │   ├─ Election      (network state machine, hotspot ownership)    │
                 │   │    └─ NmcliAdapter (worker thread, fixed argv)  ── nmcli ──► NetworkManager
                 │   ├─ ClockSync     (4-timestamp exchange, skew, slew/step)        │
                 │   ├─ Timeline      (bpm/anchor/slew, LIVE/PREDICTED/MANUAL)       │
                 │   ├─ Scheduler     (scene plan in beats, overrides, eras)         │
                 │   ├─ Calibration   (tap statistics, per-CRT offsets)              │
                 │   ├─ UDP 47800     (HMAC-SHA256 datagrams: beacons, sync, state)  │◄──► other Pis
                 │   └─ FastAPI :8080 (admin API + WS, renderer WS, static files)    │◄──► admin phone/laptop
                 │        │ ws://127.0.0.1:8080/ws/render                            │
                 │ cyberspace-renderer  cage (Wayland) + Chromium kiosk on tty1      │
                 │        Canvas 2D 640×480 virtual → composite 720×576i PAL → CRT   │
                 └───────────────────────────────────────────────────────────────────┘
```

## Processes and supervision (systemd)

| unit | role | why separate |
|---|---|---|
| `cyberspace-core` | network/election, sync, replication, API/WebSocket server | one event loop; nmcli on a worker thread so radio switching never blocks ticks or the renderer feed |
| `cyberspace-audio` | capture + beat tracking (only does real work if a USB capture device exists) | real-time-ish, crash isolated, restartable without touching the network |
| `cyberspace-input` | keyboard events with kernel timestamps for calibration | needs group `input` only |
| `cyberspace-renderer` | cage + Chromium kiosk | browser crashes are common; restarted in 3 s, renders from cache |
| `cyberspace-firewall` (optional) | separate nftables table | restricts 8080/47800 to 192.168.50.0/24 |

"Network", "sync" and "server" from the requirements live inside `cyberspace-core` as independent
components of one asyncio loop: they share the cluster state machine and must agree on one clock
estimate, and splitting them would add IPC latency to the timing path. Each tick catches and logs
exceptions so one failing component cannot stop the others; systemd restarts the process on crash,
and a restarted core restores the exact clock offset and beat phase from its cache (same boot id).

## Clock domains

* **local** — `CLOCK_MONOTONIC` of one Pi (`time.monotonic()`, evdev timestamps, PortAudio ADC
  time converted in the callback). *Never* compared across machines.
* **cluster** — a virtual timebase. On the leader `cluster = local + frozen_offset`; followers
  estimate `offset` by four-timestamp exchange. On failover the new leader freezes *its current
  estimate*, so cluster time stays continuous across leaders.
* **wall** — `cluster + wall_offset`, distributed by the leader, only for the on-screen date/time
  (Pi 3 has no RTC; the admin can set the cluster clock from a phone).
* **browser** — `performance.now()`; the renderer estimates `cluster − perf` over its local WebSocket.

## Replicated state (leader → all)

`state` datagrams (every 2 s and on change, zlib + HMAC) carry: timeline (bpm, anchor, slew,
confidence, source, revision, epoch, seed), schedule (eras, overrides), shared config (scenes,
safety, calibration offsets with per-entry versions, timeline offsets, display), wall offset,
manual flag and the calibration session. Followers keep a cached copy on disk
(`/var/lib/cyberspace2000/state.json`, atomic writes, at most every 10 s).

## Musical timeline

`beat(t) = anchor_beat + (t − anchor_time)·bpm/60 + slew(t)` — the same formula in Python
(`backend/sync/timeline.py`) and JavaScript (`frontend/js/engine/timeline.js`), verified against
shared golden vectors. Scenes, subdivisions, flashes and PRNG seeds are all functions of the beat
position, so every CRT computes identical frames from the same replicated state without per-beat
messages, and keeps doing so during an outage.

## Code map

| path | content |
|---|---|
| `backend/config.py` | typed configuration (pydantic), layering default → node → secrets |
| `backend/cluster/election.py` | network states, backoff, split-brain reconciliation |
| `backend/cluster/node.py` | composition of election, sync, timeline authority, commands, cache |
| `backend/cluster/scheduler.py` | deterministic scene plan |
| `backend/cluster/simulation.py` | discrete-event simulator running the real node code |
| `backend/cluster/messages.py` | HMAC-authenticated datagram codec with replay protection |
| `backend/network/nmcli.py` | NetworkManager adapter + keyfile writer |
| `backend/sync/clocksync.py` | offset/skew estimation, slew/step |
| `backend/sync/calibration.py` | circular statistics for Enter-key calibration |
| `backend/audio/analysis.py` | beat tracker; `service.py` capture + IPC |
| `backend/api/` | FastAPI app, auth, validated command models |
| `backend/services/` | core daemon, keyboard reader, whitelisted system actions |
| `frontend/` | kiosk renderer (engine, scenes, link) |
| `admin/` | dashboard (no CDN) |
| `scripts/`, `systemd/` | deployment |
