// 08 GUGGÄ-REGISTER — four registers of the Guggenmusik playing a 4/4 pattern.
import { begin, end, header, text, pre, box, alt, bpmLabel, DW } from "./kit.js";

const pad9 = (rows) => rows.map((x) => (x + "         ").slice(0, 9));

export default {
  draw(ctx, f, env) {
    const k = begin(ctx, f, env);
    const t = k.t, s = t % 8;
    header(k, "GUGGÄ.SYS // SACHSELN OW", `BPM ${bpmLabel(f)}`, { gap: 14 });
    text(k, "STOLLÄGUSLER", DW / 2 + 3, 126, 90, { align: "center", spacing: 6, scale: alt(k, 1.03, 1) });
    const R = [
      { name: "PERCUSSION", hits: [0, 1, 2, 3, 4, 5, 6, 7], sound: s % 2 === 0 ? "BUM" : "TSCH",
        rest: ["  \\o/", "   |", "  [=]", "  / \\"], hit: ["   o", "  /|\\", "  [=]", "  / \\"] },
      { name: "TROMPETEN", hits: [3, 4, 7], sound: "TÖÖT",
        rest: ["   o=<", "  /|", "   |", "  / \\"], hit: ["   o=<))", "  /|", "   |", "  / \\"] },
      { name: "POSAUNEN", hits: [2, 6], sound: "WAAAH",
        rest: ["   o===", "  /|", "   |", "  / \\"], hit: ["   o=====", "  /|", "   |", "  / \\"] },
      { name: "SOUSAPHON", hits: [0, 4], sound: "BOMP",
        rest: ["  (@)o", "   \\|\\", "    |", "   / \\"], hit: ["  (O)o", "   \\|\\", "    |", "   / \\"] },
    ];
    const gx = 56, gw = DW - 112, cw = (gw - 3 * 16) / 4, top = 230, bottom = 640;
    R.forEach((r, i) => {
      const on = r.hits.indexOf(s) >= 0;
      const lvl = on ? 6 : 1 + ((t * 3 + r.name.length) % 3);
      const x = gx + i * (cw + 16), cx = x + cw / 2;
      box(k, x, top, cw, bottom - top, 5, { glow: false });
      if (on) text(k, r.sound, cx, bottom - 350, 54, { align: "center", spacing: 3, scale: k.reduced ? 1 : 1.1 });
      pre(k, pad9(on ? r.hit : r.rest), cx, bottom - 290 + (on && !k.reduced ? -12 : 0), 46);
      text(k, r.name, cx, bottom - 96, 38, { align: "center" });
      text(k, "[" + "▮".repeat(lvl) + "▯".repeat(6 - lvl) + "]", cx, bottom - 52, 34, { align: "center" });
    });
    text(k, "4/4 REGISTER ONLINE ■ SIT 1990", 56, 660, 40, { spacing: 1 });
    end(k);
  },
};
