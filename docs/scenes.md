# Scenes

The twelve visuals are ports of the "Cyberspace 2000 – CRT Visuals" design canvas
(1024×768 artboards) to Canvas 2D. Each scene is drawn in the 1024×768 design space and mapped
into the overscan-safe area of the 640×480 canvas (`display.overscan`), so nothing important is
lost behind a CRT bezel.

| id | artboard | beat behaviour |
|---|---|---|
| `boot` | 01 Boot / Titel | boot lines typed every beat, "2000" pulses on the beat, header shows node id and beat 1–4 |
| `dance` | 02 Dance.exe | five ASCII dancers change moves on eighths, routines rotate every bar, light floor |
| `spectrum` | 03 Spectrum Analyzer | **live** 16 bands from the audio node (`CH-1`), otherwise the design's synthetic pattern labelled `SYNTH` |
| `grid` | 04 Neon Grid + Cube | floor scrolls one cell per beat, cube turns once per 8 beats and bobs on the beat |
| `rain` | 05 Data Rain | 20 seeded columns, fall speed locked to tempo, terminal messages every 4 bars |
| `countdown` | 06 Y2K Countdown | 23:59:50 → 00:00:00 one second per beat, Y2K-bug glitches (no skew/jitter in reduced-flashing mode) |
| `mattliball` | 07 Mattliball 2027 | countdown to `scenes.countdown_target` (default 2027-02-05 20:00) in days |
| `guggen` | 08 Guggä-Register | 4/4 pattern: percussion every eighth, sousaphon on 1+3, posaunen, trompeten |
| `stollen` | 09 Stollä-Tunnel | ten tunnel arches, one full flight every bar |
| `mottos` | 10 Motto.db | one motto (1990–2026) every two beats |
| `bars` | 11 Bar-Netzwerk | active bar changes every beat, packet travels hub → bar within the beat |
| `hell` | 12 Bis es hell wird | real cluster clock, night progress 20:00 → 07:30 |

Differences to the design canvas, all on purpose:

* **No TAP TEMPO buttons** on the CRTs — tempo comes from the audio node, tap tempo is in the
  admin dashboard (Timing tab).
* **Time base**: the designs counted half-beats with `setTimeout`; here every counter is derived
  from the shared musical timeline (`floor(sceneBeat × 2)`), so all CRTs are in step and keep
  running through network outages. CSS animations (rain, floor, cube, packets, tunnel) are
  recomputed from the beat position.
* **Fonts**: VT323 is bundled (`frontend/assets/fonts`, SIL OFL) — the CRTs are offline, Google
  Fonts are not available. Block characters fall back to DejaVu Sans Mono.
* **Glow** (CSS text-shadow) is a canvas `shadowBlur`; it is the most expensive effect on a Pi 3.
  `display.glow` 0/1/2 (dashboard → Scenes → Look). Benchmark on the Pi (hardware.md H2).
* **Scanlines/vignette/flicker** are one pre-rendered overlay plus a time-derived dip
  (`display.crt_overlay`); flicker is disabled in reduced-flashing mode.
* **Phosphor colour** `scenes.phosphor`: green P1 `#39ff14` (default), amber `#ffb000`, blue
  `#7dd3fc`, white `#e8f4ff` — as the design's colour property.
* On-beat brightness alternations are softened when *reduced flashing* is on.

Programme: the default playlist (config/default.yaml) plays all twelve; change order, bar
lengths, per-CRT assignments and choreography in the dashboard (Scenes tab).
