// 01 BOOT / TITEL — boot sequence typed on the beat, pulsing "2000".
import { begin, end, header, text, alt, blinkOn, nodeNumber, bpmLabel, DW } from "./kit.js";

const LINES = [
  "> BOOT SEQUENCE v2.000",
  "> CHECKING MEMORY ......... 640K OK",
  "> LOADING BASS.DRV ........ OK",
  "> MOUNTING DANCEFLOOR ..... OK",
  "> CALIBRATING STROBE ...... OK",
  "> Y2K COMPLIANCE .......... ??",
  "> DIALING INTO THE NET .... 56K",
  "> ENTERING CYBERSPACE",
];

export default {
  draw(ctx, f, env) {
    const k = begin(ctx, f, env);
    const t = k.t;
    header(k, `CYBERSPACE 2000 // NODE ${nodeNumber(f)}`, `BPM ${bpmLabel(f)} ■ BEAT ${f.beatInBar + 1}/4`,
           { y: 48, gap: 28 });
    text(k, "CYBERSPACE", DW / 2, 160, 150, { align: "center", spacing: 10 });
    text(k, "2000", DW / 2 + 24, 300, 120, { align: "center", spacing: 48, alpha: alt(k, 1, 0.55), scale: alt(k, 1.04, 1) });
    const cycle = LINES.length * 2 + 8;
    const n = Math.min(LINES.length, Math.floor((t % cycle) / 2));
    const shown = LINES.slice(Math.max(0, n - 4), n);
    shown.forEach((ln, i) => text(k, ln, 56, 440 + i * 46, 42));
    if (blinkOn(f, 1.0)) text(k, "█", 56, 440 + shown.length * 46, 42);
    end(k);
  },
};
