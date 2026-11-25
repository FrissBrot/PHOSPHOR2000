// Bit-identical twin of backend/sync/prng.py (golden vectors in tests).
export function fnv1a(text) {
  let h = 0x811c9dc5;
  const bytes = new TextEncoder().encode(text);
  for (const b of bytes) {
    h ^= b;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

export function mix32(...values) {
  let h = 0x9e3779b9;
  for (const v of values) {
    h = (h ^ (v >>> 0)) >>> 0;
    h = Math.imul(h, 0x85ebca6b) >>> 0;
    h = (h ^ (h >>> 13)) >>> 0;
    h = Math.imul(h, 0xc2b2ae35) >>> 0;
    h = (h ^ (h >>> 16)) >>> 0;
  }
  return h >>> 0;
}

export function seedFor(globalSeed, scene, startSixteenth) {
  return mix32(globalSeed, fnv1a(scene), startSixteenth);
}

export class Mulberry32 {
  constructor(seed) { this.state = seed >>> 0; }
  nextU32() {
    this.state = (this.state + 0x6d2b79f5) >>> 0;
    let t = this.state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return (t ^ (t >>> 14)) >>> 0;
  }
  random() { return this.nextU32() / 4294967296; }
}

// Stateless hash -> [0,1): for per-element, per-time randomness without stored state.
export function hash01(...values) {
  return mix32(...values) / 4294967296;
}
