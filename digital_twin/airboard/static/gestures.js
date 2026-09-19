// Airboard named-gesture engine: 21 hand landmarks -> a stable semantic
// gesture id (thumbs_up, peace, ...) per hand, published to JARVIS through
// the /state heartbeat. Board verbs (pinch, tap, clap, claw) are separate
// and stay in stage.html; this file only names what the hand is showing.
//
// Pipeline per hand and frame (all soft scores in [0, 1]):
//   landmarks -> finger extension + directions -> every rule scores
//   -> best above MIN_CONFIDENCE -> Stabilizer (majority vote + EMA)
//
// Landmarks are [x, y, z] in *selfie* image space (x right, y down), the
// same layout the rules were tuned on. fromMediaPipe() converts the raw,
// un-mirrored camera landmarks and handedness label from HandLandmarker.
// Adding a gesture = one entry in RULES.

const WRIST = 0, THUMB_TIP = 4, INDEX_MCP = 5, INDEX_TIP = 8, MIDDLE_MCP = 9;
const JOINTS = {
  thumb: [1, 2, 3, 4], index: [5, 6, 7, 8], middle: [9, 10, 11, 12],
  ring: [13, 14, 15, 16], pinky: [17, 18, 19, 20],
};

const MIN_CONFIDENCE = 0.55;

// ---- vector helpers -------------------------------------------------------
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const len = (v) => Math.hypot(v[0], v[1], v[2]);
const dist = (a, b) => len(sub(a, b));
const unit = (v) => { const n = len(v); return n < 1e-9 ? [0, 0, 0] : v.map((c) => c / n); };
function angle(v1, v2) {
  const n1 = len(v1), n2 = len(v2);
  if (n1 < 1e-9 || n2 < 1e-9) return 0;
  return Math.acos(Math.max(-1, Math.min(1, dot(v1, v2) / (n1 * n2)))) * 180 / Math.PI;
}
const jointAngle = (a, b, c) => angle(sub(a, b), sub(c, b));
function smooth(v, lo, hi) {
  if (hi <= lo) return v >= hi ? 1 : 0;
  const t = Math.max(0, Math.min(1, (v - lo) / (hi - lo)));
  return t * t * (3 - 2 * t);
}
const invSmooth = (v, lo, hi) => 1 - smooth(v, lo, hi);

// ---- features -------------------------------------------------------------
function thumbExtension(L, size) {
  const [cmc, mcp, ip, tip] = JOINTS.thumb.map((i) => L[i]);
  const straight = smooth((jointAngle(cmc, mcp, ip) + jointAngle(mcp, ip, tip)) / 2, 120, 155);
  const sep = smooth(dist(tip, L[INDEX_MCP]) / Math.max(size, 1e-6), 0.45, 0.80);
  return sep * (0.5 + 0.5 * straight);   // separation leads, straightness refines
}
function fingerExtension(L, finger) {
  const [mcp, pip, dip, tip] = JOINTS[finger].map((i) => L[i]);
  return smooth((jointAngle(mcp, pip, dip) + jointAngle(pip, dip, tip)) / 2, 120, 165);
}

export function features(L) {
  const size = Math.max(dist(L[WRIST], L[MIDDLE_MCP]), 1e-6);
  const ext = {}, dir = {};
  for (const [f, j] of Object.entries(JOINTS)) {
    ext[f] = f === "thumb" ? thumbExtension(L, size) : fingerExtension(L, f);
    dir[f] = unit(sub(L[j[3]], L[j[1]]));   // distal segment: where it points
  }
  return {
    ext, dir,
    pinch: dist(L[THUMB_TIP], L[INDEX_TIP]) / size,
    spread: angle(dir.index, dir.middle),
  };
}

// ---- scoring --------------------------------------------------------------
const UP = [0, -1, 0], DOWN = [0, 1, 0], LEFT = [-1, 0, 0], RIGHT = [1, 0, 0];
const ext = (F, f) => smooth(F.ext[f], 0.35, 0.75);
const curl = (F, f) => smooth(1 - F.ext[f], 0.35, 0.75);
const notExt = (F, f) => invSmooth(F.ext[f], 0.45, 0.80);
const aim = (v, axis) => smooth(dot(unit(v), axis), 0.50, 0.87);
// A gesture is only as convincing as its weakest constraints: geometric
// mean of the two smallest scores.
function allOf(...s) {
  s.sort((a, b) => a - b);
  return s.length === 1 ? s[0] : Math.sqrt(s[0] * s[1]);
}
const thumbRule = (axis) => (F) => allOf(ext(F, "thumb"), curl(F, "index"),
  curl(F, "middle"), curl(F, "ring"), curl(F, "pinky"), aim(F.dir.thumb, axis));
const pointRule = (axis) => (F) => allOf(ext(F, "index"), curl(F, "middle"),
  curl(F, "ring"), curl(F, "pinky"), notExt(F, "thumb"), aim(F.dir.index, axis));

export const RULES = [
  ["thumbs_up", thumbRule(UP)],
  ["thumbs_down", thumbRule(DOWN)],
  ["open_palm", (F) => allOf(...Object.keys(JOINTS).map((f) => ext(F, f)))],
  ["closed_fist", (F) => allOf(notExt(F, "thumb"), curl(F, "index"),
    curl(F, "middle"), curl(F, "ring"), curl(F, "pinky"))],
  ["pointing_up", pointRule(UP)],
  ["pointing_down", pointRule(DOWN)],
  ["pointing_left", pointRule(LEFT)],
  ["pointing_right", pointRule(RIGHT)],
  ["peace", (F) => allOf(ext(F, "index"), ext(F, "middle"), curl(F, "ring"),
    curl(F, "pinky"), notExt(F, "thumb"), smooth(F.spread, 6, 16))],
  ["ok", (F) => allOf(invSmooth(F.pinch, 0.25, 0.50), ext(F, "middle"),
    ext(F, "ring"), ext(F, "pinky"), notExt(F, "index"))],
  ["rock", (F) => allOf(ext(F, "index"), ext(F, "pinky"), curl(F, "middle"),
    curl(F, "ring"), notExt(F, "thumb"))],
  ["i_love_you", (F) => allOf(ext(F, "thumb"), ext(F, "index"), ext(F, "pinky"),
    curl(F, "middle"), curl(F, "ring"))],
  ["call_me", (F) => allOf(ext(F, "thumb"), ext(F, "pinky"), curl(F, "index"),
    curl(F, "middle"), curl(F, "ring"))],
  ["finger_gun", (F) => {
    const a = angle(F.dir.thumb, F.dir.index);
    return allOf(ext(F, "thumb"), ext(F, "index"), curl(F, "middle"),
      curl(F, "ring"), curl(F, "pinky"),
      allOf(smooth(a, 30, 55), invSmooth(a, 105, 135)));
  }],
];

/** Best single-frame match `{gesture, confidence}` or null. */
export function classify(L) {
  const F = features(L);
  let best = null;
  for (const [gesture, score] of RULES) {
    const c = score(F);
    if (c >= MIN_CONFIDENCE && (!best || c > best.confidence)) best = { gesture, confidence: c };
  }
  return best;
}

/**
 * One raw HandLandmarker hand (un-mirrored camera frame) -> `{hand, L}` in
 * selfie space: x is flipped, and the "Left"/"Right" label is swapped
 * because MediaPipe labels hands as if the image were already mirrored.
 * Returns null for an unlabelled hand.
 */
export function fromMediaPipe(lms, label) {
  const hand = label === "Left" ? "right" : label === "Right" ? "left" : null;
  return hand && { hand, L: lms.map((p) => [1 - p.x, p.y, p.z || 0]) };
}

// ---- temporal stabilisation (per hand) ------------------------------------
export class Stabilizer {
  constructor(history = 7, minVotes = 4, alpha = 0.35) {
    Object.assign(this, { history, minVotes, alpha });
    this.reset();
  }
  reset() { this.votes = []; this.current = null; this.conf = 0; }
  update(m) {
    this.votes.push(m ? m.gesture : null);
    if (this.votes.length > this.history) this.votes.shift();
    const counts = new Map();
    for (const v of this.votes) counts.set(v, (counts.get(v) || 0) + 1);
    let winner = null, n = 0;
    for (const [v, c] of counts) if (c > n) { winner = v; n = c; }  // first-seen wins ties, like Counter
    if (n >= this.minVotes && winner !== this.current) {
      this.current = winner;
      this.conf = m && m.gesture === winner ? m.confidence : 0;
    }
    if (this.current === null) return null;
    const target = m && m.gesture === this.current ? m.confidence : 0;
    this.conf += this.alpha * (target - this.conf);
    return { gesture: this.current, confidence: this.conf };
  }
}

/** Per-hand stabilised recognition. `hand` is "left" | "right". */
export class GestureEngine {
  constructor() { this.hands = {}; }
  process(hand, L) {
    const s = this.hands[hand] || (this.hands[hand] = new Stabilizer());
    return s.update(classify(L));
  }
  lost(hand) { if (this.hands[hand]) this.hands[hand].reset(); }
}
