// Y2K COUNTDOWN: configurable target date/label, big digits, beat accents.
import { GREEN, GREEN_DIM, GREEN_HOT, font } from "../engine/glyphs.js";

const pad = (n, w = 2) => String(n).padStart(w, "0");

export function parseTarget(s) {
  // ISO local time "YYYY-MM-DDTHH:MM:SS" (no zone => local wall clock of the cluster)
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?$/.exec(s || "");
  if (!m) return new Date(2027, 0, 1);
  return new Date(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0), +(m[6] || 0));
}

export default {
  draw(ctx, f, env) {
    const s = f.safe;
    const target = parseTarget(f.countdown.countdown_target);
    let diff = Math.floor((target - f.wall) / 1000);
    const past = diff <= 0;
    diff = Math.abs(diff);
    const d = Math.floor(diff / 86400), h = Math.floor(diff / 3600) % 24, m = Math.floor(diff / 60) % 60, sec = diff % 60;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.font = font(24);
    ctx.fillStyle = GREEN;
    ctx.fillText(past ? "TIME SINCE" : "COUNTDOWN TO", s.x + s.w / 2, s.y + 10);
    ctx.font = font(40);
    ctx.fillStyle = GREEN_HOT;
    ctx.fillText(f.countdown.countdown_label || "Y2K", s.x + s.w / 2, s.y + 44);
    ctx.font = font(84);
    ctx.fillStyle = GREEN;
    const big = `${pad(h)}:${pad(m)}:${pad(sec)}`;
    const pulse = env.guard.pulse(f.kick) * f.intensity;
    ctx.save();
    ctx.translate(s.x + s.w / 2, s.y + 150);
    ctx.scale(1 + 0.04 * pulse, 1 + 0.04 * pulse);
    ctx.fillText(big, 0, 0);
    ctx.restore();
    ctx.font = font(30);
    ctx.fillStyle = GREEN_DIM;
    ctx.fillText(`${d} DAYS`, s.x + s.w / 2, s.y + 250);
    if (past) {
      // celebratory flash on downbeats, rate/luminance limited by the guard
      const a = env.guard.flash(`cd${f.bar}`, f.t, f.beatInBar === 0 ? 0.5 * Math.exp(-f.phase * 5) : 0);
      if (a > 0.01) { ctx.globalAlpha = a; ctx.fillStyle = GREEN; ctx.fillRect(0, 0, 640, 480); ctx.globalAlpha = 1; }
      ctx.font = font(28);
      ctx.fillStyle = GREEN_HOT;
      ctx.fillText("HAPPY NEW MILLENNIUM", s.x + s.w / 2, s.y + 310);
    }
    // progress ticks: 16 cells advancing per sixteenth within the bar
    const cells = 16, cw = s.w / cells;
    const k = Math.floor((f.beat % f.bpb) * 4);
    for (let i = 0; i < cells; i++) {
      ctx.fillStyle = i <= k ? GREEN : GREEN_DIM;
      ctx.globalAlpha = i <= k ? 0.9 : 0.3;
      ctx.fillRect(s.x + i * cw + 2, s.y + s.h - 40, cw - 4, 14);
    }
    ctx.globalAlpha = 1;
    ctx.textAlign = "left";
  },
};
