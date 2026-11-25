// 12 BIS ES HELL WIRD — system clock, sunrise progress from 20:00 to 07:30.
import { begin, end, header, text, pre, alt, bpmLabel, DW } from "./kit.js";

const p2 = (n) => String(n).padStart(2, "0");

export default {
  draw(ctx, f, env) {
    const k = begin(ctx, f, env);
    const now = f.wall;
    header(k, "UPTIME.EXE // STOLLÄGUSLER", `BPM ${bpmLabel(f)}`, { gap: 14 });
    const sep = k.onBeat || k.reduced ? ":" : " ";
    text(k, "SYSTEMZEIT", 56, 140, 38, { spacing: 1 });
    text(k, p2(now.getHours()) + sep + p2(now.getMinutes()) + sep + p2(now.getSeconds()), 56, 186, 150, { spacing: 2 });
    const mins = now.getHours() * 60 + now.getMinutes() + now.getSeconds() / 60;
    const start = 20 * 60, dawn = 7 * 60 + 30, total = 24 * 60 - start + dawn;
    const night = mins >= start || mins < dawn;
    let p, headline, remain;
    if (night) {
      const since = mins >= start ? mins - start : mins + 24 * 60 - start;
      p = Math.min(1, since / total);
      const left = Math.max(0, Math.round(total - since));
      headline = "WIR GEHEN ERST HEIM, WENN ES HELL WIRD.";
      remain = "> RESTZEIT BIS SHUTDOWN  " + p2(Math.floor(left / 60)) + ":" + p2(left % 60);
    } else {
      p = 1;
      headline = "ES IST HELL. JETZT GIBTS KAFI.";
      remain = "> STANDBY BIS 20:00";
    }
    // sunrise window (300 x 230, horizon line at the bottom)
    const wx = DW - 56 - 300, wy = 126;
    ctx.save();
    ctx.beginPath(); ctx.rect(wx, wy, 300, 230); ctx.clip();
    const sun = k.onBeat || k.reduced ? ["\\  |  /", "-- ( ) --", "/  |  \\"] : [" .  |  . ", "-  ( )  -", " .  |  . "];
    pre(k, sun, wx + 150, wy + Math.round(240 - p * 200), 44);
    ctx.restore();
    ctx.fillStyle = k.phos; ctx.shadowColor = k.phos; ctx.shadowBlur = k.glowPx;
    ctx.fillRect(wx, wy + 230, 300, 7);
    ctx.shadowBlur = 0;
    text(k, headline, 56, 400, 40, { spacing: 1, alpha: alt(k, 1, 0.8) });
    ["> SESSION START ....... 20:00", "> TAGESANBRUCH CA. .... 07:30", remain]
      .forEach((ln, i) => text(k, ln, 56, 460 + i * 44, 40));
    const cells = 18, fill = Math.round(p * cells);
    text(k, "NACHT [" + "█".repeat(fill) + "░".repeat(cells - fill) + "] TAG", 56, 616, 44);
    end(k);
  },
};
