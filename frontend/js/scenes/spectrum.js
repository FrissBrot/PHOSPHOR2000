// SPECTRUM ANALYZER: live frequency bars from the audio node, or an
// explicitly labelled SYNTHETIC fallback when no live spectrum is fresh.
import { GREEN, GREEN_DIM, GREEN_HOT, font } from "../engine/glyphs.js";

export default {
  draw(ctx, f, env) {
    const s = f.safe;
    const spec = f.audio.spectrum || [];
    const n = spec.length || 32;
    const st = env.sceneState;
    st.peaks = st.peaks || new Array(n).fill(0);
    const top = s.y + 40, bottom = s.y + s.h - 50;
    const hgt = bottom - top;
    const bw = s.w / n;
    const seg = 10;  // LED segment height
    for (let i = 0; i < n; i++) {
      const v = Math.max(0, Math.min(1, spec[i] ?? 0));
      st.peaks[i] = Math.max(v, (st.peaks[i] || 0) - 0.02);
      const lit = Math.floor(v * hgt / seg);
      const x = s.x + i * bw + 1;
      for (let k = 0; k < lit; k++) {
        const y = bottom - (k + 1) * seg;
        ctx.fillStyle = k > (hgt / seg) * 0.8 ? GREEN_HOT : GREEN;
        ctx.fillRect(x, y + 2, bw - 3, seg - 3);
      }
      ctx.fillStyle = GREEN_HOT;
      ctx.fillRect(x, bottom - st.peaks[i] * hgt - 3, bw - 3, 3);
    }
    ctx.fillStyle = GREEN_DIM;
    ctx.fillRect(s.x, bottom + 2, s.w, 2);
    ctx.font = font(22);
    ctx.textBaseline = "top";
    ctx.fillStyle = GREEN;
    ctx.fillText("SPECTRUM ANALYZER", s.x, s.y);
    ctx.textAlign = "right";
    ctx.fillStyle = f.audioLive ? GREEN : GREEN_HOT;
    ctx.fillText(f.audioLive ? "LIVE INPUT" : "SYNTHETIC", s.x + s.w, s.y);
    ctx.textAlign = "left";
    ctx.font = font(14);
    ctx.fillStyle = GREEN_DIM;
    const lvl = (x) => "#".repeat(Math.round((x || 0) * 12)).padEnd(12, ".");
    ctx.fillText(`BASS ${lvl(f.audio.bass)} MID ${lvl(f.audio.mid)} TRE ${lvl(f.audio.treble)}`, s.x, bottom + 12);
  },
};
