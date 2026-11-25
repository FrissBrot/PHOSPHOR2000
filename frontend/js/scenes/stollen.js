// 09 STOLLÄ-TUNNEL — tunnel arches rushing towards the viewer, one per bar.
import { begin, end, text, alt, bpmLabel, DW, BG } from "./kit.js";

const HEADS = ["IM STOLLÄ SIT 1990", "STOLLÄGUSLER", "GUGGÄMUSIG SACHSLE", "NO LANG NID FERTIG"];

// cubic-bezier(.55,0,1,.45) (CSS easeInCubic-like), solved numerically
function ease(x) {
  let lo = 0, hi = 1, u = x;
  for (let i = 0; i < 20; i++) {
    u = (lo + hi) / 2;
    const bx = 3 * (1 - u) * (1 - u) * u * 0.55 + 3 * (1 - u) * u * u * 1 + u * u * u;
    if (bx < x) lo = u; else hi = u;
  }
  return 3 * (1 - u) * u * u * 0.45 + u * u * u;
}

function arch(ctx, s) {
  // 760x560 box, top corners elliptical 380/320, open bottom; centred at (512, 364)
  const w = 760 * s, h = 560 * s, rx = 380 * s, ry = 320 * s;
  const x0 = 512 - w / 2, y0 = 364 - h / 2;
  ctx.beginPath();
  ctx.moveTo(x0, y0 + h);
  ctx.lineTo(x0, y0 + ry);
  ctx.ellipse(512, y0 + ry, rx, ry, 0, Math.PI, 2 * Math.PI);
  ctx.lineTo(x0 + w, y0 + h);
  ctx.stroke();
}

export default {
  draw(ctx, f, env) {
    const k = begin(ctx, f, env);
    const t = k.t;
    const group = alt(k, 1, 0.7);
    ctx.strokeStyle = k.phos;
    ctx.shadowColor = k.phos;
    ctx.shadowBlur = k.glowPx;
    for (let i = 0; i < 10; i++) {
      const x = ((f.sceneBeat / 4) + i / 10) % 1;          // 4 beats per ring cycle
      const s = 0.02 + ease(x) * (1.9 - 0.02);
      const op = x < 0.12 ? (x / 0.12) * 0.9 : 0.9;
      ctx.globalAlpha = op * group;
      ctx.lineWidth = 8 * s;
      arch(ctx, s);
    }
    ctx.globalAlpha = 1;
    ctx.shadowBlur = 0;
    ctx.globalAlpha = 0.8;
    ctx.fillStyle = k.phos;
    ctx.fillRect(0, 536, DW, 7);
    ctx.globalAlpha = 1;
    ctx.fillStyle = BG;
    ctx.fillRect(56, 36, DW - 112, 56);
    text(k, `STOLLÄ.NAV // TUNNEL-KM ${(t * 0.012).toFixed(3)}`, 56, 40, 40, { spacing: 1 });
    text(k, `BPM ${bpmLabel(f)}`, DW - 56, 40, 40, { spacing: 1, align: "right" });
    ctx.fillStyle = BG;
    ctx.fillRect(56, 768 - 92 - 160, DW - 112, 160);
    ctx.fillStyle = k.phos;
    ctx.fillRect(56, 768 - 92 - 160, DW - 112, 6);
    ctx.fillRect(56, 768 - 92 - 6, DW - 112, 6);
    text(k, HEADS[Math.floor(t / 16) % HEADS.length], DW / 2 + 4, 768 - 92 - 136, 72, { align: "center", spacing: 8 });
    text(k, "NAMENSGEBER: SACHSLER UMFAHRUNGSTUNNEL", DW / 2, 768 - 92 - 56, 38, { align: "center" });
    end(k);
  },
};
