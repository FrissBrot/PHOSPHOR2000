// node --test tests/js : JS twins must match Python bit-for-bit / to 1e-9.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fnv1a, mix32, Mulberry32, seedFor } from "../../frontend/js/engine/prng.js";
import { Timeline } from "../../frontend/js/engine/timeline.js";

const g = JSON.parse(readFileSync(new URL("../fixtures/golden.json", import.meta.url)));

test("fnv1a", () => { for (const [s, v] of Object.entries(g.fnv1a)) assert.equal(fnv1a(s), v, s); });
test("mix32", () => { for (const [args, v] of g.mix32) assert.equal(mix32(...args), v); });
test("seedFor", () => { for (const [gs, sc, b, v] of g.seed_for) assert.equal(seedFor(gs, sc, b), v); });
test("mulberry32", () => {
  for (const [seed, vals] of Object.entries(g.mulberry)) {
    const r = new Mulberry32(Number(seed));
    assert.deepEqual(vals.map(() => r.nextU32()), vals);
  }
});
test("timeline evaluation incl. slews and deferred steps", () => {
  for (const c of g.timelines) {
    const tl = new Timeline(c.tl);
    c.t.forEach((t, i) => assert.ok(Math.abs(tl.beatAt(t) - c.beat[i]) < 1e-9, `t=${t}`));
    for (const [b, t] of c.inv) assert.ok(Math.abs(tl.timeOfBeat(b) - t) < 1e-9);
  }
});
