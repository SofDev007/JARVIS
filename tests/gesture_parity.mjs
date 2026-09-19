// Parity check for digital_twin/airboard/static/gestures.js against the
// frozen outputs of the original Python gesture engine
// (tests/data/gesture_golden.json). Run by tests/test_airboard_gestures.py.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { features, classify, RULES, Stabilizer } from "../digital_twin/airboard/static/gestures.js";

const golden = JSON.parse(readFileSync(new URL("./data/gesture_golden.json", import.meta.url)));

for (const p of golden.poses) {
  const F = features(p.landmarks);
  for (const [name, score] of RULES) {
    assert.ok(Math.abs(score(F) - p.scores[name]) < 1e-6, `${p.pose}: ${name} score`);
  }
  const best = classify(p.landmarks);
  assert.equal(best ? best.gesture : null, p.best, `${p.pose}: winner`);
}

const st = new Stabilizer();
for (const s of golden.stabilizer) {
  const out = st.update(s.in ? { gesture: s.in, confidence: s.conf } : null);
  assert.equal(out ? out.gesture : null, s.out, "stabilizer winner");
  assert.ok(Math.abs((out ? out.confidence : 0) - s.out_conf) < 1e-6, "stabilizer confidence");
}
console.log(`ok ${golden.poses.length} poses, ${golden.stabilizer.length} stabilizer steps`);
