// 03 SPECTRUM ANALYZER — live bands from the audio node, or the design's
// synthetic pattern, explicitly labelled SYNTH when no live spectrum is fresh.
import { begin, end, header, text, noise, bpmLabel, DW } from "./kit.js";

const N = 16, MAXH = 400, TOP = 129, BOTTOM = 559;

export default {
  draw(ctx, f, env) {
    const k = begin(ctx, f, env);
    const t = k.t, onBeat = k.onBeat;
    const st = env.sceneState;
    st.peaks = st.peaks || new Array(N).fill(0);
    const dt = st.lastTick === undefined ? 0 : Math.max(0, f.sceneBeat * 2 - st.lastTick);
    st.lastTick = f.sceneBeat * 2;
    header(k, `SPECTRUM ANALYZER // ${f.audioLive ? "CH-1" : "SYNTH"}`, `BPM ${bpmLabel(f)} ■ GAIN +${onBeat ? 12 : 9} dB`,
           { gap: 18 });
    const live = f.audioLive && f.audio.spectrum && f.audio.spectrum.length >= 2 * N;
    const x0 = 60, x1 = DW - 60, gap = 18, bw = (x1 - x0 - gap * (N - 1)) / N;
    let sum = 0;
    for (let i = 0; i < N; i++) {
      let v;
      if (live) {
        v = Math.min(1, (f.audio.spectrum[2 * i] + f.audio.spectrum[2 * i + 1]) / 2);
      } else {
        const pos = i / (N - 1);
        const bassKick = onBeat ? Math.max(0, 1 - pos * 2.2) : 0.15 * (1 - pos);
        const hat = !onBeat ? Math.max(0, pos - 0.6) * 1.6 : 0;
        const body = 0.25 + 0.45 * noise(t, i) * (1 - pos * 0.4);
        v = Math.min(1, body + bassKick * 0.6 + hat * 0.5);
      }
      sum += v;
      const h = 18 + v * (MAXH - 18);
      st.peaks[i] = Math.max(h, st.peaks[i] - 40 * dt);
      const x = x0 + i * (bw + gap);
      ctx.fillStyle = k.phos;
      ctx.shadowColor = k.phos;
      ctx.shadowBlur = k.glowPx;
      for (let y = BOTTOM; y > BOTTOM - h; y -= 26) ctx.fillRect(x, Math.max(BOTTOM - h, y - 18), bw, Math.min(18, y - (BOTTOM - h)));
      ctx.fillRect(x, BOTTOM - st.peaks[i] - 16, bw, 10);
    }
    ctx.shadowBlur = 0;
    ctx.fillRect(56, BOTTOM, DW - 112, 6);
    text(k, "BASS", 60, 583, 36);
    text(k, "MID", DW / 2, 583, 36, { align: "center" });
    text(k, "HIGH", DW - 60, 583, 36, { align: "right" });
    const level = Math.round((sum / N) * 14), r = Math.max(0, level - 1);
    text(k, "L " + "█".repeat(level) + "░".repeat(14 - level) + "  R " + "█".repeat(r) + "░".repeat(14 - r), 56, 640, 42, { spacing: 1 });
    end(k);
  },
};
