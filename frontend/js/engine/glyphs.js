// Pre-rendered glyph atlas: drawImage per character is far cheaper than
// fillText on a Raspberry Pi 3 (no per-glyph shaping/rasterisation per frame).
export const FONT = '"DejaVu Sans Mono", "Liberation Mono", "Courier New", monospace';
export const GREEN = "#33ff66";
export const GREEN_DIM = "#118833";
export const GREEN_HOT = "#ccffcc";

export class GlyphAtlas {
  constructor(size, chars, colors = [GREEN, GREEN_DIM, GREEN_HOT]) {
    this.size = size;
    this.chars = chars;
    this.cw = Math.ceil(size * 0.62);
    this.ch = Math.ceil(size * 1.15);
    this.colors = colors;
    const c = document.createElement("canvas");
    c.width = this.cw * chars.length;
    c.height = this.ch * colors.length;
    const g = c.getContext("2d");
    g.font = `bold ${size}px ${FONT}`;
    g.textBaseline = "top";
    colors.forEach((col, row) => {
      g.fillStyle = col;
      for (let i = 0; i < chars.length; i++) g.fillText(chars[i], i * this.cw, row * this.ch + 1);
    });
    this.canvas = c;
    this.index = new Map([...chars].map((ch, i) => [ch, i]));
  }
  draw(ctx, ch, x, y, colorRow = 0) {
    const i = this.index.get(ch);
    if (i === undefined) return;
    ctx.drawImage(this.canvas, i * this.cw, colorRow * this.ch, this.cw, this.ch, x, y, this.cw, this.ch);
  }
  drawText(ctx, text, x, y, colorRow = 0) {
    for (let i = 0; i < text.length; i++) this.draw(ctx, text[i], x + i * this.cw, y, colorRow);
  }
}

export function font(px, bold = true) { return `${bold ? "bold " : ""}${px}px ${FONT}`; }
