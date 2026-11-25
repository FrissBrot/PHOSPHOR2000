// Kiosk entry point. URL options (development / benchmarking only):
//   ?demo=1            run without a core (local clock, synthetic state)
//   ?scene=rain        force a scene (with demo)
//   ?bench=1           cycle all scenes and record frame times in window.__bench
//   ?offset=-20        override visual offset (demo)
import { BrowserClock } from "./engine/clock.js";
import { Renderer } from "./engine/renderer.js";
import { Link } from "./services/link.js";
import { PLAYABLE } from "./scenes/index.js";

// Make sure the bundled VT323 is decoded before the first frames (canvas does not trigger font loads).
document.fonts.load('40px "VT323"').catch(() => {});
document.fonts.load('40px "VT323"', "ÄÖÜäöü").catch(() => {});

const params = new URLSearchParams(location.search);
const canvas = document.getElementById("screen");

// Composite PAL (720x576, ratio 1.25) is shown 4:3 by the CRT itself -> stretch to the full output.
// Any other output (e.g. an HDMI monitor on the master, 16:9) -> keep 4:3 with black bars.
function fitCanvas() {
  const r = window.innerWidth / Math.max(1, window.innerHeight);
  const composite = Math.abs(r - 1.25) < 0.03 || Math.abs(r - 720 / 480) < 0.03;
  if (composite || Math.abs(r - 4 / 3) < 0.03) {
    canvas.style.width = "100vw"; canvas.style.height = "100vh"; canvas.style.marginTop = "0";
  } else {
    const h = Math.min(window.innerHeight, window.innerWidth * 0.75);
    canvas.style.height = `${h}px`; canvas.style.width = `${h * 4 / 3}px`;
    canvas.style.margin = `${(window.innerHeight - h) / 2}px auto 0`;
  }
}
window.addEventListener("resize", fitCanvas);
fitCanvas();
const clock = new BrowserClock();
const renderer = new Renderer(canvas, clock);
window.__phos = renderer;

function demoState(scene) {
  const segs = [];
  const order = PLAYABLE;
  for (let i = 0; i < 400; i++) {
    const sc = scene || order[i % order.length];
    segs.push({ scene: sc, start_beat: i * 16, end_beat: (i + 1) * 16, seed: 0x2000 + i * 7919, origin_beat: i * 16 });
  }
  return {
    t: "render_state", node: params.get("node") || "pi-01", state: "DEMO", leader: null,
    timeline: { bpm: Number(params.get("bpm") || 128), anchor_time: 0, anchor_beat: 0, beats_per_bar: 4,
                source: "PREDICTED", seed: 0x2000 },
    segments: segs, visual_offset_ms: Number(params.get("offset") || 0), display_latency_ms: 40,
    wall_offset: Date.now() / 1000 - performance.now() / 1000,
    sync: { uncertainty_ms: 0 }, audio_fresh: false,
    scenes: { intensity: 0.8, countdown_target: "2027-02-05T20:00:00", countdown_label: "MATTLIBALL", phosphor: params.get("phosphor") || "#39ff14" },
    safety: { reduced_flashing: params.get("reduced") === "1", max_flash_hz: 3, max_flash_luminance: 0.85 },
    display: { overscan: { top: 0.06, bottom: 0.06, left: 0.07, right: 0.07 }, fps_cap: Number(params.get("fps") || 25),
               glow: Number(params.get("glow") ?? 1), crt_overlay: params.get("overlay") !== "0", hud: params.get("hud") === "1" },
    cal: params.get("cal") ? { active: true, mode: params.get("cal"), target: params.get("node") || "pi-01",
                               bpm: 120, origin: 1, tap_count: 0, result: null } : { active: false },
  };
}

if (params.get("demo") || params.get("bench")) {
  // Local clock: cluster time := performance.now() seconds.
  clock.applied = clock.target = 0;
  const bench = params.get("bench");
  renderer.setState(demoState(params.get("scene")));
  if (bench) {
    const scenes = [...PLAYABLE, "calibration"];
    const per = Number(params.get("secs") || 4) * 1000;
    const res = {};
    let idx = -1, startedAt = 0, times = [];
    window.__bench = { done: false, results: res };
    const origDraw = renderer.draw.bind(renderer);
    renderer.draw = (ts) => {
      const now = performance.now();
      if (idx < 0 || now - startedAt > per) {
        if (idx >= 0) {
          times.sort((a, b) => a - b);
          const p = (q) => times[Math.min(times.length - 1, Math.floor(q * times.length))];
          res[scenes[idx]] = { frames: times.length, fps: times.length / (per / 1000),
                               draw_ms_p50: p(0.5), draw_ms_p95: p(0.95), draw_ms_max: times.at(-1) };
        }
        idx++;
        if (idx >= scenes.length) { window.__bench.done = true; idx = 0; }
        const sc = scenes[idx];
        const st = demoState(sc === "calibration" ? null : sc);
        if (sc === "calibration") st.cal = { active: true, mode: "single", target: st.node, bpm: 120, origin: 0, tap_count: 0 };
        renderer.setState(st);
        startedAt = now; times = [];
      }
      const t0 = performance.now();
      origDraw(ts);
      times.push(performance.now() - t0);
    };
  }
} else {
  const link = new Link(clock, (st) => renderer.setState(st), (f) => renderer.setFeatures(f));
  const cached = link.loadCache();
  if (cached) renderer.setState(cached);
  link.connect();
  let lastReport = 0;
  renderer.onStats = null;
  setInterval(() => {
    const seg = renderer.lastSegKey || "";
    const ev = renderer.eventStats();
    const r1 = (x) => (x === null ? null : Math.round(x * 10) / 10);
    link.report({ fps: Math.round(renderer.fps() * 10) / 10, frame_ms: Math.round(renderer.drawMsAvg * 100) / 100,
                  dropped: renderer.dropped, scene: seg.split("@")[0],
                  refresh_hz: r1(1000 / renderer.refreshMs), beat_frames: renderer.forcedFrames,
                  event_err_ms: r1(ev.mean), event_err_p95_ms: r1(ev.p95),
                  browser_rtt_ms: clock.rttMs, ua: navigator.userAgent.slice(0, 80) });
  }, 2000);
}
renderer.start();
