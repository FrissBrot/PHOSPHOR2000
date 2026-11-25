// 07 MATTLIBALL 2027 — title, event countdown (configurable target), typed info lines.
import { begin, end, header, text, box, alt, blinkOn, bpmLabel, DW } from "./kit.js";
import { parseTarget } from "./util.js";

const LINES = [
  "> FR 05.02.2027 // FASNACHTSFREITAG",
  "> MATTLISCHULHAUS SACHSELN",
  "> STOLLÄGUSLER + SKICLUB FLÜELI-RANFT",
  "> 2000+ USERS // HOTSPOT DER FASNACHT",
  "> LOGOUT ERST WENN ES HELL WIRD",
];

export default {
  draw(ctx, f, env) {
    const k = begin(ctx, f, env);
    const t = k.t;
    header(k, "MATTLIBALL.EXE // SACHSELN OW", `BPM ${bpmLabel(f)}`, { gap: 18 });
    text(k, "MATTLIBALL", DW / 2 + 4, 126, 150, { align: "center", spacing: 8, scale: alt(k, 1.03, 1) });
    const target = parseTarget(f.countdown.countdown_target);
    const days = Math.ceil((target - f.wall) / 86400000);
    const year = String(target.getFullYear());
    ctx.font = '50px "VT323", monospace';
    const cd = days > 0 ? "T-MINUS " + days + " TAGE" : ">> JETZT LIVE <<";
    const wYear = 190, wCd = ctx.measureText(cd).width + cd.length * 2;
    const x0 = DW / 2 - (wYear + 24 + wCd) / 2;
    box(k, x0, 282, wYear, 80, 6, { alpha: alt(k, 1, 0.5) });
    text(k, year, x0 + wYear / 2 + 7, 290, 64, { align: "center", spacing: 14, alpha: alt(k, 1, 0.5) });
    text(k, cd, x0 + wYear + 24, 297, 50, { spacing: 2 });
    const cycle = LINES.length * 2 + 10;
    const n = Math.min(LINES.length, Math.floor((t % cycle) / 2));
    LINES.slice(0, n).forEach((ln, i) => text(k, ln, 56, 390 + i * 46, 42));
    if (blinkOn(f, 1.0)) text(k, "█", 56, 390 + n * 46, 42);
    end(k);
  },
};
