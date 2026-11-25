// DATA RAIN: seeded falling characters. Each column's speed/offset comes from
// the scene seed; positions are pure functions of the beat, so all CRTs and a
// restarted browser show the identical frame for the same musical time.
import { hash01 } from "../engine/prng.js";
import { GREEN_HOT, font, GREEN } from "../engine/glyphs.js";

export default {
  draw(ctx, f, env) {
    const at = env.atlas.rain;
    const cols = Math.floor(640 / at.cw);
    const rows = Math.ceil(480 / at.ch) + 1;
    const n = at.chars.length;
    const sb = f.sceneBeat;
    for (let c = 0; c < cols; c++) {
      const speed = 2 + hash01(f.seed, c, 1) * 6;           // rows per beat
      const len = 6 + Math.floor(hash01(f.seed, c, 2) * 14);
      const off = hash01(f.seed, c, 3) * (rows + len);
      const span = rows + len;
      const head = (sb * speed + off) % span;
      const x = c * at.cw;
      for (let k = 0; k < len; k++) {
        const r = Math.floor(head) - k;
        if (r < 0 || r >= rows) continue;
        // characters mutate on sixteenths
        const ci = Math.floor(hash01(f.seed, c, r, Math.floor(f.sixteenth / (k === 0 ? 1 : 3))) * n);
        const row = k === 0 ? 2 : k < len * 0.5 ? 0 : 1;
        at.draw(ctx, at.chars[ci], x, r * at.ch, row);
      }
    }
    // beat pulse band (no full-screen flash)
    ctx.globalAlpha = 0.25 * env.guard.pulse(f.kick) * f.intensity;
    ctx.fillStyle = GREEN;
    ctx.fillRect(0, 0, 640, 480 * 0.04);
    ctx.fillRect(0, 480 * 0.96, 640, 480 * 0.04);
    ctx.globalAlpha = 1;
    const s = f.safe;
    ctx.fillStyle = "#000";
    ctx.fillRect(s.x - 4, s.y + s.h / 2 - 26, 300, 40);
    ctx.font = font(26);
    ctx.textBaseline = "top";
    ctx.fillStyle = GREEN_HOT;
    ctx.fillText("DATA RAIN", s.x, s.y + s.h / 2 - 20);
  },
};
