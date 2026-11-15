// Face + head animation for an ARKit-52 / Oculus-viseme GLB (e.g. assets/models/mpfb.glb).
//
// Lip sync:  real phoneme-timed Oculus visemes from the backend, coarticulated (blend into the
//            next viseme before it starts), scaled by the audio loudness envelope; closures
//            (PP/FF) always seal.
// Head:      critically-damped springs toward targets made of smooth noise + speech prosody
//            (nods/tilts on stressed-syllable onsets, settle at sentence end) + gaze following.
// Eyes:      saccades with micro-saccades, blinks on gaze shifts and at random, mood gaze.
import * as THREE from "three";
import { Behaviour, prosody } from "./behaviour.js";

// Oculus viseme -> blendshape weights + coupled ARKit lower-face articulators (so browser/GitHub Pages
// mode without server wav2arkit still gets realistic lip roll, press, funnel, stretch and jaw ceiling).
const OCULUS = {
  sil: {},
  PP: { viseme_PP: 0.92, mouthPressLeft: 0.14, mouthPressRight: 0.14 },
  FF: { viseme_FF: 0.9, mouthRollLower: 0.12, mouthUpperUpLeft: 0.08, mouthUpperUpRight: 0.08, jawOpen: 0.03 },
  TH: { viseme_TH: 0.86, jawOpen: 0.06, tongueOut: 0.2, mouthLowerDownLeft: 0.1, mouthLowerDownRight: 0.1 },
  DD: { viseme_DD: 0.82, jawOpen: 0.05, mouthStretchLeft: 0.1, mouthStretchRight: 0.1 },
  kk: { viseme_kk: 0.82, jawOpen: 0.08 },
  CH: { viseme_CH: 0.9, mouthFunnel: 0.28, mouthPucker: 0.16, jawOpen: 0.05 },
  SS: { viseme_SS: 0.86, mouthStretchLeft: 0.14, mouthStretchRight: 0.14, jawOpen: 0.025 },
  nn: { viseme_nn: 0.78, jawOpen: 0.05 },
  RR: { viseme_RR: 0.86, mouthFunnel: 0.22, mouthPucker: 0.18, jawOpen: 0.05 },
  aa: { viseme_aa: 0.92, jawOpen: 0.16, mouthLowerDownLeft: 0.16, mouthLowerDownRight: 0.16, cheekSquintLeft: 0.06, cheekSquintRight: 0.06 },
  E: { viseme_E: 0.88, jawOpen: 0.09, mouthStretchLeft: 0.16, mouthStretchRight: 0.16, mouthLowerDownLeft: 0.1, mouthLowerDownRight: 0.1 },
  I: { viseme_I: 0.84, jawOpen: 0.05, mouthStretchLeft: 0.2, mouthStretchRight: 0.2 },
  O: { viseme_O: 0.92, jawOpen: 0.12, mouthFunnel: 0.32, mouthPucker: 0.2 },
  U: { viseme_U: 0.92, jawOpen: 0.06, mouthPucker: 0.38, mouthFunnel: 0.2 },
};
// Phoneme-specific loudness-to-jaw scaling for non-neural lip sync (prevents sibilants/close vowels from over-dropping the jaw)
const JAW_ENV_GAIN = {
  sil: 0, PP: 0, FF: 0.03, SS: 0.04, I: 0.06, DD: 0.07, nn: 0.07, CH: 0.07, RR: 0.08, U: 0.08,
  TH: 0.09, kk: 0.11, E: 0.15, O: 0.19, aa: 0.24,
};
const CLOSURES = new Set(["PP", "FF"]);     // must reach full weight regardless of loudness
const RHUBARB_TO_OCULUS = { A: "PP", B: "SS", C: "E", D: "aa", E: "O", F: "U", G: "FF", H: "nn", X: "sil" };

// LLM emotion tag -> expression overlay (+ head roll/pitch bias). Blended slowly.
const EMOTIONS = {
  neutral: {},
  warm: { mouthSmileLeft: 0.35, mouthSmileRight: 0.35, cheekSquintLeft: 0.15, cheekSquintRight: 0.15, browInnerUp: 0.1 },
  curious: { browInnerUp: 0.35, browOuterUpLeft: 0.2, browOuterUpRight: 0.2, _roll: 0.06 },
  impressed: { browOuterUpLeft: 0.4, browOuterUpRight: 0.4, mouthSmileLeft: 0.25, mouthSmileRight: 0.25,
               eyeWideLeft: 0.2, eyeWideRight: 0.2, _pitch: -0.03 },
  skeptical: { browDownLeft: 0.4, browOuterUpRight: 0.25, mouthPressLeft: 0.3, eyeSquintLeft: 0.25,
               eyeSquintRight: 0.1, _roll: -0.05 },
  thinking: { browDownLeft: 0.25, browDownRight: 0.25, mouthPucker: 0.12, mouthRollLower: 0.1, _roll: 0.04 },
  encouraging: { mouthSmileLeft: 0.3, mouthSmileRight: 0.3, browInnerUp: 0.25, _pitch: 0.03 },
  amused: { mouthSmileLeft: 0.5, mouthSmileRight: 0.5, cheekSquintLeft: 0.3, cheekSquintRight: 0.3,
            eyeSquintLeft: 0.15, eyeSquintRight: 0.15 },
};

// Neural lip sync (server: wav2arkit, audio -> 52 ARKit weights @ 30 fps). Only lower-face channels
// are used - eyes, brows and head stay with our own idle/emotion logic. Gains are calibrated so
// jaw drop and upper-lip lift stay natural without over-exposing teeth or gums.
const NEURAL_CHANNEL = /^(jaw|mouth|cheek|tongue|noseSneer)/;
const NEURAL_GAIN = { jawOpen: 2.15, mouthLowerDownLeft: 0.85, mouthLowerDownRight: 0.85, mouthUpperUpLeft: 0.76,
                      mouthUpperUpRight: 0.76, mouthFunnel: 1.5, mouthPucker: 1.35, mouthClose: 0.62, mouthRollLower: 0.7,
                      mouthRollUpper: 0.95, mouthPressLeft: 0.75, mouthPressRight: 0.75, mouthShrugLower: 0.42,
                      mouthShrugUpper: 0.75, mouthStretchLeft: 1.25, mouthStretchRight: 1.25 };

const COART_S = 0.068;  // start blending into the next viseme this long before it begins
const LEAD_S = 0.035;   // mouth leads the audio slightly (perceived sync)

// Soft viscoelastic tissue curve: eases in gently at low weights and avoids harsh linear snaps
function softTissue(x, gamma = 1.22) {
  const c = Math.max(0, Math.min(1, x));
  return Math.pow(c, gamma);
}

// smooth 1D noise: sum of incommensurate sines with random phases, roughly -1..1
function noise1(seed) {
  const ph = [0, 1, 2].map(() => Math.random() * 100);
  return (t) => (Math.sin(t * 0.31 * seed + ph[0]) * 0.5 + Math.sin(t * 0.73 * seed + ph[1]) * 0.3
                 + Math.sin(t * 1.37 * seed + ph[2]) * 0.2);
}

// critically damped spring (no overshoot), omega = stiffness in rad/s
class Spring {
  constructor(omega) { this.x = 0; this.v = 0; this.w = omega; }
  step(target, dt) {
    // substep: explicit integration of a stiff spring blows up when frames are slow
    // (background tabs, software rendering), swinging the head to extremes
    const n = Math.max(1, Math.ceil(dt * 120)), h = dt / n, w = this.w;
    for (let i = 0; i < n; i++) {
      const a = -w * w * (this.x - target) - 2 * w * this.v;
      this.v += a * h; this.x += this.v * h;
    }
    if (!Number.isFinite(this.x)) { this.x = target; this.v = 0; }
    return this.x;
  }
  kick(dv) { this.v += dv; }
}

// Unreal MetaHuman skeleton -> our canonical bone names. Arms are left out: the MetaHuman bind pose is
// already relaxed and its bone axes differ, so the MPFB T-pose arm drop would twist them.
const METAHUMAN_BONES = { head: "Head", neck_01: "Neck", spine_03: "Spine", spine_04: "Spine1", spine_05: "Spine2",
                          clavicle_l: "LeftShoulder", clavicle_r: "RightShoulder" };

// Oculus viseme -> ARKit shapes, for heads that only carry ARKit blendshapes (e.g. a MetaHuman GLB).
const VISEME_ARKIT = {
  PP: { mouthClose: 0.5, mouthPressLeft: 0.5, mouthPressRight: 0.5, mouthRollLower: 0.3, mouthRollUpper: 0.3 },
  FF: { mouthRollLower: 0.6, mouthUpperUpLeft: 0.2, mouthUpperUpRight: 0.2, jawOpen: 0.08 },
  TH: { jawOpen: 0.15, mouthLowerDownLeft: 0.15, mouthLowerDownRight: 0.15 },
  DD: { jawOpen: 0.2, mouthStretchLeft: 0.15, mouthStretchRight: 0.15 },
  kk: { jawOpen: 0.22 },
  CH: { jawOpen: 0.12, mouthFunnel: 0.55, mouthPucker: 0.3 },
  SS: { jawOpen: 0.08, mouthStretchLeft: 0.3, mouthStretchRight: 0.3 },
  nn: { jawOpen: 0.15 },
  RR: { jawOpen: 0.12, mouthFunnel: 0.4, mouthPucker: 0.35 },
  aa: { jawOpen: 0.5, mouthLowerDownLeft: 0.3, mouthLowerDownRight: 0.3 },
  E: { jawOpen: 0.28, mouthStretchLeft: 0.35, mouthStretchRight: 0.35 },
  I: { jawOpen: 0.15, mouthStretchLeft: 0.45, mouthStretchRight: 0.45 },
  O: { jawOpen: 0.35, mouthFunnel: 0.6, mouthPucker: 0.3 },
  U: { jawOpen: 0.12, mouthPucker: 0.7, mouthFunnel: 0.35 },
};

export class AvatarRig {
  constructor(root) {
    this.meshes = [];
    this.bones = {};
    let metahuman = false;
    root.traverse((o) => { if (o.isBone && o.name === "neck_01") metahuman = true; });
    root.updateMatrixWorld(true);
    const rootInv = root.matrixWorld.clone().invert(), m = new THREE.Matrix4();
    root.traverse((o) => {
      if (o.isMesh) { o.frustumCulled = false; if (o.morphTargetDictionary) this.meshes.push(o); }
      if (!o.isBone) return;
      const name = metahuman ? METAHUMAN_BONES[o.name] : o.name;
      if (!name || !["Head", "Neck", "Spine", "Spine1", "Spine2", "LeftShoulder", "RightShoulder", "LeftArm", "RightArm"].includes(name)) return;
      if (this.bones[name]) return;                 // MetaHuman GLB has a body and a face skeleton: keep the first
      const b = this.bones[name] = { bone: o, rest: o.quaternion.clone() };
      // MetaHuman bone axes aren't model-aligned: rotate about model axes (pitch = X, yaw = Y, roll = Z) instead
      if (metahuman) b.frame = new THREE.Quaternion().setFromRotationMatrix(m.multiplyMatrices(rootInv, o.matrixWorld));
    });
    // body, face and outfit each have their own skeleton; GLTFLoader renames the duplicates (head_1, head_2).
    // Mirror the body skeleton's pose onto the copies every frame, or the face stays frozen while the body turns.
    this.mirrors = [];
    if (metahuman) root.traverse((o) => {
      const name = o.isBone && METAHUMAN_BONES[o.name.replace(/_\d$/, "")], b = name && this.bones[name];
      if (b && b.bone !== o) {
        b.restPosM ??= b.bone.position.clone();
        this.mirrors.push([o, b.bone, o.quaternion.clone(), b.rest, o.position.clone(), b.restPosM]);
      }
    });
    // groom cards (hair, brows) are static meshes in the GLB; UE binds them to the head - do the same,
    // keeping their world pose, so they turn and nod with the face
    if (metahuman && this.bones.Head) {
      const cards = [];
      root.traverse((o) => { if (o.isMesh && !o.isSkinnedMesh && /Hair_|Eyebrows_|Beard_|Mustache_/.test(o.name)) cards.push(o); });
      for (const o of cards) this.bones.Head.bone.attach(o);
    }
    this.visemeFallback = !this.meshes.some((x) => "viseme_aa" in x.morphTargetDictionary);
    this._rot("LeftArm", THREE.MathUtils.degToRad(34), 0, 0);
    this._rot("RightArm", THREE.MathUtils.degToRad(34), 0, 0);

    this.weights = {};
    this.lipsync = "hybrid";
    this.face = {};
    this.mood = "listening";
    this.speech = null;           // {cues, env, envFps, clock(): seconds into clip}
    this.userTalking = false;
    this.t = 0;
    // head springs: pitch (nod), yaw (turn), roll (tilt)
    this.sp = { pitch: new Spring(9), yaw: new Spring(7), roll: new Spring(7) };
    this.nz = { pitch: noise1(1.0), yaw: noise1(0.8), roll: noise1(0.6) };
    // gaze (eyes jump, head follows)
    this.gaze = new THREE.Vector2(); this.gazeTarget = new THREE.Vector2(); this.nextSaccade = 0;
    this.nextBlink = 1.5; this.blinkT = -1;
    this.lastEnv = 0; this.onsetCooldown = 0; this.nextListenNod = 2;
    this.emotion = EMOTIONS.neutral; this.emotionUntil = 0;
    this.setMood("listening");
  }

  /** Expression for the current sentence (from the LLM's [tag]); fades out after `hold` seconds. */
  setEmotion(name, hold = 6) {
    this.emotion = EMOTIONS[name] ?? EMOTIONS.neutral;
    this.emotionUntil = this.t + hold;
    if (name === "impressed" || name === "encouraging") this.sp.pitch.kick(0.6);   // nod with it
  }

  headWorldPosition(v = new THREE.Vector3()) {
    return (this.bones.Head?.bone ?? this.meshes[0]).getWorldPosition(v);
  }

  setMood(m) {
    this.mood = m;
    const f = this.face;
    for (const k of Object.keys(f)) f[k] = 0;
    const pair = (b, w) => { f[b + "Left"] = w; f[b + "Right"] = w; };
    if (m === "listening") { pair("mouthSmile", 0.16); f.browInnerUp = 0.12; }
    if (m === "thinking") { pair("browDown", 0.3); pair("eyeSquint", 0.18); pair("mouthPress", 0.2);
                            this._saccade(new THREE.Vector2((Math.random() < 0.5 ? -1 : 1) * 0.4, 0.35)); }
    if (m === "speaking") pair("mouthSmile", 0.08);
  }

  startSpeech(msg, clock) {
    const cues = msg.visemes.map((c) => ({ t: c.t, o: c.o ?? RHUBARB_TO_OCULUS[c.v] ?? "sil" }));
    this.speech = { cues, env: msg.env ?? null, envFps: msg.env_fps ?? 50, clock, i: -1 };
    if (msg.arkit?.length && msg.arkit_names) {
      const idx = msg.arkit_names.map((n, i) => [n, i]).filter(([n]) => NEURAL_CHANNEL.test(n));
      this.speech.neural = { frames: msg.arkit, fps: msg.arkit_fps || 30, idx };
    }
    if (this.mood !== "speaking") this.setMood("speaking");
    if (msg.emotion) this.setEmotion(msg.emotion, (msg.duration ?? 3) + 1.5);
    this._blink();                                   // people often blink as they start a phrase
  }
  endSpeech() {
    if (this.speech) this.sp.pitch.kick(0.25);       // settle: small downward drop at phrase end
    this.speech = null;
  }

  update(dt) {
    this.t += dt;
    const target = { ...this.face };
    const add = (k, w) => { target[k] = Math.max(target[k] ?? 0, w); };
    if (this.t > this.emotionUntil) this.emotion = EMOTIONS.neutral;
    for (const [k, w] of Object.entries(this.emotion)) if (!k.startsWith("_")) add(k, w);
    const env = this._lipsync(target, add, dt);
    this._face(target, add, dt, env);
    this._clampMouthOverlap(target);
    this._apply(target, dt);
  }

  /** Prevent lower-lip raising/closing morphs from stacking past jawOpen/viseme seals and overlapping the upper lip. */
  _clampMouthOverlap(target) {
    const jaw = target.jawOpen ?? 0;
    const lipSeal = Math.min(1, (target.viseme_PP ?? 0) + 0.75 * (target.viseme_FF ?? 0));
    if (target.mouthClose) {
      // mouthClose compensates for jawOpen; when the jaw is near rest or viseme_PP/FF already seals the lips,
      // full mouthClose drives the lower lip upward past the upper lip.
      const maxClose = Math.max(0, jaw * 0.72 * (1 - 0.85 * lipSeal));
      target.mouthClose = Math.min(target.mouthClose, maxClose);
    }
    if (target.mouthShrugLower) {
      const cap = (this.speech ? Math.min(0.24, 0.1 + jaw * 0.65) : 0.28) * (1 - 0.85 * lipSeal);
      target.mouthShrugLower = Math.min(target.mouthShrugLower, Math.max(0, cap));
    }
    if (target.mouthRollLower) {
      const cap = 0.36 * (1 - 0.65 * lipSeal);
      target.mouthRollLower = Math.min(target.mouthRollLower, Math.max(0, cap));
    }
    for (const n of ["mouthPressLeft", "mouthPressRight"]) {
      if (target[n]) {
        const cap = (this.speech ? 0.36 : 0.48) * (1 - 0.4 * lipSeal);
        target[n] = Math.min(target[n], Math.max(0, cap));
      }
    }
  }

  /** Mouth from the phoneme visemes / neural frames; returns the loudness envelope value. */
  _lipsync(target, add, dt) {
    let env = 0;
    if (this.speech) {
      const s = this.speech, pt = s.clock() + LEAD_S, cues = s.cues;
      while (s.i + 1 < cues.length && cues[s.i + 1].t <= pt) s.i++;
      if (s.env) env = s.env[Math.min(s.env.length - 1, Math.max(0, Math.floor(pt * s.envFps)))] ?? 0;
      // lipsync mode: "hybrid" (default) | "neural" | "visemes"; neural frames only exist in server mode
      const neural = s.neural && this.lipsync !== "visemes" ? s.neural : null;
      const visemeScale = !neural ? 1 : this.lipsync === "neural" ? 0 : 0.3;
      if (neural) {
        const f = Math.max(0, pt * neural.fps), i0 = Math.min(neural.frames.length - 1, Math.floor(f));
        const i1 = Math.min(neural.frames.length - 1, i0 + 1), a = f - Math.floor(f);
        for (const [name, k] of neural.idx) {
          const v = (neural.frames[i0][k] * (1 - a) + neural.frames[i1][k] * a) * (NEURAL_GAIN[name] ?? 1.2);
          target[name] = Math.max(target[name] ?? 0, v);
        }
      }
      if (s.i >= 0) {
        const cur = cues[s.i], nxt = cues[s.i + 1], prv = cues[s.i - 1];
        // Forward + backward coarticulation: ease out of the previous phoneme over the first 42 ms
        // and ease into the next phoneme during the last COART_S (68 ms) of this one.
        let k = 0, kPrev = 0;
        if (nxt) { const x = (pt - (nxt.t - COART_S)) / COART_S; k = x <= 0 ? 0 : x >= 1 ? 1 : x * x * (3 - 2 * x); }
        if (prv && prv.o && prv.o !== "sil" && !CLOSURES.has(cur.o)) {
          const x = (pt - cur.t) / 0.042;
          if (x > 0 && x < 1) kPrev = 0.26 * (1 - x) * (1 - x);
        }
        // closures (p b m / f v) always seal from exact phoneme timing, even in neural mode
        const gain = (o) => CLOSURES.has(o) ? (this.lipsync === "neural" && neural ? 0 : 1) : (0.38 + 0.72 * env) * visemeScale;
        for (const [o, w] of [[prv?.o, kPrev], [cur.o, (1 - k) * (1 - kPrev)], [nxt?.o, k]]) {
          if (!o || w <= 0) continue;
          const g = w * gain(o);
          for (const [shape, v] of Object.entries(OCULUS[o] ?? {})) {
            if (neural && !shape.startsWith("viseme_")) target[shape] = Math.max(target[shape] ?? 0, v * g);
            else target[shape] = (target[shape] ?? 0) + v * g;
          }
        }
        if (!neural) {
          const jawGain = (JAW_ENV_GAIN[cur.o] ?? 0.1) * (1 - k) + (JAW_ENV_GAIN[nxt?.o] ?? 0) * k;
          add("jawOpen", Math.max(target.jawOpen ?? 0, jawGain * env));
        }
        // Seal bilabials (PP) completely and labiodentals (FF) softly against upper teeth
        const ppSeal = (cur.o === "PP" ? 1 - k : 0) + (nxt?.o === "PP" ? k : 0);
        const ffSeal = (cur.o === "FF" ? 1 - k : 0) + (nxt?.o === "FF" ? k : 0);
        const seal = Math.min(1, ppSeal + 0.78 * ffSeal);
        if (seal > 0 && !(this.lipsync === "neural" && neural)) {
          for (const n of ["jawOpen", "mouthLowerDownLeft", "mouthLowerDownRight", "mouthUpperUpLeft", "mouthUpperUpRight",
                           "mouthFunnel", "mouthClose", "mouthShrugLower"])
            if (target[n]) target[n] *= 1 - 0.92 * seal;
        }
        // Corrective volume preservation: wide jaw drop + lip funnel/pucker rolls the lower lip slightly
        // and prevents paper-thin lip stretching on rounded open vowels ("O", "aw", "r")
        const openRound = (target.jawOpen ?? 0) * ((target.mouthFunnel ?? 0) + (target.mouthPucker ?? 0));
        if (openRound > 0.02) {
          add("mouthRollLower", Math.min(0.22, openRound * 0.24));
          add("cheekSquintLeft", openRound * 0.22);
          add("cheekSquintRight", openRound * 0.22);
        }
        // Soft saturation on jaw drop and upper-lip lift so teeth/gums never over-expose on loud vowels
        if (target.jawOpen > 0.52) target.jawOpen = 0.52 + 0.35 * Math.tanh((target.jawOpen - 0.52) / 0.35);
        for (const n of ["mouthUpperUpLeft", "mouthUpperUpRight"])
          if (target[n] > 0.38) target[n] = 0.38 + 0.2 * Math.tanh((target[n] - 0.38) / 0.2);
        // Subtle lateral jaw & mouth micro-motion during articulated speech (avoids purely vertical robotic jaw hinge)
        const lat = Math.sin(this.t * 4.3) * 0.022 * env * (1 - seal);
        if (lat > 0) { add("jawRight", lat); add("mouthRight", lat * 0.7); }
        else if (lat < 0) { add("jawLeft", -lat); add("mouthLeft", -lat * 0.7); }
        if ((target.mouthPucker ?? 0) + (target.mouthFunnel ?? 0) > 0.25)
          add("jawForward", Math.min(0.06, ((target.mouthPucker ?? 0) + (target.mouthFunnel ?? 0)) * 0.09));
      }
      // prosody: a loudness onset (stressed syllable) -> small nod + brow lift
      this.onsetCooldown -= dt;
      if (env - this.lastEnv > 0.18 && env > 0.6 && this.onsetCooldown <= 0) {
        this.sp.pitch.kick(0.35 + Math.random() * 0.3);
        this.sp.yaw.kick((Math.random() - 0.5) * 0.4);
        if (Math.random() < 0.35) this.browPulse = 1;
        this.onsetCooldown = 0.35 + Math.random() * 0.4;
      }
      this.lastEnv = env;
    }
    return env;
  }

  /** Eyes, blinks, brows and head: smooth noise + saccades (the original rig; LiveRig replaces this). */
  _face(target, add, dt, env) {
    const g = this.gaze;
    this.browPulse = Math.max(0, (this.browPulse ?? 0) - dt * 2.5);
    if (this.browPulse > 0) { add("browInnerUp", 0.35 * this.browPulse); add("browOuterUpLeft", 0.25 * this.browPulse); add("browOuterUpRight", 0.25 * this.browPulse); }

    // ---------------- eyes: saccades + blinks
    if (this.t >= this.nextSaccade) {
      const away = this.mood === "thinking" ? 0.7 : this.mood === "speaking" ? 0.3 : 0.15;
      if (Math.random() < away) this._saccade(new THREE.Vector2((Math.random() - 0.5) * 0.8, Math.random() * 0.4 - 0.1));
      else this._saccade(new THREE.Vector2((Math.random() - 0.5) * 0.06, (Math.random() - 0.5) * 0.04)); // micro-saccade / back to camera
    }
    this.gaze.lerp(this.gazeTarget, 1 - Math.exp(-40 * dt));          // saccades are fast
    if (this.t >= this.nextBlink) this._blink();
    let blink = 0;
    if (this.blinkT >= 0) {
      this.blinkT += dt;
      const b = this.blinkT;                                          // 60 ms close, 40 ms hold, 110 ms open
      blink = b < 0.06 ? b / 0.06 : b < 0.1 ? 1 : b < 0.21 ? 1 - (b - 0.1) / 0.11 : 0;
      if (b >= 0.21) this.blinkT = -1;
    }
    Object.assign(target, {
      eyeLookOutLeft: Math.max(0, -g.x), eyeLookInRight: Math.max(0, -g.x),
      eyeLookInLeft: Math.max(0, g.x), eyeLookOutRight: Math.max(0, g.x),
      eyeLookUpLeft: Math.max(0, g.y), eyeLookUpRight: Math.max(0, g.y),
      eyeLookDownLeft: Math.max(0, -g.y), eyeLookDownRight: Math.max(0, -g.y),
    });
    add("eyeBlinkLeft", blink); add("eyeBlinkRight", blink);
    add("eyeBlinkLeft", Math.max(0, -g.y) * 0.3); add("eyeBlinkRight", Math.max(0, -g.y) * 0.3); // lids follow downward gaze
    this._head(g, dt);
  }

  /** Blendshape smoothing and application (asymmetric muscle attack/release, blinks/eyes direct). */
  _apply(target, dt) {
    const mouthAtk = this.speech?.neural && this.lipsync !== "visemes" ? 1 - Math.exp(-48 * dt) : 1 - Math.exp(-34 * dt);
    const mouthRel = 1 - Math.exp(-19 * dt);      // viscoelastic soft-tissue recoil is gentler than contraction
    const expAtk = 1 - Math.exp(-11 * dt), expRel = 1 - Math.exp(-6.5 * dt);
    for (const k of new Set([...Object.keys(target), ...Object.keys(this.weights)])) {
      const direct = k.startsWith("eyeBlink") || k.startsWith("eyeLook");
      const mouthy = k.startsWith("viseme_") || NEURAL_CHANNEL.test(k);
      const cur = this.weights[k] ?? 0, tgt = Math.min(1, target[k] ?? 0);
      const a = direct ? 1 : mouthy ? (tgt >= cur ? mouthAtk : mouthRel) : (tgt >= cur ? expAtk : expRel);
      const next = THREE.MathUtils.lerp(cur, tgt, a);
      this.weights[k] = next < 0.0015 && tgt === 0 ? 0 : next;
    }
    let out = this.weights;
    if (this.visemeFallback) {                     // no viseme_* shapes: express visemes with ARKit shapes
      out = { ...this.weights };
      for (const k in this.weights) {
        const map = k.startsWith("viseme_") && VISEME_ARKIT[k.slice(7)];
        if (map) for (const s in map) out[s] = Math.min(1, (out[s] ?? 0) + map[s] * this.weights[k]);
      }
    }
    for (const m of this.meshes)
      for (const k in out) { const i = m.morphTargetDictionary[k]; if (i !== undefined) m.morphTargetInfluences[i] = out[k]; }
    for (const [o, src, oRest, srcRest, oRestPos, srcRestPos] of this.mirrors) {
      o.quaternion.copy(oRest).multiply(srcRest.clone().invert().multiply(src.quaternion));
      o.position.copy(oRestPos).add(src.position).sub(srcRestPos);   // breathing lifts the spine too
    }
    // Drive dynamic oral-cavity AO, eyelid contact-shadow, and FACS expression wrinkle uniforms
    if (this.shaderUniforms) {
      const w = this.weights;
      const mouthOpen = Math.min(1, (w.jawOpen ?? 0) * 1.45 + (w.viseme_aa ?? 0) * 0.55 + (w.viseme_O ?? 0) * 0.45
                                    + (w.viseme_E ?? 0) * 0.4 + ((w.mouthLowerDownLeft ?? 0) + (w.mouthLowerDownRight ?? 0)) * 0.25);
      const eyeClosure = Math.min(1, 0.5 * ((w.eyeBlinkLeft ?? 0) + (w.eyeBlinkRight ?? 0))
                                     + 0.25 * ((w.eyeSquintLeft ?? 0) + (w.eyeSquintRight ?? 0)));
      if (this.shaderUniforms.uMouthOpen) this.shaderUniforms.uMouthOpen.value = mouthOpen;
      if (this.shaderUniforms.uEyeClosure) this.shaderUniforms.uEyeClosure.value = eyeClosure;
      if (this.shaderUniforms.uExprWrinkles) {
        const browUp = Math.min(1, (w.browInnerUp ?? 0) * 0.85 + 0.5 * ((w.browOuterUpLeft ?? 0) + (w.browOuterUpRight ?? 0)));
        const browDown = Math.min(1, 0.75 * ((w.browDownLeft ?? 0) + (w.browDownRight ?? 0)));
        const smileSquint = Math.min(1, 0.45 * ((w.mouthSmileLeft ?? 0) + (w.mouthSmileRight ?? 0))
                                        + 0.4 * ((w.cheekSquintLeft ?? 0) + (w.cheekSquintRight ?? 0))
                                        + 0.25 * ((w.eyeSquintLeft ?? 0) + (w.eyeSquintRight ?? 0)));
        this.shaderUniforms.uExprWrinkles.value.set(browUp, browDown, smileSquint);
      }
    }
  }

  _head(g, dt) {
    const talking = !!this.speech, t = this.t;
    const amp = talking ? 0.75 : this.mood === "thinking" ? 0.5 : 0.3;   // calm idle: less sway = less tiring
    if (this.userTalking && t >= this.nextListenNod) {                // attentive listening nods
      this.sp.pitch.kick(0.5 + Math.random() * 0.3); this.nextListenNod = t + 1.8 + Math.random() * 2.5;
    }
    const tgt = {
      pitch: this.nz.pitch(t) * 0.035 * amp - g.y * 0.12 + (this.mood === "thinking" ? -0.04 : 0)
             + (this.emotion._pitch ?? 0),
      yaw: this.nz.yaw(t) * 0.05 * amp + g.x * 0.18,                   // head follows the eyes, partially
      roll: this.nz.roll(t) * 0.03 * amp + (this.userTalking ? 0.05 : 0) + (this.mood === "thinking" ? 0.04 : 0)
            + (this.emotion._roll ?? 0),
    };
    const clamp = (v, m) => Math.max(-m, Math.min(m, v));          // keep within a natural range of motion
    const p = clamp(this.sp.pitch.step(tgt.pitch, dt), 0.18), y = clamp(this.sp.yaw.step(tgt.yaw, dt), 0.25),
          r = clamp(this.sp.roll.step(tgt.roll, dt), 0.12);
    // split rotation between neck (40%) and head (60%) so it pivots naturally
    this._rot("Neck", p * 0.4, y * 0.4, r * 0.4);
    this._rot("Head", p * 0.6, y * 0.6, r * 0.6);
    const breath = Math.sin(t * (talking ? 1.9 : 1.4));
    this._rot("Spine2", breath * 0.01, this.nz.yaw(t * 0.5) * 0.01, 0);
  }

  _saccade(v) {
    const big = v.distanceTo(this.gazeTarget) > 0.2;
    this.gazeTarget.copy(v);
    this.nextSaccade = this.t + (big ? 0.8 + Math.random() * 1.5 : 0.4 + Math.random() * 1.2);
    if (big && Math.random() < 0.5) this._blink();                    // blinks often accompany big gaze shifts
  }
  _blink() {
    if (this.blinkT >= 0) return;
    this.blinkT = 0;
    this.nextBlink = this.t + (Math.random() < 0.12 ? 0.3 : 2 + Math.random() * 4);   // occasional double blink
  }
  _rot(name, x, y, z) {
    const b = this.bones[name]; if (!b) return;
    const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(x, y, z));
    if (b.frame) q.premultiply(b.frame.clone().invert()).multiply(b.frame);   // model-axis rotation in bone space
    b.bone.quaternion.copy(b.rest).multiply(q);
  }
}


// ---------------------------------------------------------------------------------------------------
// The 4th option: the same head, driven by the research-based behaviour model (web/behaviour.js, a port of
// the photoreal service's) and by the speech's prosody: blinks at natural intervals, eye contact while
// listening, a look-away when starting to speak and while thinking, nods and brow raises on pitch accents,
// backchannel nods when the candidate pauses, emotion baselines. CPU only; lip sync as above.
const DEG = Math.PI / 180;
export class LiveRig extends AvatarRig {
  constructor(root) {
    super(root);
    this.beh = new Behaviour();
    this.headGazeX = new Spring(3); this.headGazeY = new Spring(3);
    this._userTalking = false; this.firstOfTurn = true;
    // body life: breathing, posture (lean), slow weight shifts - all on the spine, the neck compensates
    this.breathPhase = Math.random() * 6.3; this.inhaleAt = -10; this.exhaleUntil = 0; this.speechEndAt = -10;
    this.lean = new Spring(3); this.twist = new Spring(1.6); this.tilt = new Spring(1.6);
    this.shiftTarget = { twist: 0, tilt: 0 }; this.nextShift = 4 + Math.random() * 6;
    this.thinkCycle = Math.random() * 6.3;
    this.face = {};                                  // mood faces come from the behaviour instead
  }
  setMood(m) {                                       // page moods: listening (waiting) | thinking | speaking
    this.mood = m;
    if (!this.beh) return;                           // called by the base constructor before ours runs
    if (m === "thinking") this.beh.setMode("thinking");
    else if (m === "listening" && !this._userTalking) { this.beh.setMode("idle"); this.firstOfTurn = true; }
  }
  get userTalking() { return this._userTalking; }
  set userTalking(on) {
    on = !!on;
    if (on && !this._userTalking) { this.beh.setMode("listening"); this.firstOfTurn = true; }
    else if (!on && this._userTalking) this.beh.candidatePaused();
    this._userTalking = on;
  }
  setEmotion() {}                                    // handled per sentence by the behaviour
  startSpeech(msg, clock) {
    super.startSpeech(msg, clock);
    let accents = [], rise = false;
    try { if (msg.buffer) ({ accents, rise } = prosody(msg.buffer.getChannelData(0), msg.buffer.sampleRate)); } catch {}
    const dur = msg.duration ?? msg.buffer?.duration ?? 2;
    const lead = Math.max(0, -(clock?.() ?? 0));
    this.speech.dur = dur;
    this.inhaleAt = this.t; this.exhaleUntil = this.t + 0.35 + lead + dur;   // breathe in as the sentence starts, out while speaking
    this.beh.startSentence({ duration: dur, accents, emotion: msg.emotion, lead,
                             isQuestion: /\?\s*$/.test(msg.text || "") || rise, firstOfTurn: this.firstOfTurn });
    this.firstOfTurn = false;
  }
  endSpeech() {
    const completed = !!this.speech && (this.speech.clock?.() ?? 0) >= (this.speech.dur ?? 1) - 0.25;
    if (this.speech) {
      this.exhaleUntil = Math.min(this.exhaleUntil, this.t + 0.25);
      this.speechEndAt = this.t;
    }
    this.speech = null;
    this.beh?.endSentence(completed);
  }
  _face(target, add, dt, env = 0) {
    if (!this.speech && this.beh.mode === "speaking") this.beh.setMode(this._userTalking ? "listening" : "idle");
    const c = this.beh.tick(dt);
    // brows: LivePortrait-style units (-20..24) -> ARKit, with non-linear soft-tissue activation & subtle asymmetry
    if (c.brow > 0) {
      const b = softTissue(c.brow / 24, 1.18);
      add("browInnerUp", b * 0.78);
      add("browOuterUpLeft", b * 0.56);
      add("browOuterUpRight", b * 0.52);
    } else if (c.brow < 0) {
      const b = softTissue(-c.brow / 16, 1.2);
      add("browDownLeft", b * 0.58);
      add("browDownRight", b * 0.55);
      add("eyeSquintLeft", b * 0.2);
      add("eyeSquintRight", b * 0.18);
    }
    // Duchenne smile coupling: orbicularis oculi cheek/eye squint + subtle dimples + upper-lip cheek lift
    if (c.smile > 0) {
      const sm = softTissue(c.smile, 1.15), sq = softTissue(c.smile, 1.35);
      add("mouthSmileLeft", sm * 0.58);
      add("mouthSmileRight", sm * 0.55);
      add("cheekSquintLeft", sq * 0.38);
      add("cheekSquintRight", sq * 0.36);
      add("eyeSquintLeft", sq * 0.18);
      add("eyeSquintRight", sq * 0.16);
      add("mouthDimpleLeft", sm * 0.15);
      add("mouthDimpleRight", sm * 0.14);
    } else if (c.smile < 0) {
      const pr = softTissue(-c.smile, 1.2);
      add("mouthPressLeft", pr * 0.55);
      add("mouthPressRight", pr * 0.52);
    }
    // Cognitive micro-expressions while thinking (subtle asymmetric lip-corner tension / contemplation)
    if (this.beh.mode === "thinking") {
      this.thinkCycle += dt * 1.35;
      const th = 0.5 + 0.5 * Math.sin(this.thinkCycle);
      add("eyeSquintLeft", 0.14 + 0.04 * th); add("eyeSquintRight", 0.13 + 0.04 * th);
      add("mouthPressLeft", 0.12 + 0.08 * th); add("mouthPressRight", 0.08 + 0.06 * th);
      add("mouthRollLower", 0.08 * th);
      add("mouthDimpleLeft", 0.09 * th);
    }
    // Attentive resting warmth (gently breathing micro-smile rather than a fixed static grin)
    if (this.beh.mode !== "thinking" && c.smile >= 0) {
      const restDrift = 0.025 * Math.sin(this.t * 0.43) + 0.015 * Math.cos(this.t * 0.71);
      const restSmile = this.speech ? 0.03 : 0.075 + restDrift;
      add("mouthSmileLeft", restSmile); add("mouthSmileRight", restSmile * 0.92);
      if (!this.speech) { add("cheekSquintLeft", 0.04); add("cheekSquintRight", 0.035); }
    }
    // Attentive listening micro-engagement while the candidate is speaking
    if (this.beh.mode === "listening") {
      add("cheekSquintLeft", 0.055); add("cheekSquintRight", 0.05);
      add("eyeSquintLeft", 0.025); add("eyeSquintRight", 0.025);
    }
    // Pre-speech preparatory breath intake (0..0.35 s after startSpeech): slight nostril flare + soft lip part
    const sinceInhale = this.t - this.inhaleAt;
    if (sinceInhale >= 0 && sinceInhale < 0.35) {
      const inh = Math.sin((sinceInhale / 0.35) * Math.PI);
      add("noseSneerLeft", inh * 0.14); add("noseSneerRight", inh * 0.14);
      if (env < 0.08) add("jawOpen", inh * 0.035);
    }
    // Post-speech lip settle (0..0.38 s after endSpeech): brief soft lip press/roll as the mouth closes
    const sinceEnd = this.t - this.speechEndAt;
    if (!this.speech && sinceEnd >= 0 && sinceEnd < 0.38) {
      const settle = Math.sin((sinceEnd / 0.38) * Math.PI);
      add("mouthRollLower", settle * 0.11);
      add("mouthPressLeft", settle * 0.09); add("mouthPressRight", settle * 0.09);
    }
    // Subtle cheek/nostril engagement during energetic speech
    if (env > 0.45) {
      const spEffort = (env - 0.45) * 0.22;
      add("cheekSquintLeft", spEffort); add("cheekSquintRight", spEffort);
      add("noseSneerLeft", spEffort * 0.45); add("noseSneerRight", spEffort * 0.45);
    }
    // gaze: behaviour units (about +-18) -> eyeLook weights (0..0.8), eyes then head
    const gx = Math.max(-0.8, Math.min(0.8, c.gaze_x / 18)), gy = Math.max(-0.6, Math.min(0.6, c.gaze_y / 16));
    this.gaze.set(gx, gy);
    // Damp vertical gaze morphs during blinks so they never pull against full eyelid closure,
    // and add slight near-field ocular convergence (vergence) when looking toward the camera.
    const blinkDamp = 1 - 0.65 * c.blink;
    const vergence = 0.024 * Math.max(0, 1 - Math.hypot(gx, gy));
    Object.assign(target, {
      eyeLookOutLeft: Math.max(0, -gx), eyeLookInRight: Math.max(0, -gx) + vergence,
      eyeLookInLeft: Math.max(0, gx) + vergence, eyeLookOutRight: Math.max(0, gx),
      eyeLookUpLeft: Math.max(0, gy) * blinkDamp, eyeLookUpRight: Math.max(0, gy) * blinkDamp,
      eyeLookDownLeft: Math.max(0, -gy) * blinkDamp, eyeLookDownRight: Math.max(0, -gy) * blinkDamp,
    });
    add("eyeBlinkLeft", c.blink); add("eyeBlinkRight", c.blink * 0.985); // tiny natural eyelid asymmetry
    add("eyeSquintLeft", c.blink * 0.16); add("eyeSquintRight", c.blink * 0.15); // lower lid rises slightly to meet upper lid
    add("cheekSquintLeft", c.blink * 0.06); add("cheekSquintRight", c.blink * 0.06); // subtle orbicularis oculi twitch on blink
    add("eyeBlinkLeft", Math.max(0, -gy) * 0.3); add("eyeBlinkRight", Math.max(0, -gy) * 0.3);
    // Lift the upper lids slightly with upward gaze or raised brows; fade this during a blink
    // so widening cannot compete with eyelid closure.
    const browLift = c.brow > 0 ? Math.min(1, c.brow / 24) * 0.1 : 0;
    const alertLid = Math.max(0, 1 + gy * 2) * 0.06;
    const lidLift = (alertLid + Math.max(0, gy) * 0.18 + browLift) * (1 - c.blink);
    add("eyeWideLeft", lidLift); add("eyeWideRight", lidLift);
    // head: degrees -> radians; the head follows the eyes partially, like the original rig
    // Eyes lead the head; the slower head response avoids a rigid locked-together turn.
    const p = c.pitch * DEG + this.headGazeY.step(gy, dt) * -0.12;
    const y = c.yaw * DEG + this.headGazeX.step(gx, dt) * 0.18, r = c.roll * DEG;
    // ---- body: breath b in 0..1 (1 = full inhale). Resting ~14 breaths/min; while speaking a quick inhale at each
    // sentence start (0.35 s), then a slow exhale over the sentence.
    const t = this.t;
    let b;
    if (t < this.exhaleUntil) {
      const since = t - this.inhaleAt;
      b = since < 0.35 ? since / 0.35 : Math.max(0, 1 - (since - 0.35) / Math.max(0.5, this.exhaleUntil - this.inhaleAt - 0.35));
      this.breathPhase = Math.asin(Math.min(1, Math.max(-1, 2 * b - 1)));    // resume the rest rhythm from here
    } else {
      this.breathPhase += dt * 2 * Math.PI * (this.beh.mode === "thinking" ? 0.2 : 0.23);
      b = 0.5 + 0.5 * Math.sin(this.breathPhase);
    }
    // posture: lean in while the candidate talks (engaged), sit back a little while thinking
    const mode = this.beh.mode;
    const lean = this.lean.step(mode === "listening" ? 0.035 : mode === "thinking" ? -0.02 : mode === "speaking" ? 0.012 : 0, dt);
    // slow weight shifts: a new small twist/tilt every 8-20 s (less often while listening: people hold still)
    if (t >= this.nextShift) {
      this.shiftTarget = { twist: (Math.random() - 0.5) * 0.05, tilt: (Math.random() - 0.5) * 0.03 };
      this.nextShift = t + (mode === "listening" ? 14 : 8) + Math.random() * 12;
    }
    const twist = this.twist.step(this.shiftTarget.twist, dt), tilt = this.tilt.step(this.shiftTarget.tilt, dt);
    const chest = -0.014 * b;                                            // chest opens up on the inhale
    const s2 = this.bones.Spine2;                                        // ...and the upper torso rises a few mm
    if (s2) { s2.restPos ??= s2.bone.position.clone(); s2.bone.position.copy(s2.restPos); s2.bone.position.y += 0.0035 * b / (s2.bone.parent?.getWorldScale(new THREE.Vector3()).y || 1); }
    this._rot("Spine", 0, twist, 0);
    this._rot("Spine1", lean, 0, tilt);
    this._rot("Spine2", chest, 0, 0);
    // the neck takes back most of the body motion so the head (and eye contact) stays steady
    this._rot("Neck", p * 0.4 - 0.8 * (lean + chest), y * 0.4 - 0.7 * twist, r * 0.4 - 0.8 * tilt);
    this._rot("Head", p * 0.6, y * 0.6, r * 0.6);
  }
}
