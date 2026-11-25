// 10 MOTTO.DB — scrolling database of the Stollägusler mottos 1990–2026.
import { begin, end, header, text, box, alt, bpmLabel, DW, BG } from "./kit.js";

const M = [
  [1990, "Gründung"], [1991, "ohne Motto"], [1992, "Indonesien"], [1993, "Panzerkleid"],
  [1994, "Giggel-Grind-Gwand"], [1995, "Sonne-Mond-Gwand"], [1996, "Das Phantom der Oper"],
  [1997, "Rezession"], [1998, "Troll"], [1999, "Grafitti"], [2000, "Las Vegas"],
  [2001, "L'Oréal"], [2002, "Miär packid's"], [2003, "Hang Loose"], [2004, "Bärädräck"],
  [2005, "15. Jagdsaison"], [2006, "Obacht"], [2007, "Vom Kenig zum Bättler"],
  [2008, "Ovi - Miär chends nid besser, aber lenger"], [2009, "Immer im Mittelpunkt"],
  [2010, "20 Jahr Nahbrand leschä"], [2011, "Mid Stiil"], [2012, "Dr Teff alah"],
  [2013, "Sind miär z'starch, bisch dui z'schwach"], [2014, "Immer voll gladä"],
  [2015, "25 Jahr - dr Stollä isch no lang nid fertig"], [2016, "Eis zwitschärä"],
  [2017, "Strobo Party"], [2018, "Uf Glattiis"], [2019, "Läck mich!"],
  [2020, "Sit 30 Jahr i Form"], [2021, "---"], [2022, "Sit 32 Jahr i Form"],
  [2023, "Uf Wildfang oder so!"], [2024, "Bizzli Chic"], [2025, "Sit 35 Jahr im Hehäflug"],
  [2026, "SRF d'StolläGusler im Radio Fiäber"],
];

export default {
  draw(ctx, f, env) {
    const k = begin(ctx, f, env);
    const t = k.t;
    header(k, "C:\\STOLLÄGUSLER> SELECT * FROM MOTTOS", `BPM ${bpmLabel(f)}`, { gap: 14 });
    const idx = Math.floor(t / 4) % M.length;
    ctx.save();
    ctx.beginPath(); ctx.rect(56, 120, DW - 112, 7 * 46); ctx.clip();
    for (let r = -3; r <= 3; r++) {
      const i = idx + r;
      if (i < 0 || i >= M.length) continue;
      const y = 124 + (r + 3) * 46;
      const sel = r === 0;
      if (sel) { ctx.fillStyle = k.phos; ctx.shadowBlur = 0; ctx.fillRect(56, y - 2, DW - 112, 46); }
      const o = sel ? 1 : 0.75 - Math.abs(r) * 0.12;
      const col = sel ? BG : k.phos;
      text(k, String(M[i][0]), 68, y, 42, { color: col, alpha: o, glow: !sel });
      text(k, M[i][1].toUpperCase(), 180, y, 42, { color: col, alpha: o, glow: !sel });
    }
    ctx.restore();
    box(k, 56, 460, DW - 112, 214, 7);
    text(k, `RECORD ${String(idx + 1).padStart(2, "0")}/37 ■ MOTTO ${M[idx][0]}`, 82, 478, 38, { spacing: 1 });
    // long mottos shrink to fit the box
    const motto = M[idx][1].toUpperCase();
    ctx.font = '64px "VT323", monospace';
    const w = ctx.measureText(motto).width + motto.length * 2;
    const size = w > DW - 170 ? Math.floor(64 * (DW - 170) / w) : 64;
    text(k, motto, 82, 540, size, { spacing: 2, scale: alt(k, 1.02, 1) });
    end(k);
  },
};
