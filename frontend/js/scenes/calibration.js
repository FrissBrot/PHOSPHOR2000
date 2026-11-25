// CALIBRATION: large green flash on every calibration beat for the selected
// CRT (or all CRTs in comparison mode). Flash times are origin + k*60/bpm on
// the cluster timeline, shifted by this CRT's visual offset like every other
// beat-reactive effect (the renderer's t_eval already includes it).
import { GREEN, GREEN_DIM, GREEN_HOT, font } from "../engine/glyphs.js";

export default {
  draw(ctx, f, env) {
    const cal = f.cal;
    const s = f.safe;
    const period = 60 / cal.bpm;
    const x = (f.t - cal.origin) / period;
    const k = Math.floor(x);
    const ph = x - k;
    const show = cal.mode !== "single" || f.isCalTarget;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    if (show && x >= 0) {
      const on = ph < 0.12;  // ~60 ms at 120 BPM: 1-2 PAL frames
      if (on) {
        if (env.guard.reduced) {
          // reduced flashing: centred box, moderate luminance, no full-field flash
          ctx.globalAlpha = 0.6;
          ctx.fillStyle = GREEN;
          ctx.fillRect(s.x + s.w * 0.3, s.y + s.h * 0.3, s.w * 0.4, s.h * 0.4);
          ctx.globalAlpha = 1;
        } else {
          const a = env.guard.flash(`cal${k}`, f.t, 1.0);
          ctx.globalAlpha = a;
          ctx.fillStyle = GREEN;
          ctx.fillRect(s.x, s.y, s.w, s.h);   // inside overscan-safe area
          ctx.globalAlpha = 1;
        }
      }
      ctx.font = font(96);
      ctx.fillStyle = on ? "#000" : GREEN;
      ctx.fillText(String(((k % 4) + 4) % 4 + 1), s.x + s.w / 2, s.y + s.h / 2 - 60);
    }
    ctx.font = font(22);
    ctx.fillStyle = GREEN_HOT;
    const title = cal.mode === "single" ? `CALIBRATING ${cal.target}` : cal.mode === "click" ? "FLASH + CLICK COMPARE" : "ALL-CRT FLASH COMPARE";
    ctx.fillText(title, s.x + s.w / 2, s.y + 6);
    ctx.font = font(16);
    ctx.fillStyle = GREEN_DIM;
    ctx.fillText(`${f.node}  ${cal.bpm.toFixed(0)} BPM  OFFSET ${f.offsetMs.toFixed(1)} ms`, s.x + s.w / 2, s.y + 34);
    if (f.kbdHud || (cal.mode === "single" && f.isCalTarget)) {
      const r = cal.result || {};
      const lines = [
        `TAPS ${cal.taps ?? cal.tap_count ?? 0}  USED ${r.n_used ?? 0}`,
        r.mean_ms == null ? "PHASE  --" : `PHASE ${r.mean_ms >= 0 ? "+" : ""}${r.mean_ms.toFixed(1)} ms  SD ${r.circ_std_ms.toFixed(1)}  SE ${r.std_err_ms.toFixed(1)}`,
        r.suggested_offset_ms == null ? "" : `SUGGEST ${r.suggested_offset_ms.toFixed(1)} ms  CONF ${(r.confidence * 100).toFixed(0)}%${r.ready ? "" : " (MORE TAPS)"}`,
        f.kbdHud ? "ENTER TAP  1-5 CRT  ARROWS/PGUP ±1/5/20  F APPLY  S SAVE  ESC CANCEL" : "",
      ];
      ctx.font = font(15);
      ctx.fillStyle = GREEN;
      lines.forEach((ln, i) => ln && ctx.fillText(ln, s.x + s.w / 2, s.y + s.h - 92 + i * 22));
    }
    ctx.textAlign = "left";
  },
};
