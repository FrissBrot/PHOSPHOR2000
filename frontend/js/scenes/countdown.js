// 06 Y2K COUNTDOWN — 23:59:50 -> 00:00:00 loop on the beat, Y2K bug glitches.
import { begin, end, header, text, bpmLabel, DW } from "./kit.js";

export default {
  draw(ctx, f, env) {
    const k = begin(ctx, f, env);
    const t = k.t, onBeat = k.onBeat;
    const p = Math.floor(t / 2) % 26;
    let headline, time, date, sub, bar, jx = 0, skew = 0, headOpacity = 1;
    if (p < 10) {
      headline = "COUNTDOWN TO Y2K"; time = "23:59:" + (50 + p); date = "31.12.1999";
      sub = "T-MINUS " + (10 - p) + " ■ HOLD ON TO YOUR DISKETTES";
      bar = "[" + "█".repeat(p * 2) + "░".repeat(20 - p * 2) + "]";
    } else {
      const q = p - 10;
      const glitch = q === 4 || q === 5 || q === 11 || q === 12;
      time = "00:00:00";
      bar = "[" + (onBeat ? "█░" : "░█").repeat(10) + "]";
      if (glitch) {
        headline = "!! Y2K BUG DETECTED !!";
        date = onBeat ? "01.01.1900" : "00.00.19100";
        sub = "SYSTEM DATE CORRUPTED ■ KEEP DANCING";
        const n = Math.sin(t * 91.7) * 10000;
        jx = k.reduced ? 0 : Math.round((n - Math.floor(n) - 0.5) * 40);
        skew = k.reduced ? 0 : (onBeat ? -6 : 4);
      } else {
        headline = "HAPPY NEW MILLENNIUM"; date = "01.01.2000"; sub = "WELCOME TO CYBERSPACE";
        headOpacity = k.reduced ? 0.8 : (onBeat ? 1 : 0.35);
      }
    }
    header(k, "RTC CLOCK // BIOS v1.99", `BPM ${bpmLabel(f)}`);
    ctx.save();
    ctx.translate(DW / 2 + jx, 360);
    ctx.transform(1, 0, Math.tan(skew * Math.PI / 180), 1, 0, 0);
    text(k, headline, 0, -205, 52, { align: "center", spacing: 8, alpha: headOpacity });
    text(k, time, 0, -140, 220, { align: "center", spacing: 6 });
    text(k, date, 0, 60, 64, { align: "center", spacing: 12 });
    text(k, sub, 0, 150, 42, { align: "center", spacing: 1 });
    ctx.restore();
    text(k, bar, 56, 610, 44);
    end(k);
  },
};
