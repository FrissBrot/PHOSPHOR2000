// 04 NEON GRID + CUBE — perspective floor scrolling one cell per beat,
// striped sun, wireframe cube spinning once per two bars, bobbing on the beat.
import { begin, end, header, text, alt, bpmLabel, DW } from "./kit.js";

const CUBE = [[-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1], [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]];
const EDGES = [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4], [0, 4], [1, 5], [2, 6], [3, 7]];
const WORDS = ["ENTER THE GRID", "CYBERSPACE 2000", "LOG IN • DANCE", "NO SIGNAL LOST"];
const FLOOR_TOP = 390;

export default {
  draw(ctx, f, env) {
    const k = begin(ctx, f, env);
    const t = k.t;
    header(k, "GRID.SYS // SECTOR 2000", `BPM ${bpmLabel(f)}`, { divider: false });
    // sun with stripes
    ctx.save();
    ctx.globalAlpha = 0.45;
    ctx.beginPath(); ctx.arc(512, 250, 136, 0, Math.PI * 2);
    ctx.lineWidth = 7; ctx.strokeStyle = k.phos; ctx.shadowColor = k.phos; ctx.shadowBlur = k.glowPx * 2;
    ctx.stroke();
    ctx.clip();
    ctx.shadowBlur = 0; ctx.fillStyle = k.phos;
    for (const [y, h] of [[150, 7], [178, 8], [202, 9], [224, 8], [246, 11]]) ctx.fillRect(370, 110 + y, 284, h);
    ctx.restore();
    // floor: classic perspective plane below the horizon, one cell per beat towards the viewer
    ctx.save();
    ctx.beginPath(); ctx.rect(0, FLOOR_TOP, DW, 768 - FLOOR_TOP); ctx.clip();
    ctx.strokeStyle = k.phos; ctx.shadowColor = k.phos; ctx.shadowBlur = k.glowPx * 0.6;
    const K = 768 - FLOOR_TOP, CELL = 260;
    const fade = (y) => Math.max(0, Math.min(1, (y - FLOOR_TOP) / (0.25 * K)));
    const frac = f.beat - Math.floor(f.beat);
    for (let n = 0; n < 40; n++) {
      const z = 1 + n - frac;
      if (z < 0.55) continue;
      const y = FLOOR_TOP + K / z;
      ctx.globalAlpha = fade(y);
      ctx.lineWidth = Math.max(1.5, 6 / z);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(DW, y); ctx.stroke();
    }
    ctx.lineWidth = 3;
    for (let m = -12; m <= 12; m++) {
      // segments so the fade towards the horizon matches the horizontal lines
      for (let j = 0; j < 6; j++) {
        const za = 0.55 * Math.pow(40 / 0.55, j / 6), zb = 0.55 * Math.pow(40 / 0.55, (j + 1) / 6);
        const ya = FLOOR_TOP + K / za, yb = FLOOR_TOP + K / zb;
        ctx.globalAlpha = fade((ya + yb) / 2);
        ctx.beginPath(); ctx.moveTo(512 + m * CELL / za, ya); ctx.lineTo(512 + m * CELL / zb, yb); ctx.stroke();
      }
    }
    ctx.restore();
    ctx.globalAlpha = 1;
    // horizon
    ctx.fillStyle = k.phos; ctx.shadowColor = k.phos; ctx.shadowBlur = k.glowPx * 3;
    ctx.fillRect(0, 386, DW, 7);
    ctx.shadowBlur = 0;
    // cube: rotateX(-22deg) rotateY(spin) with perspective 700, 150 px edge
    const ay = (f.beat / 8) * Math.PI * 2, ax = -22 * Math.PI / 180;
    const bob = k.reduced ? -4 : (k.onBeat ? -14 : 0);
    const cy = 225 + bob;
    const pts = CUBE.map(([x, y, z]) => {
      x *= 75; y *= 75; z *= 75;
      const x1 = x * Math.cos(ay) + z * Math.sin(ay), z1 = -x * Math.sin(ay) + z * Math.cos(ay);
      const y2 = y * Math.cos(ax) - z1 * Math.sin(ax), z2 = y * Math.sin(ax) + z1 * Math.cos(ax);
      const s = 700 / (700 - z2);
      return [512 + x1 * s, cy + y2 * s];
    });
    ctx.strokeStyle = k.phos; ctx.lineWidth = 7; ctx.lineJoin = "round";
    ctx.shadowColor = k.phos; ctx.shadowBlur = k.glowPx * 1.5;
    ctx.beginPath();
    for (const [a, b] of EDGES) { ctx.moveTo(pts[a][0], pts[a][1]); ctx.lineTo(pts[b][0], pts[b][1]); }
    ctx.stroke();
    ctx.shadowBlur = 0;
    ctx.globalAlpha = 0.75; ctx.fillStyle = "#020b04"; ctx.fillRect(0, 432, DW, 100); ctx.globalAlpha = 1;
    text(k, WORDS[Math.floor(t / 16) % WORDS.length], DW / 2 + 7, 440, 84, { align: "center", spacing: 14, alpha: alt(k, 1, 0.75) });
    end(k);
  },
};
