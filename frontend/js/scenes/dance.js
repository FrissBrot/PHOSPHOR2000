// 02 DANCE.EXE — five ASCII dancers switching moves on eighths, light floor.
import { begin, end, header, text, pre, alt, bpmLabel, DW } from "./kit.js";

const F = {
  A: [" \\o/ ", "  |  ", " / \\ "], B: ["  o  ", " /|\\ ", " / \\ "], C: [" _o  ", "  |\\ ", " / > "],
  D: ["  o_ ", " /|  ", " < \\ "], E: [" \\o  ", "  |\\ ", " / \\ "], G: ["  o/ ", " /|  ", " / \\ "],
  H: ["     ", " \\o/ ", " / \\ "], I: [" _o_ ", "  |  ", " / \\ "], J: [" \\o_ ", "  |  ", " / | "],
  K: [" _o/ ", "  |  ", " | \\ "],
};
const MOVES = [
  ["A", "B", "A", "B", "C", "D", "C", "D"], ["E", "G", "E", "G", "A", "H", "A", "H"],
  ["I", "B", "I", "B", "J", "K", "J", "K"], ["C", "D", "C", "D", "A", "H", "A", "H"],
];
const SHOUTS = ["*** DANCE MODE ON ***", ">> MOVE YOUR BODY <<", "[ 100% UPTIME ]", ">> NO SLEEP TILL Y2K <<"];

export default {
  draw(ctx, f, env) {
    const k = begin(ctx, f, env);
    const t = k.t;
    header(k, "C:\\PARTY> DANCE.EXE", `USERS ONLINE: 5 ■ BPM ${bpmLabel(f)}`);
    text(k, SHOUTS[Math.floor(t / 16) % SHOUTS.length], DW / 2, 128, 62,
         { align: "center", spacing: 3, alpha: k.reduced ? 0.9 : (t % 4 === 0 ? 1 : 0.7) });
    const bar = Math.floor(t / 8);
    const slot = (DW - 112) / 5;
    for (let i = 0; i < 5; i++) {
      const seq = MOVES[(i + bar) % MOVES.length];
      const key = seq[(t + (i % 2) * 2) % seq.length];
      const hop = key === "A" && k.onBeat;
      const size = i === 2 ? 120 : 92;
      const h = 3 * size * 0.95;
      pre(k, F[key], 56 + slot * (i + 0.5), 522 - h + (hop ? -18 : 0), size, { lineHeight: 0.95 });
    }
    // light floor: 16 x 3 tiles
    const gx = 56, gw = DW - 112, cw = (gw - 15 * 6) / 16;
    for (let r = 0; r < 3; r++) {
      for (let c = 0; c < 16; c++) {
        const lit = (c + r + Math.floor(t / 2)) % 4 === 0 || ((c * 7 + r * 3 + t) % 11 === 0);
        ctx.globalAlpha = lit ? (k.reduced ? 0.6 : 0.9) : 0.12;
        ctx.fillStyle = k.phos;
        ctx.shadowBlur = 0;
        ctx.fillRect(gx + c * (cw + 6), 550 + r * 40, cw, 34);
      }
    }
    ctx.globalAlpha = 1;
    end(k);
  },
};
