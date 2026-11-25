// 11 BAR-NETZWERK — the five bars of the Mattliball as a LAN, packets on the beat.
import { begin, end, header, text, box, bpmLabel, DW, BG } from "./kit.js";

const NAMES = ["FOYER BAR", "OLDIE BAR", "CHELI ZÄLT", "ZELT", "SAAL"];
const STATS = ["PROST.EXE", "LOAD 99%", "ONLINE", "NACHSCHUB OK", "PING 1ms"];
const ANGLES = [-90, -18, 54, 126, 198];
const MSGS = ["> ALLE 5 BARS ONLINE", "> PROST-PROTOKOLL AKTIV", "> MATTLISCHULHAUS", "> STOLLÄGUSLER SACHSELN", "> + SKICLUB FLÜELI-RANFT"];
const CX = 512, CY = 370, RX = 330, RY = 210;

export default {
  draw(ctx, f, env) {
    const k = begin(ctx, f, env);
    const t = k.t;
    header(k, "NETSTAT // MATTLIBALL LAN", `BPM ${bpmLabel(f)}`, { gap: 0 });
    const active = Math.floor(t / 2) % NAMES.length;
    const nodes = ANGLES.map((a) => {
      const r = (a * Math.PI) / 180;
      return [CX + RX * Math.cos(r), CY + RY * Math.sin(r)];
    });
    // links + packet (one hop per beat)
    nodes.forEach(([x, y], i) => {
      const on = i === active;
      ctx.globalAlpha = on ? 1 : 0.35;
      ctx.strokeStyle = k.phos; ctx.lineWidth = 7; ctx.setLineDash([21, 14]); ctx.shadowBlur = 0;
      ctx.beginPath(); ctx.moveTo(CX, CY); ctx.lineTo(x, y); ctx.stroke();
      ctx.setLineDash([]);
      if (on) {
        const p = f.sceneBeat - Math.floor(f.sceneBeat);
        const px = CX + (x - CX) * p, py = CY + (y - CY) * p;
        ctx.fillStyle = k.phos; ctx.shadowColor = k.phos; ctx.shadowBlur = k.glowPx * 1.2;
        ctx.fillRect(px - 15, py - 15, 30, 30);
        ctx.shadowBlur = 0;
      }
    });
    ctx.globalAlpha = 1;
    // hub
    const hs = k.reduced ? 1 : (k.onBeat ? 1.05 : 1);
    ctx.save();
    ctx.translate(CX, CY); ctx.scale(hs, hs);
    box(k, -150, -55, 300, 110, 7, { fill: BG });
    text(k, "MATTLIBALL", 0, -44, 50, { align: "center", spacing: 2 });
    text(k, "2000+ USERS", 0, 6, 32, { align: "center", spacing: 1 });
    ctx.restore();
    nodes.forEach(([x, y], i) => {
      const on = i === active;
      const bx = Math.round(x - 125), by = Math.round(y - 50);
      box(k, bx, by, 250, 100, 5, { fill: on ? k.phos : BG });
      const col = on ? BG : k.phos;
      text(k, NAMES[i], x, by + 12, 44, { align: "center", spacing: 1, color: col, glow: !on });
      text(k, on ? STATS[(i + Math.floor(t / 10)) % STATS.length] : "ONLINE", x, by + 54, 32, { align: "center", color: col, glow: !on });
    });
    text(k, MSGS[Math.floor(t / 12) % MSGS.length], 56, 700, 40);
    end(k);
  },
};
