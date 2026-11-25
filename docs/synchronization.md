# Synchronisation and calibration

Goal: *convincing frame-scale synchronisation* (a PAL frame is 40 ms, a field 20 ms) of beat
reactive visuals across 3–5 CRTs and with the music. Not: claims of millisecond photon accuracy.

## 1. From sound to a beat timestamp (audio node)

1. PortAudio callback (48 kHz, 512-sample blocks ≈ 10.7 ms): converts `inputBufferAdcTime` to
   local `CLOCK_MONOTONIC` *inside the callback* and enqueues `(block, adc_time)` with
   `put_nowait` into a 64-entry queue. It never blocks; overflows are counted.
2. Analysis thread: spectral-flux onset detection (kick band < 200 Hz for phase, full band for
   tempo), onsets timestamped at the analysis-window centre on the **capture clock** — so
   buffer and processing latency do not shift beat times.
3. Tempo every 0.5 s from 8 s of onset envelope (autocorrelation, comb over multiples, log-Gaussian
   prior around 124 BPM, folded into 80–180 BPM). Half/double/⅔ candidates must persist ~8 s
   before an octave switch; genuine tempo changes need three agreeing estimates.
4. **Beat-PLL**: predicted beat grid; onsets within ±20 % of a period correct phase (gain 0.18)
   and period (regression over the last ~24 matched onsets); a comb-filter check every 2 s
   re-acquires phase if the PLL drifted to off-beats. Output per beat: BPM, **next future beat**
   time, confidence, phase uncertainty, state (ACQUIRING / LIVE / SILENCE).
5. Silence (RMS below threshold for 2.5 s) → SILENCE: no beats are emitted, the cluster timeline
   becomes PREDICTED and free-runs at the last tempo.

Systematic detector lag can be trimmed with `audio.beat_offset_ms`.

## 2. Into the cluster clock

The audio node converts `next_beat` from its local monotonic clock into cluster time using its
current clock-sync estimate and sends it to the leader. The leader (second smoothing stage:
3 ms deadband, 35 % phase gain, 50 % tempo gain for small errors) updates the replicated timeline
and broadcasts it. **Message arrival time is never used as beat time.**

## 3. Clock synchronisation

Receive times t2 (leader) and t4 (follower) are **kernel timestamps** (`SO_TIMESTAMPNS`, taken in
the network stack when the datagram arrives, converted to CLOCK_MONOTONIC), so Python/asyncio
scheduling delay does not enter the estimate; t3 is taken right before the reply is sent.
Followers scan for competing hotspots only every 3–4 min, because a Wi-Fi scan briefly takes the
radio off-channel and produces latency spikes.


Four-timestamp exchange (1 Hz, 6-ping burst after joining):
`offset = ((t2 − t1) + (t3 − t4)) / 2`, `delay = (t4 − t1) − (t3 − t2)`.

* window of 32 samples; samples with delay > 2 × minimum + 2 ms are rejected (Wi-Fi retries, power
  save, background scans)
* offset = median of the lowest-delay half (skew-corrected); **skew** from a regression over up
  to 30 min of our own estimates (a 32 s window cannot resolve tens of ppm)
* corrections ≤ 50 ms are **slewed** at ≤ 2 ms/s; larger ones are stepped and counted
  (`step_epoch`), and the renderer applies the visible jump only on a bar boundary
* during an outage the offset keeps evolving with the last skew; the reported **uncertainty**
  grows by 100 ppm × time since the last sample (worst-case crystal) — it is shown on every CRT
  (`±N ms`) and in the dashboard
* the browser does the same against its local core over the WebSocket (RTT < 1 ms typically),
  slews ≤ 5 ms/s, steps > 30 ms only at bar boundaries

## 4. Timeline reconciliation

When a follower receives a new timeline (or rejoins after an outage) it compares its own
predicted beat position with the authoritative one at the same cluster time:

* **tempo** is adopted immediately, phase-continuously
* phase/index difference ≤ 0.06 beat → **slewed** over 4 beats (≤ 1.5 % tempo bend, invisible)
* larger differences (incl. whole beats/bars after long outages or split brain) → held until the
  next **target bar downbeat**, then stepped: the CRT snaps into place exactly when all others
  start a bar

## 5. Rendering time and output latency

For each frame the renderer evaluates the scene at

```
t_eval = cluster(t_present) + display_latency − visual_offset
t_present = rAF timestamp + one refresh interval (predicted scan-out vsync)
```

* `requestAnimationFrame` is **not** photon emission. Chromium compositing, KMS page flip, the
  composite encoder and the CRT add latency. `timeline.display_latency_ms` (default 40) is an
  *assumption* until measured (camera/photodiode, §7).
* PAL is 50 Hz **interlaced**: the two fields of a frame are 20 ms apart, and a line at the bottom
  of the screen is scanned ~20 ms after the top within a field. Large flashes therefore span
  one field of uncertainty by nature. The renderer draws at 25 fps (every second vsync).
* **Beat-locked frames.** At 25 fps a frame stays 40 ms on screen, which would make beat events
  appear 0–40 ms late and differ between CRTs whose vsyncs are not aligned. The renderer therefore
  checks on *every* vsync whether an eighth-note boundary (or a calibration flash) became due; if
  so it draws immediately, between the regular frames, and evaluates the frame at the end of that
  vsync's window. Every musical event appears on the vsync nearest to it: error ±½ refresh
  (±10 ms at 50 Hz). Desktop test: mean 4 ms, p95 7 ms at 60 Hz. If the Pi cannot draw two frames
  in a row (draw time > 70 % of a refresh), the extra frame is skipped.
* The dashboard shows per CRT the measured refresh rate (**Hz**) and the render-side event error
  (**beat err ms**, mean / p95). It does not include display latency (measure that with a camera).
* The audio service runs on CPU core 3 with real-time round-robin priority; core and Chromium are
  pinned to cores 0–2, so the busy master Pi cannot starve audio capture.
* `visual_offset = global_visual_offset_ms + offsets_ms[node]` applies to **every** beat-reactive
  element because all of them derive from `t_eval`.

**Sign convention:** negative offset ⇒ visuals scheduled **earlier**; positive ⇒ later.

## 6. Enter-key calibration (alignment aid)

Purpose: equalise *relative* visual timing between CRTs (different composite paths, CRT
electronics) and roughly align visuals with the sound. It measures perceived alignment including
the operator's anticipation bias (person-dependent, often 10–50 ms) — **never** treat it as a
latency measurement.

### Procedure (USB keyboard on any Pi — preferred)

1. Plug a USB keyboard into the Pi whose CRT you can see (usually the leader at 192.168.50.1).
2. Press **1–5** to select `pi-01` … `pi-05`. The selected CRT shows a large green flash at
   120 BPM with a beat counter inside the overscan-safe area; other CRTs keep playing. The
   keyboard Pi's CRT shows a HUD with tap count, phase, SD, SE, confidence and suggestion.
3. Watch the flashing CRT and press **Enter** rhythmically, **anticipating** each flash (like
   tapping along to music), not reacting to it. Give 16–32 taps.
   * key-down events only; key repeat and key-up are ignored; presses closer than 0.35 beat to the
     previous one are discarded as double presses
   * timestamps come from the kernel (evdev with `EVIOCSCLOCKID = CLOCK_MONOTONIC`), converted to
     cluster time on that Pi, independent of Chromium or Wi-Fi delay
4. Read the result: phase estimate (+ = you tap after the scheduled beat, i.e. the flash looks
   late), circular SD, standard error, confidence, suggested offset = current − phase.
5. Adjust live without restarting anything: **↑/↓ ±5 ms, →/← ±1 ms, PgUp/PgDn ±20 ms,
   F = apply suggestion**. Adjusting clears the taps (old taps were measured with the old offset).
   Tap again to verify the phase is near 0.
6. **S** saves (replicated to all Pis, persisted, survives failover/reboot), **Esc** cancels
   (trial offset discarded), **R** resets the CRT to 0. **A** = all-CRT flash comparison,
   **C** = flash + audio click.

The same workflow is in the dashboard (Calibration tab: select CRT, TAP pad / Enter key, ±1/5/20,
save/cancel/reset). Browser taps use the DOM event timestamp and the admin WebSocket clock sync;
Wi-Fi and phone input latency make them noticeably less precise than the USB keyboard.

### Relative alignment of all CRTs

*Flash ALL CRTs* makes every CRT flash on the same beats, each with its own offset. Film all
screens together (§7) and adjust individual offsets until the flashes appear in the same video
frame / field.

### Audio click comparison

*Flash + audio click* additionally plays a click on each beat via WebAudio (kiosk) and optionally
in the admin browser ("click in this browser"). The click is scheduled from cluster time minus the
browser-reported `outputLatency`, which is frequently wrong on Linux and very wrong for Bluetooth
and many phones. Use it to judge audio-vs-visual alignment only with wired output and preferably
confirm with a camera that records sound.

## 7. Physical measurement procedures

### Slow-motion camera (120/240 fps)

1. Put all CRTs side by side in frame; set the camera to 240 fps (4.2 ms per frame) and lock exposure.
2. Start *Flash ALL CRTs* at 60 BPM (one flash per second, easier to count).
3. Record 20–30 flashes. Step through the video: for each flash note the first frame in which each
   CRT's flash is visible at the **same screen height** (scan position matters: bottom lines are
   ~20 ms later than top lines within a field).
4. Relative offset per CRT = mean frame difference × frame period. Enter the corrections
   (sign: if CRT B lights up 8 ms after A, give B −8 ms relative to A).
5. For audio-to-visual: record with sound, use a sharp transient (kick/click) from the PA, compare
   the audio waveform onset with the flash frame. Remember sound travels ~3 ms per metre.

### Photodiode (accurate)

1. Photodiode / phototransistor (e.g. BPW34 + 10 kΩ, or a light sensor module) taped to the CRT
   at a fixed screen position, read by an oscilloscope or a second sound card's line-in.
2. Feed the *same* audio that the DJ mixer sends to the audio Pi into the other scope / sound-card
   channel (or a click at known beat times).
3. Measure the delay between the audio onset and the light rise over ≥ 50 beats; the mean is the
   physical audio→photon latency of that CRT at that screen height, the SD its jitter.
4. Set `display_latency_ms` (system-wide) and per-CRT offsets so that the mean becomes ≈ 0.

These procedures, not tapping, are the basis for any statement in ms about physical alignment.
