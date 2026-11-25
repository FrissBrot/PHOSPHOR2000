// NEON GRID + CUBE: perspective floor scrolling with the beat, wireframe cube.
import { GREEN, GREEN_DIM, GREEN_HOT, font } from "../engine/glyphs.js";

const CUBE = [[-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1], [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]];
const EDGES = [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4], [0, 4], [1, 5], [2, 6], [3, 7]];

export default {
  draw(ctx, f, env) {
    const W = 640, H = 480;
    const horizon = H * 0.52;
    const cx = W / 2;
    ctx.lineWidth = 2;
    // horizon glow lines
    ctx.strokeStyle = GREEN;
    ctx.beginPath(); ctx.moveTo(0, horizon); ctx.lineTo(W, horizon); ctx.stroke();
    // horizontal lines: z positions scroll one line per beat
    ctx.strokeStyle = GREEN_DIM;
    const scroll = f.beat - Math.floor(f.beat);
    ctx.beginPath();
    for (let i = 0; i < 14; i++) {
      const z = 1 + i - scroll;            // distance
      if (z <= 0.2) continue;
      const y = horizon + (H - horizon) * (1.6 / z);
      if (y > H) continue;
      ctx.moveTo(0, y); ctx.lineTo(W, y);
    }
    // radial lines
    for (let i = -10; i <= 10; i++) {
      ctx.moveTo(cx + i * 12, horizon);
      ctx.lineTo(cx + i * 150, H);
    }
    ctx.stroke();
    // sun stripes (beat pulsing)
    const r = 70 + 10 * env.guard.pulse(f.kick) * f.intensity;
    ctx.fillStyle = GREEN_DIM;
    for (let k = 0; k < 6; k++) {
      const y = horizon - r + k * (r / 6) * 1.0;
      const hw = Math.sqrt(Math.max(0, r * r - (y - horizon) ** 2));
      ctx.fillRect(cx - hw, y, hw * 2, Math.max(2, (r / 6) - k * 1.5 - 2));
    }
    // cube: rotation derived from beat (deterministic), size kicks on the beat
    const a = f.beat * Math.PI / 8, b = f.beat * Math.PI / 13;
    const scale = 46 * (1 + 0.18 * env.guard.pulse(f.kick) * f.intensity);
    const ca = Math.cos(a), sa = Math.sin(a), cb = Math.cos(b), sb = Math.sin(b);
    const pts = CUBE.map(([x, y, z]) => {
      let x1 = x * ca - z * sa, z1 = x * sa + z * ca;
      let y1 = y * cb - z1 * sb, z2 = y * sb + z1 * cb;
      const p = 3.2 / (z2 + 4.2);
      return [cx + 150 + x1 * scale * p * 1.6, horizon - 130 + y1 * scale * p * 1.6];
    });
    ctx.strokeStyle = GREEN_HOT;
    ctx.lineWidth = 3;
    ctx.beginPath();
    for (const [i, j] of EDGES) { ctx.moveTo(pts[i][0], pts[i][1]); ctx.lineTo(pts[j][0], pts[j][1]); }
    ctx.stroke();
    ctx.lineWidth = 1;
    const s = f.safe;
    ctx.font = font(22);
    ctx.textBaseline = "top";
    ctx.fillStyle = GREEN;
    ctx.fillText("NEON GRID", s.x, s.y);
  },
};
