# Test report

**Status: all results below are SIMULATED or from desktop software. No result has been obtained
on Raspberry Pi 3 / CRT hardware yet** — use docs/hardware.md to produce hardware numbers.

Environment: Debian 13 x86_64 container, Python 3.11, Node 24, headless Chromium (Playwright build
1248). Date: 2026-11-25.

## Automated test suite (`pytest`, 107 tests)

| area | file | kind |
|---|---|---|
| musical timeline, phase continuity, reconciliation, tap tempo | tests/test_timeline.py | unit |
| scene scheduling, quantised overrides, choreography, config eras | tests/test_scheduler.py | unit |
| Python↔JS parity (PRNG, timeline incl. slews) | tests/test_golden.py + tests/js/ | unit (golden vectors) |
| election state machine, claim order, backoff, holddown, scouting | tests/test_election.py | unit |
| clock sync: convergence, outliers, slew/step, drift free-run | tests/test_clocksync.py | unit |
| simultaneous boot (3/5), master crash + return, outage prediction, split brain, no oscillation, controlled handover, beat agreement at 100/120/128/140/160 BPM, audio loss, audio-node loss, offset persistence/merge | tests/test_cluster_sim.py | **integration, simulated** (discrete-event simulator running the real node code with drifting clocks, Wi-Fi latency/loss/spikes and radio switching delays) |
| beat tracker: tempo/phase at 100–174 BPM, noise, tempo change, half-time, silence, non-blocking callback | tests/test_audio.py | unit, **synthetic audio** |
| calibration statistics: sign, wrapping, missing taps, outliers, key repeat/double press, evdev parsing | tests/test_calibration.py | unit |
| API: auth, rate limit, CSRF, validation, commands, calibration flow, export/import, WebSockets, keyboard mode | tests/test_api.py | integration (in-process) |
| renderer: all scenes without errors, full stack core→WS→renderer, **browser restart**, throttled benchmark, admin dashboard flows | tests/test_browser.py | integration, **desktop Chromium** |
| composite script idempotency/rollback (fake boot dir), keyfiles, nmcli adapter argv | tests/test_scripts.py | unit with fakes |

Run: `.venv/bin/python -m pytest -q` (browser tests skip automatically without Chromium).

## Simulated cluster results (`python -m tools.sim_report`)

Simulator model: radio scan 3 s, hotspot start 6 s, association 4 s, link-loss detection 2.5 s,
one-way latency 2 ms + exp(6 ms), 4 % spikes of +20–120 ms, 1 % loss, clocks ±40 ppm with random
origins, audio onset noise 4 ms. Real Pi 3 radio timings are likely slower and burstier.

| scenario | nodes | runs | result (min / median / max) |
|---|---|---|---|
| simultaneous cold boot -> single leader (s) | 3 | 20 | 23.2 / 25.0 / 39.0 |
| leader crash -> replacement leader, all following (s) | 3 | 20 | 23.5 / 24.2 / 25.8 |
| beat spread between CRTs 20 s after failover (ms) | 3 | 20 | 0.1 / 0.6 / 1.9 |
| simultaneous cold boot -> single leader (s) | 5 | 20 | 23.2 / 25.0 / 37.5 |
| leader crash -> replacement leader, all following (s) | 5 | 20 | 23.8 / 25.0 / 26.2 |
| beat spread between CRTs 20 s after failover (ms) | 5 | 20 | 0.2 / 1.6 / 2.7 |
| split brain (2 hotspots) healed -> single leader (s) | 5 | 20 | 15.8 / 53.4 / 127.5 |
| LIVE 100 BPM: future-beat spread across CRTs (ms) | 4 | 5 | 0.8 / 1.2 / 1.6 |
| LIVE 100 BPM: worst CRT beat vs. true music beat (ms) | 4 | 5 | 1.0 / 1.7 / 2.3 |
| LIVE 120 BPM: future-beat spread across CRTs (ms) | 4 | 5 | 0.6 / 0.8 / 2.6 |
| LIVE 120 BPM: worst CRT beat vs. true music beat (ms) | 4 | 5 | 0.7 / 2.2 / 4.9 |
| LIVE 128 BPM: future-beat spread across CRTs (ms) | 4 | 5 | 1.0 / 1.4 / 1.9 |
| LIVE 128 BPM: worst CRT beat vs. true music beat (ms) | 4 | 5 | 2.3 / 3.0 / 4.9 |
| LIVE 140 BPM: future-beat spread across CRTs (ms) | 4 | 5 | 1.1 / 1.3 / 2.2 |
| LIVE 140 BPM: worst CRT beat vs. true music beat (ms) | 4 | 5 | 0.7 / 3.8 / 4.3 |
| LIVE 160 BPM: future-beat spread across CRTs (ms) | 4 | 5 | 0.5 / 1.2 / 2.2 |
| LIVE 160 BPM: worst CRT beat vs. true music beat (ms) | 4 | 5 | 0.4 / 3.0 / 4.2 |
| 2 survivors isolated 120 s (no network at all): beat spread (ms) | 3 | 10 | 0.4 / 1.7 / 5.0 |

## Audio (synthetic signals, `tests/test_audio.py`)

Kick on every beat + off-beat hats + noise, 48 kHz / 512: tempo within 0.3 %, predicted future
beat within ~1–5 ms mean, < 2 ms SD after lock (100–174 BPM). Half-time kick patterns keep the
octave. Analysis cost ~1.6 % of one x86 core in real time; estimated ~15 % of one Pi 3 core
(not measured).

## Renderer benchmark (desktop Chromium, CPU throttled 6×)

JavaScript draw time per frame (p50 / p95 ms): boot 1.4/2.9, dance 1.0/1.9, spectrum 1.7/2.9,
grid 1.1/1.9, rain 6.2/9.0, countdown 1.0/1.8, calibration 0.7/1.9. Budget at 25 fps: 40 ms.
This excludes rasterisation/compositing in Chromium's GPU process and is **not** a Pi 3
measurement; H2 in docs/hardware.md is the real test.

## Known limitations

* No hardware validation yet (radio timings, composite latency, Chromium on Pi 3 fps, real music).
* Split-brain healing time depends on scan intervals (sim median ~53 s, max ~128 s).
* Configuration edits in a losing partition are discarded (offsets are merged).
* The beat tracker is designed for four-on-the-floor DJ music; breaks, ambient or heavy
  syncopation lower confidence (→ PREDICTED/MANUAL).
