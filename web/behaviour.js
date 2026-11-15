// Nonverbal behaviour for the 3D interviewer, CPU only: a port of tools/photoreal/behaviour.py.
// Turns the conversation state (idle / listening / thinking / speaking), the speech audio and each
// sentence's emotion into per-frame controls: head pitch/yaw/roll (degrees), blink (0..1), brow (-20..24,
// LivePortrait-style units), smile (0..1), gaze_x/gaze_y (degrees-ish). Everything eases through springs.
// Timings follow conversation research for people on video calls:
//   blinks ~15/min listening, 20/min speaking, 8/min thinking, gamma-like gaps never under 0.6 s, more likely
//   at sentence ends and after gaze shifts, ~12% double blinks; eye contact while listening, a look-away when
//   starting a turn, look up-and-aside while thinking; nods and brow raises on pitch accents while speaking
//   and backchannel nods when the candidate pauses; a smile that eases in over 0.5 s and fades over ~3 s.

export const FPS = 25;
export const EMOTION = {   // smile, brow, roll (deg), pitch (deg)
  neutral: [0, 0, 0, 0], warm: [0.55, 3, 2.0, 0], encouraging: [0.45, 2, 0, 0.5], curious: [0.1, 14, 3.5, -0.5],
  impressed: [0.35, 18, 0, 0], skeptical: [-0.15, -12, 0, -2.0], thinking: [0, -6, 1.5, 0], amused: [0.9, 2, 2.5, 0],
};
const BLINK_DUR = 0.24;     // 60 ms fast close, 38 ms full-closure hold, 142 ms smooth cubic reopen
function blinkCurve(s) {
  if (s <= 0 || s >= BLINK_DUR) return 0;
  if (s < 0.06) { const u = s / 0.06; return u * u * (3 - 2 * u); }
  if (s < 0.098) return 1;
  const u = (s - 0.098) / (BLINK_DUR - 0.098);
  return (1 - u) * (1 - u) * (1 + 0.5 * u);
}

class Spring {
  constructor(tau) { this.v = 0; this.target = 0; this.tau = tau; }
  step(dt) { this.v += (this.target - this.v) * (1 - Math.exp(-dt / this.tau)); return this.v; }
}
const gamma = (k, theta) => {       // Marsaglia-Tsang, k >= 1
  const d = k - 1 / 3, c = 1 / Math.sqrt(9 * d);
  for (;;) {
    let x, v;
    do { x = randn(); v = 1 + c * x; } while (v <= 0);
    v = v * v * v; const u = Math.random();
    if (u < 1 - 0.0331 * x ** 4 || Math.log(u) < 0.5 * x * x + d * (1 - v + Math.log(v))) return d * v * theta;
  }
};
const randn = () => Math.sqrt(-2 * Math.log(1 - Math.random())) * Math.cos(2 * Math.PI * Math.random());
const U = (a, b) => a + Math.random() * (b - a);
const sign = () => (Math.random() < 0.5 ? -1 : 1);

export class Behaviour {
  constructor() {
    this.s = { pitch: new Spring(0.35), yaw: new Spring(0.4), roll: new Spring(0.5), brow: new Spring(0.18),
               smile: new Spring(0.6), gaze_x: new Spring(0.055), gaze_y: new Spring(0.055) };
    this.mode = "idle"; this.emotion = "neutral"; this.t = 0;
    this.nextBlink = 2.0; this.blinkAt = null; this.lastBlink = -10;
    this.phase = Array.from({ length: 6 }, () => Math.random() * 6.3);
    this.pose = [0, 0, 0]; this.nextPose = 4.0;
    this.glanceUntil = 0; this.glanceNext = 3.0;
    this.nextFixation = 0; this.fixation = [0, 0];
    this.pulses = []; this.emotionUntil = 0;
    this.emotionScale = 1; this.lastReaction = ""; this.reactionAfter = 0; this.lastAccentKind = "";
  }

  // ---- events --------------------------------------------------------------------------------------
  setMode(mode) {            // idle | listening (candidate talking) | thinking (LLM working) | speaking
    if (mode === this.mode) return;
    const prev = this.mode;
    this.mode = mode;
    if (prev === "speaking") this.cancelFuturePulses();
    if (mode === "thinking") {
      if (Math.random() < 0.65) this.glance(sign() * U(7, 13), U(3, 9), U(0.5, 1.4));
      this.setEmotion("thinking", 2.5);
      this.pose = [sign() * U(0.6, 1.8), U(-0.8, 0.4), sign() * U(0.4, 1.2)];
      this.nextPose = this.t + U(3.5, 6.5);
    } else if (mode === "listening") {
      this.glanceUntil = 0;
      if (this.emotion === "thinking") this.emotion = "neutral";
      this.pose = [U(-0.4, 0.4), U(-0.2, 0.3), U(-0.4, 0.6)];
      this.nextPose = this.t + U(6.0, 11.0);
    } else if (mode === "idle") {
      this.glanceUntil = 0;
      this.pose = [U(-0.6, 0.6), U(-0.3, 0.3), U(-0.4, 0.4)];
      this.nextPose = this.t + U(5.0, 9.0);
    }
  }
  setEmotion(e, hold = 3.0) {
    if (e in EMOTION) {
      if (e !== this.emotion || this.t >= this.emotionUntil) this.emotionScale = U(0.7, 1.05);
      this.emotion = e; this.emotionUntil = this.t + hold;
    }
  }
  cancelFuturePulses() {
    // Drop unstarted scheduled gestures on barge-in or mode exit; let in-flight pulses finish smoothly.
    this.pulses = this.pulses.filter(([t0, t1]) => t0 <= this.t && t1 > this.t);
    this.sentenceEnd = 0;
  }
  endSentence(completed = false) {
    this.cancelFuturePulses();
    this.glanceUntil = 0;
    if (completed) {
      this.pulse("pitch", U(0.55, 1.05), U(0.32, 0.48), 0.02);
      this.blinkSoon(U(0.06, 0.22), 0.55);
    }
    if (this.mode === "speaking") this.setMode("idle");
  }
  candidatePaused() {
    if (this.t < this.reactionAfter || Math.random() < 0.4) return;
    const choices = ["nod", "brow", "tilt"].filter((x) => x !== this.lastReaction);
    const reaction = choices[Math.floor(Math.random() * choices.length)];
    this.lastReaction = reaction; this.reactionAfter = this.t + U(4, 8);
    const delay = U(0.08, 0.3);
    if (reaction === "nod") this.pulse("pitch", U(1.2, 2.5), U(0.45, 0.75), delay);
    if (reaction === "brow") this.pulse("brow", U(3, 6), U(0.5, 0.9), delay);
    if (reaction === "tilt") this.pulse("roll", sign() * U(0.7, 1.5), U(0.7, 1.1), delay);
    this.blinkSoon(U(0.1, 0.3), 0.2);
  }
  blinkSoon(delay = 0.1, chance = 1) { if (Math.random() < chance && this.t - this.lastBlink > 0.7) this.nextBlink = Math.min(this.nextBlink, this.t + delay); }
  glance(x, y, seconds) { this.s.gaze_x.target = x; this.s.gaze_y.target = y; this.glanceUntil = this.t + seconds; this.blinkSoon(0.1, 0.5); }
  pulse(control, amp, seconds, delay = 0, tag = "") { this.pulses.push([this.t + delay, this.t + delay + seconds, control, amp, tag]); }

  /** A sentence starts playing now: schedule its emphasis. accents = seconds into the audio of pitch accents. */
  startSentence({ duration, accents = [], isQuestion = false, emotion = null, firstOfTurn = false, lead = 0 }) {
    this.cancelFuturePulses();
    this.setMode("speaking");
    const off = Math.max(0, lead);
    if (emotion) this.setEmotion(emotion, off + duration + 3.0);
    if (firstOfTurn) {
      if (Math.random() < 0.5) this.glance(sign() * U(4, 8), U(1, 5), U(0.3, 0.7));
      this.pose = [sign() * U(0.3, 1.1), U(-0.3, 0.4), sign() * U(0.2, 0.7)];
      this.nextPose = this.t + U(3.0, 5.5);
    }
    let lastAccent = -2;
    for (const a of accents) {
      if (a - lastAccent < 1.1 || a < 0.08 || a > duration - 0.15) continue;
      if (Math.random() < 0.45) continue;
      const kinds = ["pitch", "brow", "both", "tilt"].filter((k) => k !== this.lastAccentKind);
      const kind = kinds[Math.floor(Math.random() * kinds.length)];
      this.lastAccentKind = kind;
      const at = off + a;
      if (kind === "pitch") this.pulse("pitch", U(0.8, 1.7), U(0.38, 0.58), Math.max(0, at - 0.09), "speech");
      else if (kind === "brow") this.pulse("brow", U(3.2, 6.8), U(0.4, 0.64), Math.max(0, at - 0.12), "speech");
      else if (kind === "both") {
        this.pulse("brow", U(2.8, 5.5), U(0.4, 0.6), Math.max(0, at - 0.12), "speech");
        this.pulse("pitch", U(0.6, 1.3), U(0.36, 0.52), Math.max(0, at - 0.08), "speech");
      } else this.pulse("roll", sign() * U(0.45, 1.05), U(0.45, 0.7), Math.max(0, at - 0.1), "speech");
      lastAccent = a;
    }
    if (isQuestion && duration > 0.8) {      // turn-yielding: brows up, look at them
      if (Math.random() < 0.55) this.pulse("brow", U(3, 7), U(0.7, 1.1), off + duration - 0.8, "speech");
      else if (Math.random() < 0.45) this.pulse("roll", sign() * U(0.5, 1.3), 0.8, Math.max(0, off + duration - 0.6), "speech");
      this.glanceNext = this.t + off + duration + 1.0;
    }
    this.sentenceEnd = this.t + off + duration;
  }

  // ---- per-frame -----------------------------------------------------------------------------------
  tick(dt) {
    this.t += dt;
    const t = this.t;
    if (t >= this.emotionUntil && this.emotion !== "neutral" && this.mode !== "speaking") this.emotion = "neutral";
    const base = EMOTION[t < this.emotionUntil || this.mode === "speaking" ? this.emotion : "neutral"].map((v) => v * this.emotionScale);
    const ph = this.phase, s = this.s;
    if (t >= this.nextPose) {
      const span = this.mode === "listening" ? 0.35 : this.mode === "idle" ? 0.5 : 0.85;
      this.pose = [U(-1.1, 1.1) * span, U(-0.6, 0.6) * span, U(-0.5, 0.5) * span];
      this.nextPose = t + (this.mode === "listening" ? U(6.5, 11.5) : U(4.0, 8.0));
    }
    // Calm conversational poise: discrete spring-damped posture targets + tiny physiological micro-sway
    // instead of continuous large sinusoidal bobbing.
    const sway = { listening: 0.16, idle: 0.22, speaking: 0.36, thinking: 0.32 }[this.mode] ?? 0.22;
    const gazeFollowX = Math.max(-1.2, Math.min(1.2, s.gaze_x.target * 0.08));
    const gazeFollowY = Math.max(-0.8, Math.min(0.8, s.gaze_y.target * 0.06));
    s.yaw.target = this.pose[0] + gazeFollowX + sway * Math.sin(0.27 * t + ph[0]);
    s.pitch.target = base[3] + this.pose[1] - gazeFollowY + sway * 0.7 * Math.sin(0.33 * t + ph[2]);
    s.roll.target = base[2] + this.pose[2] + sway * 0.5 * Math.sin(0.21 * t + ph[4]);
    s.brow.target = base[1];
    s.smile.target = base[0] + (this.mode === "listening" ? 0.12 : 0);
    if (t >= this.glanceUntil) {
      // Alternate face-triangle fixation points (left eye, right eye, nose bridge, mouth)
      // rather than uniform random box drift, mirroring human social gaze scanpaths.
      if (t >= this.nextFixation) {
        const tri = [[-0.75, 0.28], [0.75, 0.28], [0.0, 0.12], [0.0, -0.48]];
        const pt = tri[Math.floor(Math.random() * (this.mode === "listening" ? 4 : 3))];
        this.fixation = [pt[0] + U(-0.22, 0.22), pt[1] + U(-0.16, 0.16)];
        this.nextFixation = t + U(1.1, 3.0);
      }
      [s.gaze_x.target, s.gaze_y.target] = this.fixation;
      if ((this.mode === "speaking" || this.mode === "idle") && t >= this.glanceNext) {
        if (Math.random() < 0.45) this.glance(sign() * U(4, 8), U(-2, 3), U(0.3, 0.65));
        this.glanceNext = t + U(4.0, 8.0);
      } else if (this.mode === "listening" || this.mode === "thinking") this.glanceNext = t + U(4, 8);
    }
    if (this.sentenceEnd && t >= this.sentenceEnd - 0.08) { this.blinkSoon(0.05 + Math.random() * 0.2, 0.7); this.sentenceEnd = 0; }
    const out = {}; for (const k in s) out[k] = s[k].step(dt);
    for (const p of [...this.pulses]) {     // sin^2 bumps, added after the springs
      const [t0, t1, c, a] = p;
      if (t > t1) this.pulses.splice(this.pulses.indexOf(p), 1);
      else if (t >= t0) out[c] += a * Math.sin(Math.PI * (t - t0) / (t1 - t0)) ** 2;
    }
    // blinks: gamma-like gaps around the mode's rate, never under 0.6 s; continuous sub-frame curve
    let blink = 0;
    if (this.blinkAt !== null) {
      this.blinkAt += dt;
      if (this.blinkAt >= BLINK_DUR) { this.blinkAt = null; if (Math.random() < 0.12) this.nextBlink = t + 0.24; }
      else blink = blinkCurve(this.blinkAt);
    } else if (t >= this.nextBlink) {
      this.blinkAt = 0.012; this.lastBlink = t; blink = blinkCurve(this.blinkAt);
      const mean = 60 / { listening: 15, speaking: 20, thinking: 8, idle: 15 }[this.mode];
      this.nextBlink = t + Math.max(0.6, gamma(2.5, mean / 2.5));
    }
    out.blink = blink;
    return out;
  }
}

/** Pitch accents (seconds) and a final F0 rise, from a decoded sentence (Float32 PCM). yin on 8 kHz audio:
 *  ~15 ms for a 10 s sentence. Voicing is approximated by loudness. */
export function prosody(pcm, sr) {
  const ds = Math.max(1, Math.round(sr / 8000)), n = Math.floor(pcm.length / ds), x = new Float32Array(n);
  for (let i = 0; i < n; i++) { let a = 0; for (let j = 0; j < ds; j++) a += pcm[i * ds + j]; x[i] = a / ds; }
  const fs = sr / ds, hop = Math.max(1, Math.round(fs / FPS)), W = 512, lagMin = Math.floor(fs / 400), lagMax = Math.ceil(fs / 70);
  const frames = Math.max(0, 1 + Math.floor((n - W - lagMax) / hop));
  if (frames < 5) return { accents: [], rise: false };
  const f0 = new Float32Array(frames), rms = new Float32Array(frames);
  for (let f = 0; f < frames; f++) {
    const o = f * hop; let e = 0;
    for (let i = 0; i < W; i++) e += x[o + i] * x[o + i];
    rms[f] = Math.sqrt(e / W);
    // yin: cumulative-mean-normalised difference, first dip under 0.15
    let run = 0, best = -1, bestV = 1;
    for (let lag = 1; lag <= lagMax; lag++) {
      let d = 0; for (let i = 0; i < W; i++) { const q = x[o + i] - x[o + i + lag]; d += q * q; }
      run += d; const v = lag < lagMin ? 1 : d * lag / (run || 1e-9);
      if (lag >= lagMin) { if (v < 0.15) { best = lag; break; } if (v < bestV) { bestV = v; best = lag; } }
    }
    f0[f] = best > 0 ? fs / best : 0;
  }
  const loud = [...rms].filter((v) => v > 0.004).sort((a, b) => a - b), medE = loud[loud.length >> 1] || 0;
  const voiced = [...rms].map((v) => v > Math.max(0.004, 0.35 * medE));
  const vf = f0.filter((_, i) => voiced[i]); if (vf.length < 5) return { accents: [], rise: false };
  const sorted = [...vf].sort((a, b) => a - b), med = sorted[sorted.length >> 1];
  const sd = Math.sqrt(vf.reduce((s, v) => s + (v - med) ** 2, 0) / vf.length);
  const vrms = [...rms].filter((_, i) => voiced[i]).sort((a, b) => a - b), medV = vrms[vrms.length >> 1];
  const accents = []; let last = -100;
  for (let i = 1; i < frames - 1; i++)
    if (voiced[i] && f0[i] >= f0[i - 1] && f0[i] > f0[i + 1] && f0[i] > med + 0.5 * sd && rms[i] > medV && i - last >= 0.3 * FPS) { accents.push(i / FPS); last = i; }
  const vi = voiced.map((v, i) => (v ? i : -1)).filter((i) => i >= 0);
  const mean = (idx) => idx.reduce((s, i) => s + f0[i], 0) / (idx.length || 1);
  const rise = vi.length > 30 && mean(vi.slice(-10)) > 1.08 * mean(vi.slice(-35, -10));
  return { accents, rise };
}
