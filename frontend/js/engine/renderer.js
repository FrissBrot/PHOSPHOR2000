// Frame loop: time-derived, deterministic rendering on the cluster timeline.
//
// Evaluation time for a frame:
//   t_eval = cluster(t_present) + display_latency - visual_offset
// where t_present is the *predicted* vsync at which this frame is scanned out
// (rAF timestamp + one refresh interval). display_latency is a configured
// estimate (Chromium compositor + KMS + composite encoder + CRT), NOT a
// measurement; visual_offset is the per-CRT calibration (negative = earlier).
import { Timeline } from "./timeline.js";
import { FlashGuard } from "./safety.js";
import { GlyphAtlas, GREEN, GREEN_DIM, font } from "./glyphs.js";
import { hash01 } from "./prng.js";
import { SCENES } from "../scenes/index.js";

const W = 640, H = 480;
const ASCII = " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~";

export class Renderer {
  constructor(canvas, clock, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d", { alpha: false, desynchronized: true });
    this.clock = clock;
    this.opts = opts;
    this.state = null;
    this.timeline = new Timeline();
    this.features = null;
    this.featuresAt = -1e9;
    this.guard = new FlashGuard();
    this.refreshMs = 20;
    this.lastTs = null;
    this.lastDraw = -1e9;
    this.frames = 0; this.dropped = 0; this.drawMsAvg = 0; this.forcedFrames = 0;
    this.lastEventKey = null; this.evErrs = [];
    this.fpsWindow = [];
    this.lastSegKey = null;
    this.segStartBeat = 0;
    this.atlas = {
      ascii: new GlyphAtlas(16, ASCII),
    };
    this.env = { W, H, atlas: this.atlas, guard: this.guard, sceneState: {} };
    this.audioCtx = null;
    this.nextClick = null;
  }

  setState(st) {
    this.state = st;
    this.timeline = new Timeline(st.timeline);
    this.guard.configure(st.safety);
    if (st.display && st.display.fps_cap) this.fpsCap = st.display.fps_cap;
  }

  setFeatures(f) { this.features = f; this.featuresAt = performance.now(); }

  start(onFrameStats) {
    this.onFrameStats = onFrameStats;
    const loop = (ts) => { this.frame(ts); requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  }

  frame(ts) {
    if (this.lastTs !== null) {
      const d = ts - this.lastTs;
      if (d > 5 && d < 100) this.refreshMs += (d - this.refreshMs) * 0.05;
    }
    this.lastTs = ts;
    const cap = this.fpsCap || 25;
    const minGap = 1000 / cap - this.refreshMs * 0.5;
    const regular = ts - this.lastDraw >= minGap;   // e.g. 50 Hz refresh -> every 2nd vsync
    // Beat-locked frames: if a musical event (eighth-note boundary, calibration flash)
    // became due since the last drawn frame, this vsync is the one nearest to it -> draw now,
    // even between the regular 25 fps frames. Error per event: +-half a refresh (+-10 ms at 50 Hz)
    // instead of 0..40 ms. Skipped if the Pi is too slow to draw two frames in a row.
    let forced = false;
    if (!regular) {
      const ev = this.eventKey(this.evalTime(ts));
      forced = ev !== null && ev !== this.lastEventKey && this.drawMsAvg < this.refreshMs * 0.7;
      if (!forced) return;
      this.forcedFrames++;
    }
    if (this.lastDraw > 0 && ts - this.lastDraw > 2.5 * (1000 / cap)) this.dropped++;
    this.lastDraw = ts;
    this.clock.update(ts);
    const t0 = performance.now();
    try { this.draw(ts); } catch (e) { this.drawError(e); }
    const dt = performance.now() - t0;
    this.drawMsAvg += (dt - this.drawMsAvg) * 0.05;
    this.frames++;
    this.fpsWindow.push(ts);
    while (this.fpsWindow.length && ts - this.fpsWindow[0] > 2000) this.fpsWindow.shift();
    if (this.onFrameStats) this.onFrameStats(this);
  }

  fps() { return this.fpsWindow.length > 1 ? (this.fpsWindow.length - 1) / ((this.fpsWindow.at(-1) - this.fpsWindow[0]) / 1000) : 0; }

  // Identifies the current musical event window; changes exactly at eighth-note boundaries of
  // the timeline and at calibration flashes.
  eventKey(t) {
    if (t === null || t === undefined) return null;
    const cal = (this.state && this.state.cal) || {};
    let key = Math.floor(this.timeline.beatAt(t) * 2);
    if (cal.active && cal.bpm) key = key * 1000 + (Math.floor(((t - cal.origin) * cal.bpm) / 60) & 511);
    return key;
  }

  // Event-timing error of the frame drawn now: physical display start minus the physical time
  // at which the event boundary was due (render side only; excludes display latency).
  trackEventError(ts, t) {
    const key = this.eventKey(t);
    if (key === this.lastEventKey) return;
    const prev = this.lastEventKey;
    this.lastEventKey = key;
    if (prev === null || prev === undefined || this.clock.applied === null) return;
    const tl = this.timeline;
    const b = Math.floor(tl.beatAt(t) * 2) / 2;                       // last eighth boundary
    const st = this.state || {};
    const lat = (st.display_latency_ms ?? 40) / 1000, off = (st.visual_offset_ms ?? 0) / 1000;
    const due = (tl.timeOfBeat(b) - lat + off - this.clock.applied) * 1000;   // perf ms when the event should be visible
    const err = ts + this.refreshMs - due;
    if (Math.abs(err) > 200) return;  // timeline jump, not a frame-timing error
    this.evErrs.push(err);
    if (this.evErrs.length > 200) this.evErrs.shift();
  }

  eventStats() {
    const a = this.evErrs.map(Math.abs).sort((x, y) => x - y);
    if (!a.length) return { mean: null, p95: null };
    return { mean: a.reduce((s, x) => s + x, 0) / a.length, p95: a[Math.floor(a.length * 0.95)] };
  }

  evalTime(ts) {
    const st = this.state || {};
    // Frame drawn at rAF time ts is scanned out from the next vsync (ts + refresh). Evaluate at the
    // END of that refresh's window: every event whose nearest vsync is this one is already shown.
    const present = ts + this.refreshMs * 1.5;
    let c = this.clock.clusterAt(present);
    if (c === null) return null;
    const lat = (st.display_latency_ms ?? 40) / 1000;
    const off = (st.visual_offset_ms ?? 0) / 1000;
    return c + lat - off;
  }

  segmentAt(beat) {
    const segs = (this.state && this.state.segments) || [];
    for (const s of segs) if (beat >= s.start_beat && beat < s.end_beat) return s;
    if (segs.length) {
      const last = segs[segs.length - 1];
      if (beat >= last.end_beat) return { ...last, end_beat: Infinity };
      return segs[0];
    }
    return { scene: "boot", start_beat: 0, end_beat: Infinity, seed: 0x2000, origin_beat: 0 };
  }

  draw(ts) {
    const ctx = this.ctx;
    const st = this.state || {};
    let t = this.evalTime(ts);
    if (t === null) { this.drawWaiting(ts); return; }
    const tl = this.timeline;
    // Large clock corrections are applied only on a bar boundary.
    if (this.clock.pendingStep) {
      const bpb = tl.beats_per_bar;
      const b = tl.beatAt(t);
      if (Math.floor(b / bpb) !== Math.floor(this._lastBeat / bpb)) { this.clock.applyStep(); t = this.evalTime(ts); }
    }
    const beat = tl.beatAt(t);
    this._lastBeat = beat;
    this.trackEventError(ts, t);
    const phase = beat - Math.floor(beat);
    const bpb = tl.beats_per_bar;
    const seg = this.segmentAt(beat);
    const cal = st.cal || {};
    const calOn = cal.active && (cal.mode === "all" || cal.mode === "click" || cal.target === st.node);
    const sceneName = calOn ? "calibration" : seg.scene;
    const segKey = calOn ? "cal" : `${seg.scene}@${seg.origin_beat}`;
    if (segKey !== this.lastSegKey) { this.lastSegKey = segKey; this.env.sceneState = {}; }
    const audioLive = !!st.audio_fresh && performance.now() - this.featuresAt < 600 && this.features;
    const f = {
      t, beat, phase, bpb,
      bar: Math.floor(beat / bpb), beatInBar: Math.floor(beat) % bpb + (beat < 0 ? bpb : 0),
      eighth: Math.floor(beat * 2), sixteenth: Math.floor(beat * 4),
      kick: Math.exp(-phase * 7),
      sceneBeat: beat - seg.origin_beat, segment: seg,
      seed: seg.seed >>> 0, tl,
      intensity: (st.scenes && st.scenes.intensity) ?? 0.8,
      wall: new Date((t + (st.wall_offset || 0)) * 1000),
      node: st.node || "pi-??",
      state: st.state || "OFFLINE",
      source: tl.source,
      audio: audioLive ? this.features : synthAudio(beat, seg.seed),
      audioLive: !!audioLive,
      countdown: st.scenes || {},
      safe: safeRect(st.display),
      cal, calTarget: cal.target, isCalTarget: cal.target === st.node, kbdHud: !!st.kbd_hud,
      sync: st.sync || {},
      offsetMs: st.visual_offset_ms ?? 0,
      st,
    };
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = 1;
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, W, H);
    const scene = SCENES[sceneName] || SCENES.boot;
    scene.draw(ctx, f, this.env);
    if (!calOn) this.transition(ctx, f, seg);
    if (calOn && cal.mode === "click") this.scheduleClicks(f);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.shadowBlur = 0;
    this.hud(ctx, f);
  }

  // Short deterministic glitch-wipe during the first half beat of a scene instance.
  transition(ctx, f, seg) {
    const d = f.beat - seg.start_beat;
    if (d < 0 || d > 0.5 || seg.start_beat <= (this.state?.segments?.[0]?.start_beat ?? -1e9)) return;
    const k = 1 - d / 0.5;
    ctx.fillStyle = "#000";
    const rows = 12;
    for (let i = 0; i < rows; i++) {
      const r = hash01(seg.seed, i, Math.floor(f.sixteenth));
      const w = W * k * (0.4 + 0.6 * r);
      ctx.fillRect(r > 0.5 ? 0 : W - w, (i * H) / rows, w, H / rows + 1);
    }
    ctx.fillStyle = GREEN;
    ctx.globalAlpha = 0.6 * k;
    ctx.fillRect(0, H * hash01(seg.seed, 99, f.sixteenth), W, 3);
    ctx.globalAlpha = 1;
  }

  hud(ctx, f) {
    const s = f.safe;
    const alerts = f.st.alerts || [];
    if (alerts.length) {
      ctx.fillStyle = "#000";
      ctx.fillRect(s.x - 4, s.y - 2, s.w + 8, 22);
      ctx.fillStyle = "#ffb000";
      ctx.font = font(16);
      ctx.textBaseline = "top";
      ctx.fillText("!! " + alerts[0], s.x, s.y);
      ctx.textBaseline = "alphabetic";
    }
    const abnormal = !["FOLLOWER", "LEADER", "DEMO"].includes(f.state);
    const show = (f.st.display && f.st.display.hud) || abnormal;
    if (!show) return;
    ctx.font = font(14);
    ctx.textBaseline = "alphabetic";
    ctx.fillStyle = "#000";
    ctx.fillRect(s.x - 4, s.y + s.h - 15, s.w + 8, 20);  // keep HUD legible over busy scenes
    ctx.fillStyle = GREEN_DIM;
    const src = f.source === "LIVE" ? "LIVE" : f.source === "MANUAL" ? "MAN" : "PRED";
    const unc = f.sync.uncertainty_ms == null ? "" : ` ±${Math.round(f.sync.uncertainty_ms)}ms`;
    ctx.fillText(`${f.node} ${f.tl.bpm.toFixed(1)} ${src}${f.audioLive ? "" : " SYN"}${unc}`, s.x, s.y + s.h);
    if (f.state !== "FOLLOWER" && f.state !== "LEADER") {
      ctx.textAlign = "right";
      ctx.fillText(f.state, s.x + s.w, s.y + s.h);
      ctx.textAlign = "left";
    }
  }

  drawWaiting(ts) {
    const ctx = this.ctx;
    ctx.fillStyle = "#000"; ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = GREEN; ctx.font = font(24);
    ctx.fillText("PHOSPHOR2000", 60, 200);
    ctx.font = font(16);
    ctx.fillText("CONNECTING TO LOCAL CORE" + ".".repeat(Math.floor(ts / 400) % 4), 60, 240);
  }

  drawError(e) {
    if (!this._errLogged) { console.error(e); this._errLogged = true; }
    const ctx = this.ctx;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = "#000"; ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = GREEN; ctx.font = font(16);
    ctx.fillText("RENDER FAULT - RECOVERING", 60, 240);
  }

  // Calibration click via WebAudio: scheduled on AudioContext time mapped from
  // cluster time. outputLatency is browser-reported and often inaccurate on Linux.
  scheduleClicks(f) {
    try {
      if (!this.audioCtx) this.audioCtx = new AudioContext({ latencyHint: "interactive" });
      const ac = this.audioCtx;
      if (ac.state === "suspended") ac.resume();
      const cal = f.cal;
      const period = 60 / cal.bpm;
      const perfNow = performance.now();
      const cNow = this.clock.clusterAt(perfNow);
      const k = Math.ceil((cNow - cal.origin) / period);
      if (this.nextClick === k) return;
      const when = cal.origin + k * period;               // cluster time of the beat
      const lead = when - cNow - (ac.outputLatency || ac.baseLatency || 0);
      if (lead < 0.02 || lead > 0.6) return;
      const at = ac.currentTime + lead;
      const o = ac.createOscillator(), g = ac.createGain();
      o.frequency.value = k % 4 === 0 ? 1760 : 880;
      g.gain.setValueAtTime(0.0001, at);
      g.gain.exponentialRampToValueAtTime(0.5, at + 0.002);
      g.gain.exponentialRampToValueAtTime(0.0001, at + 0.05);
      o.connect(g).connect(ac.destination);
      o.start(at); o.stop(at + 0.06);
      this.nextClick = k;
    } catch (e) { /* no audio output available */ }
  }
}

export function safeRect(display) {
  const o = (display && display.overscan) || { top: 0.06, bottom: 0.06, left: 0.07, right: 0.07 };
  const x = Math.round(W * o.left), y = Math.round(H * o.top);
  return { x, y, w: Math.round(W * (1 - o.left - o.right)), h: Math.round(H * (1 - o.top - o.bottom)) };
}

// Explicitly synthetic audio features (labelled "SYN" on screen) when no live
// spectrum is available: derived from beat phase + seed, deterministic.
export function synthAudio(beat, seed) {
  const phase = beat - Math.floor(beat);
  const kick = Math.exp(-phase * 7);
  const n = 32;
  const spectrum = new Array(n);
  const b16 = Math.floor(beat * 4);
  for (let i = 0; i < n; i++) {
    const base = 0.25 + 0.5 * Math.exp(-i / 9);
    const wobble = 0.25 * hash01(seed, i, b16);
    spectrum[i] = Math.min(1, base * (0.5 + 0.5 * kick * (i < 8 ? 1.4 : 0.6)) + wobble * 0.5);
  }
  return { synthetic: true, rms: 0.3 + 0.4 * kick, bass: kick, mid: 0.4 + 0.2 * hash01(seed, 1, b16),
           treble: 0.3 + 0.3 * hash01(seed, 2, b16), spectrum, onset: phase < 0.08 };
}
