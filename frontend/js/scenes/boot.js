// BOOT/TITLE: startup terminal typed out on the beat grid + live clock.
import { GREEN, GREEN_DIM, GREEN_HOT, font } from "../engine/glyphs.js";
import { hash01 } from "../engine/prng.js";

const LINES = [
  "CYBERSPACE 2000 BIOS v2.000  (C) 1999-2000",
  "CPU: QUAD CORTEX-A53 @ 1200MHz ... OK",
  "MEMORY TEST: %MEM% KB OK",
  "PAL COMPOSITE 720x576i @ 50Hz ... OK",
  "MOUNTING /dev/cyberspace ... OK",
  "NODE %NODE%  STATE %STATE%",
  "SYNC: %SRC% TIMELINE %BPM% BPM",
  "LOADING NEURAL INTERFACE ........ DONE",
  "WELCOME TO THE FUTURE.",
];

const pad = (n, w = 2) => String(n).padStart(w, "0");

export default {
  draw(ctx, f, env) {
    const s = f.safe;
    const sb = Math.max(0, f.sceneBeat);
    // one line per half beat, characters typed per sixteenth
    const lineIdx = Math.floor(sb * 2);
    const charsNow = Math.floor((sb * 2 - lineIdx) * 48);
    ctx.font = font(16);
    ctx.textBaseline = "top";
    const subs = {
      "%MEM%": String(Math.min(1048576, Math.floor(sb * 65536))),
      "%NODE%": f.node, "%STATE%": f.state, "%SRC%": f.source, "%BPM%": f.tl.bpm.toFixed(1),
    };
    const shown = Math.min(LINES.length, lineIdx + 1);
    for (let i = 0; i < shown; i++) {
      let text = LINES[i].replace(/%\w+%/g, (m) => subs[m] ?? m);
      if (i === lineIdx) text = text.slice(0, charsNow);
      ctx.fillStyle = i === shown - 1 ? GREEN_HOT : GREEN;
      ctx.fillText(text, s.x, s.y + i * 22);
    }
    // cursor blinks on the beat
    if (f.phase < 0.5) {
      ctx.fillStyle = GREEN;
      const y = s.y + Math.min(shown - 1, lineIdx) * 22;
      ctx.fillRect(s.x + (lineIdx < LINES.length ? charsNow : 0) * 10 + 2, y + 2, 9, 16);
    }
    // big clock (wall time distributed by the leader)
    const d = f.wall;
    const hh = pad(d.getHours()), mm = pad(d.getMinutes()), ss = pad(d.getSeconds());
    const colon = f.phase < 0.5 ? ":" : " ";
    ctx.font = font(76);
    ctx.textAlign = "center";
    ctx.fillStyle = GREEN;
    const cy = s.y + s.h - 120;
    ctx.fillText(`${hh}${colon}${mm}${colon}${ss}`, s.x + s.w / 2, cy);
    ctx.font = font(20);
    ctx.fillStyle = GREEN_DIM;
    const days = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];
    ctx.fillText(`${days[d.getDay()]} ${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
                 s.x + s.w / 2, cy + 82);
    ctx.textAlign = "left";
    // seeded noise flicker line
    ctx.globalAlpha = 0.35 * env.guard.pulse(f.kick);
    ctx.fillStyle = GREEN;
    ctx.fillRect(0, Math.floor(hash01(f.seed, f.sixteenth) * 480), 640, 2);
    ctx.globalAlpha = 1;
  },
};
