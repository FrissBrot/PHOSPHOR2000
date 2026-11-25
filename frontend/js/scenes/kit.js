// Shared drawing kit for the PHOSPHOR2000 CRT scenes.
//
// Scenes are authored in the 1024x768 design space of the original artboards
// and mapped into the overscan-safe rectangle of the 640x480 canvas. All
// motion is a function of the beat position (f.sceneBeat / f.beat) or the
// cluster wall clock, never of frame count, so every CRT shows the same frame
// for the same musical time.
export const DW = 1024, DH = 768;
export const BG = "#020b04";
export const FONT = '"VT323", "DejaVu Sans Mono", monospace';

let overlayCache = null;

export function begin(ctx, f, env) {
  const st = f.st || {};
  const disp = st.display || {};
  const s = f.safe;
  const phos = (st.scenes && st.scenes.phosphor) || "#39ff14";
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = BG;
  ctx.fillRect(0, 0, 640, 480);
  const sx = s.w / DW, sy = s.h / DH;
  ctx.setTransform(sx, 0, 0, sy, s.x, s.y);
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, DW, DH);
  ctx.clip();
  const t = Math.max(0, Math.floor(f.sceneBeat * 2));   // eighth-note tick, like the design's tick
  return {
    ctx, f, env, phos, sx, sy, t,
    onBeat: t % 2 === 0,
    glowPx: [0, 5, 10][disp.glow ?? 1] ?? 5,
    overlay: disp.crt_overlay ?? true,
    reduced: env.guard.reduced,
  };
}

export function end(k) {
  const { ctx } = k;
  ctx.restore();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.shadowBlur = 0;
  ctx.globalAlpha = 1;
  if (k.overlay) ctx.drawImage(overlay(), 0, 0);
  // phosphor flicker (design: 2.7 s cycle, dips to .86), time-derived
  const c = (k.f.t % 2.7) / 2.7;
  const dip = c > 0.46 && c < 0.54 ? (c < 0.5 ? 0.14 : 0.06) : 0;
  if (dip && !k.reduced) {
    ctx.globalAlpha = dip;
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, 640, 480);
    ctx.globalAlpha = 1;
  }
}

// Scanlines + vignette, rendered once into an offscreen canvas.
function overlay() {
  if (overlayCache) return overlayCache;
  const c = document.createElement("canvas");
  c.width = 640; c.height = 480;
  const g = c.getContext("2d");
  g.fillStyle = "rgba(0,0,0,0.2)";
  for (let y = 0; y < 480; y += 3) g.fillRect(0, y, 640, 1);
  const r = g.createRadialGradient(320, 240, 0, 320, 240, 400);
  r.addColorStop(0, "rgba(0,0,0,0)");
  r.addColorStop(0.55 * 0.75, "rgba(0,0,0,0)");
  r.addColorStop(1, "rgba(0,0,0,0.8)");
  g.fillStyle = r;
  g.fillRect(0, 0, 640, 480);
  overlayCache = c;
  return c;
}

// alternate between two values on/off the beat (softened with reduced flashing)
export function alt(k, on, off) {
  if (k.reduced) return on - (on - off) * 0.3;
  return k.onBeat ? on : off;
}

export function glow(k, on = true) {
  const { ctx } = k;
  if (on && k.glowPx) { ctx.shadowColor = k.phos; ctx.shadowBlur = k.glowPx; }
  else ctx.shadowBlur = 0;
}

export function text(k, str, x, y, size, o = {}) {
  const { ctx } = k;
  ctx.font = `${size}px ${FONT}`;
  ctx.textAlign = o.align || "left";
  ctx.textBaseline = o.baseline || "top";
  ctx.letterSpacing = `${o.spacing || 0}px`;
  ctx.fillStyle = o.color || k.phos;
  ctx.globalAlpha = o.alpha ?? 1;
  glow(k, o.glow !== false);
  if (o.scale && o.scale !== 1) {
    ctx.save();
    ctx.translate(x, y + size / 2);
    ctx.scale(o.scale, o.scale);
    ctx.fillText(str, 0, -size / 2);
    ctx.restore();
  } else {
    ctx.fillText(str, x, y);
  }
  ctx.globalAlpha = 1;
  ctx.letterSpacing = "0px";
}

// Multi-line monospace block (like <pre>): lines left-aligned inside a block
// whose horizontal centre is cx.
export function pre(k, lines, cx, y, size, o = {}) {
  const { ctx } = k;
  ctx.font = `${size}px ${FONT}`;
  const cw = ctx.measureText("M").width;
  const width = Math.max(...lines.map((l) => l.length)) * cw;
  const lh = size * (o.lineHeight ?? 1);
  const x0 = o.left ?? (cx - width / 2);
  lines.forEach((ln, i) => text(k, ln, x0, y + i * lh, size, { ...o, align: "left", scale: 1 }));
  return lines.length * lh;
}

export function header(k, left, right, o = {}) {
  const y = o.y ?? 40, padX = o.padX ?? 56;
  text(k, left, padX, y, 40, { spacing: 1 });
  text(k, right, DW - padX, y, 40, { spacing: 1, align: "right" });
  if (o.divider !== false) hline(k, padX, DW - padX, y + 48 + (o.gap ?? 20), 5, 0.45);
}

export function hline(k, x0, x1, y, w, alpha = 1, glowOn = false) {
  const { ctx } = k;
  ctx.globalAlpha = alpha;
  ctx.fillStyle = k.phos;
  glow(k, glowOn);
  ctx.fillRect(x0, y, x1 - x0, w);
  ctx.globalAlpha = 1;
  ctx.shadowBlur = 0;
}

export function box(k, x, y, w, h, lw, o = {}) {
  const { ctx } = k;
  if (o.fill) { ctx.fillStyle = o.fill; ctx.shadowBlur = 0; ctx.fillRect(x, y, w, h); }
  ctx.globalAlpha = o.alpha ?? 1;
  ctx.strokeStyle = o.color || k.phos;
  ctx.lineWidth = lw;
  glow(k, o.glow !== false);
  ctx.strokeRect(x + lw / 2, y + lw / 2, w - lw, h - lw);
  ctx.globalAlpha = 1;
  ctx.shadowBlur = 0;
}

export function blinkOn(f, period) {
  return (f.t % period) < period / 2;
}

// deterministic pseudo-noise from the designs: fract(sin(a*12.9898 + b*78.233) * 43758.5453)
export function noise(a, b) {
  const x = Math.sin(a * 12.9898 + b * 78.233) * 43758.5453;
  return x - Math.floor(x);
}

export function nodeNumber(f) {
  const m = /(\d+)$/.exec(f.node || "");
  return m ? m[1].padStart(2, "0") : "01";
}

export const bpmLabel = (f) => String(Math.round(f.tl.bpm));
