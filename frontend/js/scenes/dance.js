// DANCE.EXE: ASCII figures switching poses on beats/eighths, deterministic by seed.
import { GREEN, GREEN_HOT, GREEN_DIM, font } from "../engine/glyphs.js";
import { hash01 } from "../engine/prng.js";

const POSES = [
  [" o ", "/|\\", "/ \\"],
  ["\\o/", " | ", "/ \\"],
  [" o/", "/| ", "/ >"],
  ["\\o ", " |\\", "< \\"],
  [" o ", "<|>", " |\\"],
  ["_o_", " | ", "/ \\"],
  [" o ", "-|-", "| |"],
  ["\\o_", " / ", "| \\"],
];

export default {
  draw(ctx, f, env) {
    const s = f.safe;
    const n = 4 + Math.floor(hash01(f.seed, 7) * 2);  // 4..5 dancers
    const cell = s.w / n;
    const size = 30;
    ctx.font = font(size);
    ctx.textBaseline = "top";
    ctx.textAlign = "center";
    const half = f.eighth;                  // pose changes each eighth for even dancers, beat for odd
    for (let i = 0; i < n; i++) {
      const step = i % 2 === 0 ? half : Math.floor(f.beat);
      const pose = POSES[Math.floor(hash01(f.seed, i, step) * POSES.length)];
      const bounce = Math.sin(Math.min(1, f.phase * 2) * Math.PI) * 18 * f.intensity;
      const x = s.x + cell * (i + 0.5);
      const y = s.y + s.h * 0.38 - bounce + (i % 2) * 8;
      ctx.fillStyle = i === f.beatInBar % n ? GREEN_HOT : GREEN;
      for (let r = 0; r < 3; r++) ctx.fillText(pose[r], x, y + r * size * 1.05);
    }
    // floor with beat-synchronous light tiles
    const tiles = 8;
    const tw = s.w / tiles;
    for (let i = 0; i < tiles; i++) {
      const on = hash01(f.seed, 50 + i, Math.floor(f.beat)) > 0.55;
      ctx.fillStyle = on ? GREEN : GREEN_DIM;
      ctx.globalAlpha = on ? 0.35 + 0.5 * env.guard.pulse(f.kick) * f.intensity : 0.25;
      ctx.fillRect(s.x + i * tw + 2, s.y + s.h * 0.78, tw - 4, 22);
    }
    ctx.globalAlpha = 1;
    ctx.font = font(26);
    ctx.fillStyle = GREEN;
    ctx.fillText("DANCE.EXE", s.x + s.w / 2, s.y);
    ctx.font = font(16);
    ctx.fillStyle = GREEN_DIM;
    ctx.fillText(`BAR ${f.bar}  BEAT ${f.beatInBar + 1}/${f.bpb}`, s.x + s.w / 2, s.y + s.h - 40);
    ctx.textAlign = "left";
  },
};
