// 05 DATA RAIN — 20 seeded columns falling at tempo-locked speeds, terminal box.
import { begin, end, text, box, blinkOn, bpmLabel, FONT, DW, BG } from "./kit.js";
import { Mulberry32 } from "../engine/prng.js";

const CHARS = "0123456789ABCDEF<>/\\#$%&*+=:;[]{}";
const MSGS = ["ACCESS GRANTED", "BASS DETECTED", "UPLOADING GROOVE", "SYSTEM OVERLOAD"];
let atlas = null;

function getAtlas(phos) {
  if (atlas && atlas.phos === phos && atlas.fontReady === document.fonts.check(`40px ${FONT}`)) return atlas;
  const c = document.createElement("canvas");
  const cw = 24, ch = 40;
  c.width = cw * CHARS.length; c.height = ch;
  const g = c.getContext("2d");
  g.font = `40px ${FONT}`; g.textBaseline = "top"; g.fillStyle = phos;
  for (let i = 0; i < CHARS.length; i++) g.fillText(CHARS[i], i * cw, 0);
  atlas = { c, cw, ch, phos, fontReady: document.fonts.check(`40px ${FONT}`) };
  return atlas;
}

function columns(seed) {
  const r = new Mulberry32(seed);
  const cols = [];
  for (let i = 0; i < 20; i++) {
    const chars = [];
    for (let j = 0; j < 35; j++) chars.push(Math.floor(r.random() * CHARS.length));
    cols.push({ x: 12 + i * 51, chars, speed: 4 + r.random() * 6, phase: r.random(), o: 0.2 + r.random() * 0.4 });
  }
  return cols;
}

export default {
  draw(ctx, f, env) {
    const k = begin(ctx, f, env);
    const st = env.sceneState;
    if (!st.cols) st.cols = columns(f.seed);
    const a = getAtlas(k.phos);
    for (const c of st.cols) {
      const durBeats = c.speed * 128 / 60;            // design: speed * 128/bpm seconds
      const frac = (((f.sceneBeat / durBeats) + c.phase) % 1 + 1) % 1;
      const top = -1350 + frac * 2150;
      for (let j = 0; j < 35; j++) {
        const y = top + j * 38;
        if (y < -40 || y > 768) continue;
        const m = Math.min(1, j / (35 * 0.75));          // mask: transparent top -> opaque bottom
        ctx.globalAlpha = c.o * m;
        ctx.drawImage(a.c, c.chars[j] * a.cw, 0, a.cw, a.ch, c.x, y, a.cw, a.ch);
      }
    }
    ctx.globalAlpha = 1;
    const bx = DW / 2 - 410, by = 234, bw = 820, bh = 300;
    box(k, bx, by, bw, bh, 7, { fill: BG });
    text(k, "[ TERMINAL ]", bx + 40, by + 32, 38, { spacing: 1 });
    text(k, `BPM ${bpmLabel(f)}`, bx + bw - 40, by + 32, 38, { align: "right", spacing: 1 });
    const t = k.t;
    const msgOn = k.reduced || blinkOn(f, 0.9);
    text(k, MSGS[Math.floor(t / 32) % MSGS.length], DW / 2, by + 90, 84, { align: "center", spacing: 2, alpha: msgOn ? 1 : 0.25 });
    const step = t % 32, filled = Math.min(14, Math.floor((step / 31) * 14));
    text(k, "[" + "#".repeat(filled) + ".".repeat(14 - filled) + "] " + Math.round((filled / 14) * 100) + "%",
         DW / 2, by + 196, 46, { align: "center", spacing: 1 });
    end(k);
  },
};
