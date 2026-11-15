// In-browser engine: the whole backend (VAD -> Whisper -> LLM -> Kokoro) running on the
// visitor's GPU via WebGPU, so the page works from static hosting (GitHub Pages).
//
// It speaks the same message protocol as backend/server.py (state / vad / stop / say /
// transcript / error), so index.html and avatar.js don't care which engine is in use.
// Differences from the server: smaller LLM, and visemes are estimated (Kokoro's phonemes spread
// over the voiced audio) instead of exact durations.
//
// Libraries are pinned to exact versions (>= 2 weeks old) on jsDelivr.
import { speakable } from "./speech-text.js";
import { NOTE_MARK, makeAckTrimmer, dropRejectionLuck, rememberQuestions, coveredNote, designNote, designFollowNote, dedupeQuestion, diagnoseAnswer, isClarifyingQuestion } from "./persona.js";

const CDN = {
  webllm: "https://cdn.jsdelivr.net/npm/@mlc-ai/web-llm@0.2.86/+esm",
  // +esm: jsDelivr rewrites its bare onnxruntime-web/webgpu import to the exact pinned build
  transformers: "https://cdn.jsdelivr.net/npm/@huggingface/transformers@4.3.1/+esm",
  kokoro: "https://cdn.jsdelivr.net/npm/kokoro-js@1.2.1/dist/kokoro.web.js",
  ort: "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.30.0/dist/ort.wasm.bundle.min.mjs",
  silero: "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.31/dist/silero_vad_v5.onnx",
  wav2arkit: "https://huggingface.co/myned-ai/wav2arkit_cpu/resolve/main/",
};
export const BROWSER_DEFAULTS = {
  // 1.5B, not 3B: on AMD/Linux (RADV) the 3B models' prompt prefill runs long enough per GPU
  // dispatch to trip the driver watchdog ("Device was lost") once the persona prompt is ~700+
  // tokens. 1.5B handles a full JD + resume prompt. Override with ?llm=<WebLLM model id>.
  llm: "Qwen2.5-1.5B-Instruct-q4f16_1-MLC",
  whisper: "onnx-community/whisper-base.en",
  voice: "af_heart",
};

const EMOTIONS = ["neutral", "warm", "curious", "impressed", "skeptical", "thinking", "encouraging", "amused"];
const persona = (role) => `You are Claire, a friendly but rigorous job interviewer on a video call.
Interview the candidate for the role: ${role}.
Rules:
- Speak naturally, as spoken dialogue. No markdown, lists, emojis or stage directions.
- Keep each turn short: one or two sentences, then ask exactly one question.
- Ask follow-ups that dig into specifics the candidate mentioned.
- Start by greeting the candidate and asking them to introduce themselves.
- Begin EVERY sentence with your facial expression tag in square brackets, chosen from:
  ${EMOTIONS.join(", ")}. Example: "[warm] Nice to meet you. [curious] What drew you to backend work?"`;

// ---------------------------------------------------------------- visemes (IPA -> Oculus)
const IPA = {};
for (const [chars, v] of [["pbm", "PP"], ["fv", "FF"], ["θð", "TH"], ["tdɾʔ", "DD"], ["kgŋx", "kk"],
  ["ʃʒʧʤç", "CH"], ["sz", "SS"], ["nlɫ", "nn"], ["ɹrɚɝ", "RR"], ["aɑɐʌæ", "aa"], ["ɛeəɜᵊ", "E"],
  ["iɪjᵻ", "I"], ["ɔoɒ", "O"], ["uʊw", "U"]]) for (const c of chars) IPA[c] = v;
const VOWELS = new Set(["aa", "E", "I", "O", "U"]);
const VISEME_DUR = {
  aa: 1.5, O: 1.45, E: 1.35, I: 1.2, U: 1.2, RR: 1.1,
  SS: 0.95, CH: 0.95, FF: 0.9, TH: 0.9, nn: 0.9,
  PP: 0.78, DD: 0.75, kk: 0.75,
};
// trailing words that mean the candidate hasn't finished their sentence (same list as backend/server.py)
const UNFINISHED = new Set("and but so because or um uh erm like the a an to of with that which then if when i we my our is was were for in on at by as also basically actually".split(" "));

function envelope(x, sr, fps = 50) {
  const hop = Math.floor(sr / fps), n = Math.floor(x.length / hop), rms = new Float32Array(n);
  for (let i = 0; i < n; i++) { let s = 0; for (let j = i * hop; j < (i + 1) * hop; j++) s += x[j] * x[j]; rms[i] = Math.sqrt(s / hop); }
  const sorted = Float32Array.from(rms).sort(), ref = sorted[Math.floor(n * 0.98)] || 1;
  return Array.from(rms, (r) => Math.round(Math.sqrt(Math.min(1, r / ref)) * 100) / 100);
}

// Spread the phonemes over the voiced parts of the audio (weighted by intrinsic phoneme duration, pauses silent).
function estimateCues(phonemes, env, fps) {
  const units = [];
  for (const p of phonemes) {
    if (",.;:!?—…".includes(p)) units.push(["sil", 1.5]);
    else if (IPA[p]) units.push([IPA[p], VISEME_DUR[IPA[p]] ?? (VOWELS.has(IPA[p]) ? 1.4 : 1.0)]);
  }
  const voiced = []; let start = -1;
  env.forEach((e, i) => { if (e > 0.18 && start < 0) start = i; if (e <= 0.18 && start >= 0) { if (i - start > 1) voiced.push([start, i]); start = -1; } });
  if (start >= 0) voiced.push([start, env.length]);
  const speech = units.filter(([o]) => o !== "sil");
  if (!speech.length || !voiced.length) return [{ t: 0, o: "sil" }];
  const total = voiced.reduce((s, [a, b]) => s + b - a, 0), wsum = speech.reduce((s, [, w]) => s + w, 0);
  const toReal = (x) => { for (const [a, b] of voiced) { if (x < b - a) return [(a + x) / fps, a]; x -= b - a; } const [a, b] = voiced.at(-1); return [b / fps, a]; };
  const cues = [{ t: 0, o: "sil" }]; let acc = 0, seg = voiced[0][0];
  for (const [o, w] of speech) {
    const [t, s] = toReal(total * acc / wsum);
    if (s !== seg) { const prev = voiced.find(([a]) => a === seg); cues.push({ t: prev[1] / fps, o: "sil" }); seg = s; }
    if (cues.at(-1).o !== o) cues.push({ t: Math.round(t * 1000) / 1000, o });
    acc += w;
  }
  cues.push({ t: voiced.at(-1)[1] / fps, o: "sil" });
  return cues;
}

// ---------------------------------------------------------------- turn taking (port of turns.py)
class TurnDetector {
  constructor(silero) {
    this.silero = silero; this.avatarSpeaking = false; this.buf = new Float32Array(0);
    this.FRAME = 512; const f = (s) => Math.round(s * 16000 / 512);
    this.endFrames = f(1.2); this.pauseFrames = f(0.3); this.minSpeech = f(0.25); this.preroll = f(0.3);
    this.reset();
  }
  reset() { this.inSpeech = false; this.frames = []; this.history = []; this.runOn = 0; this.silence = 0; this.speechFrames = 0; this.pauseSent = false; }
  async *feed(int16) {
    const x = new Float32Array(this.buf.length + int16.length); x.set(this.buf);
    for (let i = 0; i < int16.length; i++) x[this.buf.length + i] = int16[i] / 32768;
    let off = 0;
    for (; off + this.FRAME <= x.length; off += this.FRAME) yield* this.frame(x.slice(off, off + this.FRAME));
    this.buf = x.slice(off);
  }
  async *frame(fr) {
    const p = await this.silero(fr);
    const [thresh, startFrames] = this.avatarSpeaking ? [0.8, 8] : [0.5, 3];
    if (!this.inSpeech) {
      this.history.push(fr); if (this.history.length > this.preroll) this.history.shift();
      this.runOn = p >= thresh ? this.runOn + 1 : 0;
      if (this.runOn >= startFrames) { this.inSpeech = true; this.frames = [...this.history]; this.speechFrames = this.runOn; this.silence = 0; yield ["speech_start"]; }
      return;
    }
    this.frames.push(fr);
    if (p >= 0.35) { this.speechFrames++; this.silence = 0; this.pauseSent = false; return; }
    this.silence++;
    if (this.silence === this.pauseFrames && !this.pauseSent) { this.pauseSent = true; yield ["pause", concat(this.frames)]; }
    // adaptive patience (same as backend/turns.py): longer answers get longer mid-thought pauses, up to +0.8 s
    if (this.silence >= this.endFrames + Math.min(25, Math.floor(this.speechFrames / 12))) {
      const pcm = concat(this.frames), enough = this.speechFrames >= this.minSpeech;
      this.reset(); this.silero.reset();
      yield enough ? ["turn_end", pcm] : ["discard"];
    }
  }
}
const concat = (arrs) => { const n = arrs.reduce((s, a) => s + a.length, 0), o = new Float32Array(n); let k = 0; for (const a of arrs) { o.set(a, k); k += a.length; } return o; };

async function loadSilero(ort) {
  const session = await ort.InferenceSession.create(await (await fetch(CDN.silero)).arrayBuffer());
  const sr = new ort.Tensor("int64", [16000n]);
  let state, ctx;
  const fn = async (frame) => {
    const x = new Float32Array(64 + frame.length); x.set(ctx); x.set(frame, 64); ctx = frame.slice(-64);
    const out = await session.run({ input: new ort.Tensor("float32", x, [1, x.length]), state, sr });
    state = out.stateN; return out.output.data[0];
  };
  fn.reset = () => { state = new ort.Tensor("float32", new Float32Array(256), [2, 1, 128]); ctx = new Float32Array(64); };
  fn.reset();
  return fn;
}

// Optional client-side neural audio-to-ARKit (52 blendshapes @ 30 fps) via onnxruntime-web + Cache API
let browserArkitPromise = null;
async function loadBrowserArkit(ort) {
  if (browserArkitPromise) return browserArkitPromise;
  return (browserArkitPromise = (async () => {
    const fetchCached = async (url) => {
      if ("caches" in self) {
        const cache = await caches.open("wav2arkit-cache");
        let res = await cache.match(url);
        if (!res) { res = await fetch(url); if (res.ok) await cache.put(url, res.clone()); }
        return res;
      }
      return fetch(url);
    };
    const [cfgRes, onnxRes, dataRes] = await Promise.all([
      fetchCached(CDN.wav2arkit + "config.json"),
      fetchCached(CDN.wav2arkit + "wav2arkit_cpu.onnx"),
      fetchCached(CDN.wav2arkit + "wav2arkit_cpu.onnx.data"),
    ]);
    const cfg = await cfgRes.json();
    const session = await ort.InferenceSession.create(await onnxRes.arrayBuffer(), {
      executionProviders: ["wasm"],
      externalData: [{ path: "wav2arkit_cpu.onnx.data", data: new Uint8Array(await dataRes.arrayBuffer()) }],
    });
    return {
      names: cfg.blendshape_names,
      fps: cfg.output_fps || 30,
      async infer(pcm, sr) {
        const ratio = sr / 16000, n16 = Math.floor(pcm.length / ratio), x = new Float32Array(n16);
        for (let i = 0; i < n16; i++) {
          const p = i * ratio, i0 = Math.floor(p), a = p - i0;
          x[i] = pcm[i0] * (1 - a) + (pcm[Math.min(pcm.length - 1, i0 + 1)] ?? 0) * a;
        }
        const out = await session.run({ audio_waveform: new ort.Tensor("float32", x, [1, x.length]) });
        const raw = out.blendshapes.data, frames = Math.floor(raw.length / 52), res = [];
        for (let f = 0; f < frames; f++) {
          const row = new Array(52);
          for (let k = 0; k < 52; k++) row[k] = Math.round(Math.max(0, Math.min(1, raw[f * 52 + k])) * 1000) / 1000;
          res.push(row);
        }
        return res;
      },
    };
  })().catch((e) => { console.warn("Browser wav2arkit unavailable, using enhanced viseme FACS:", e); return null; }));
}

// ---------------------------------------------------------------- engine
// WebLLM's default 4096-token window overflows after a couple of turns (system prompt ~3k tokens); 8k, not more:
// every extra token of context is GPU memory (KV cache) on machines that may have little to spare
const LLM_OPTS = { context_window_size: 8192 };

export class BrowserEngine {
  constructor({ llm, whisper, voice, role = "Software Engineer", onProgress = () => {} } = {}) {
    this.cfg = { llm: llm || BROWSER_DEFAULTS.llm, whisper: whisper || BROWSER_DEFAULTS.whisper, voice: voice || BROWSER_DEFAULTS.voice, role };
    this.onProgress = onProgress;
    this.onmessage = () => {};
    this.history = [{ role: "system", content: persona(role) }];
    this.gen = 0; this.ready = null; this.spec = null; this.queue = Promise.resolve();
  }
  // navigator.gpu existing isn't enough: on Linux Chrome WebGPU is off by default, so requestAdapter()
  // returns null and WebLLM fails with "Unable to find a compatible GPU". Ask for a real adapter.
  static async gpuStatus() {
    if (!navigator.gpu) return { ok: false, reason: "no-api" };
    try {
      // requestAdapter() can hang forever on some Linux driver/flag combinations - time-box it
      const a = await Promise.race([navigator.gpu.requestAdapter({ powerPreference: "high-performance" }),
        new Promise((_, rej) => setTimeout(() => rej(new Error("the GPU did not respond within 8 s")), 8000))]);
      if (!a) return { ok: false, reason: "no-adapter" };
      return { ok: true, f16: a.features.has("shader-f16"), info: a.info };
    } catch (e) { return { ok: false, reason: "error", error: String(e?.message || e) }; }
  }
  emit(m) { this.onmessage(m); }

  /** Are the models already in this origin's Cache Storage? (cache is per address: localhost != 127.0.0.1) */
  async cacheStatus() {
    try {
      if (!("caches" in self)) return { available: false };
      const webllm = await import(CDN.webllm);
      const llm = await webllm.hasModelInCache(this.cfg.llm);
      const tf = await caches.has("transformers-cache") && (await (await caches.open("transformers-cache")).keys()).length >= 8;
      return { available: true, llm, rest: tf, all: llm && tf };
    } catch { return { available: true, all: false }; }
  }

  /** Downloads/compiles all models (cached by the browser after the first visit). */
  load() {
    this.ready ??= (async () => {
      // ask the browser to keep our ~1.5 GB as persistent (not best-effort, evictable) storage
      try { this.persisted = await navigator.storage?.persist?.(); } catch {}
      const cached = this.cached ?? (await this.cacheStatus()).all;
      const prog = {};
      const report = (k, f, label) => { prog[k] = [f, label]; const items = Object.values(prog);
        this.onProgress(items.reduce((s, [x]) => s + x, 0) / 3, items.map(([, l]) => l).join(" · ")); };
      this.onProgress(0, "downloading libraries (WebLLM, transformers.js, Kokoro)…");
      const [webllm, tf, kokoro, ort] = this.mods = await Promise.all([import(CDN.webllm), import(CDN.transformers), import(CDN.kokoro), import(CDN.ort)]);
      this.kokoro = kokoro;
      ort.env.wasm.wasmPaths = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.30.0/dist/";
      // the HF CDN often omits content-length -> no total: show MB downloaded instead of a frozen 0%
      const fileProgress = (k, label) => (p) => {
        if (p.status === "progress") {
          if (p.total) report(k, p.loaded / p.total, `${label} ${cached ? "from cache" : "downloading"} ${Math.round(100 * p.loaded / p.total)}% (${(p.loaded / 2 ** 20).toFixed(0)} MB)`);
          else report(k, 0.5, `${label} ${(p.loaded / 2 ** 20).toFixed(0)} MB ${cached ? "read" : "downloaded"}`);
        } else if (p.status === "initiate" || p.status === "download") report(k, prog[k]?.[0] ?? 0, `${label}: fetching ${p.file || ""}`);
        else if (p.status === "ready" || p.status === "done") report(k, prog[k]?.[0] ?? 0, `${label}: preparing…`);   // one file done != stage done
      };
      // Load ONE model at a time. Loading all three WebGPU models concurrently (each library makes
      // its own GPU device, compiles shaders and allocates GBs at once) made the GPU drop the LLM's
      // device ("Device was lost") and slowed Whisper to ~37 s per turn. Sequential: LLM ~2 s
      // (cached), Whisper ~0.2 s/run, Kokoro ~0.2 s/sentence.
      this.webllm = webllm;
      this.llm = await webllm.CreateMLCEngine(this.cfg.llm, { initProgressCallback: (r) => report("llm", r.progress, `LLM: ${String(r.text || "").replace(/\s*It can take.*$/s, "").replace(/\. ?$/, "").slice(0, 110)}`) },
        LLM_OPTS);   // WebLLM's default 4096 overflows after a couple of turns (system prompt ~3k tokens)
      report("llm", 1, "LLM ready");
      this.stt = await tf.pipeline("automatic-speech-recognition", this.cfg.whisper, { device: "webgpu", dtype: "fp32", progress_callback: fileProgress("stt", "Whisper") });
      report("stt", 1, "Whisper ready");
      // Kokoro on CPU (WASM), not WebGPU: its WebGPU path gives muffled, ~1/6-volume audio (fp32) or NaN
      // (fp16/q4) on some GPUs/drivers. Multi-threaded WASM (needs cross-origin isolation) runs ~0.55x real time.
      this.tts = await kokoro.KokoroTTS.from_pretrained("onnx-community/Kokoro-82M-v1.0-ONNX", { dtype: "fp32", device: "wasm", progress_callback: fileProgress("tts", "Voice") });
      report("tts", 1, "Voice ready");
      this.silero = await loadSilero(ort);   // tiny, CPU (wasm)
      this.ort = ort;
      if (new URLSearchParams(location.search).get("arkit") === "1") this.arkit = await loadBrowserArkit(ort);
      this.turns = new TurnDetector(this.silero);
      if (this.endSilence) this.turns.endFrames = Math.round(this.endSilence * 16000 / 512);
      this.onProgress(1, "Models ready");
    })();
    return this.ready;
  }

  // --- same API as the WebSocket transport -------------------------------------
  send(msg) {
    if (msg.type === "config") {     // interview setup from the page (same message the server takes)
      if (msg.system) this.history = [{ role: "system", content: String(msg.system).slice(0, 24000) }];
      if (msg.voice && /^[a-z]{2}_[a-z]+$/.test(msg.voice)) this.cfg.voice = msg.voice;
      if (Array.isArray(msg.questions)) this.pacer = { ...(this.pacer || {}), pending: msg.questions.slice(0, 12).map(String), since: 0, answers: 0, cues: this.pacer?.cues || new Set(), asked: this.pacer?.asked || [] };
      if (msg.kind && this.pacer) this.pacer.kind = String(msg.kind);
      this.noResume = !!msg.no_resume;
      if (msg.minutes && this.pacer) this.pacer.minutes = +msg.minutes;
      const s = { quick: 0.7, normal: 1.2, patient: 2.0 }[msg.patience];
      if (s) { this.endSilence = s; if (this.turns) this.turns.endFrames = Math.round(s * 16000 / 512); }
      return;
    }
    if (msg.type === "end") { this.gen++; this.llm?.interruptGenerate(); return; }
    if (msg.type === "speak") return this.speakAs(String(msg.text || ""), msg.voice, msg.role === "interviewer" ? "interviewer" : "candidate");
    if (msg.type === "nudge") { if (!this.busy) return this.turn(String(msg.text || "(silence)")); return; }
    if (msg.type === "complete") return this.complete(msg);
    if (msg.type === "playback") { if (this.turns) this.turns.avatarSpeaking = !!msg.playing; return; }
    if (msg.type === "reset") { this.gen++; this.history = this.history.slice(0, 1); this.recentAcks = []; return; }
    if (msg.type === "start") { if (this.pacer) { this.pacer.t0 = performance.now(); this.pacer.cues = new Set(); this.pacer.asked = []; } return this.turn(null); }
    if (msg.type === "text" && msg.text?.trim()) { this.emit({ type: "transcript", role: "user", text: msg.text.trim() }); return this.turn(msg.text.trim()); }
  }
  sendAudio(buffer) {   // 16 kHz int16 PCM from the open mic
    if (!this.turns) return;
    const pcm = new Int16Array(buffer);
    this.queue = this.queue.then(async () => {        // keep frames in order (VAD is async)
      for await (const [ev, data] of this.turns.feed(pcm)) {
        if (ev === "speech_start") {
          this.gen++; this.llm.interruptGenerate(); this.emit({ type: "stop" }); this.emit({ type: "vad", state: "speech" }); this.spec = null;
          clearTimeout(this.holdTimer);                     // they carried on after an unfinished-sounding pause
          // "I wasn't done": talking again within 4 s of their turn ending -> merge with that answer
          if (this.lastTurn && performance.now() - this.lastTurn.t < 4000 && !this.held) {
            this.held = this.lastTurn.text; this.mergePending = true; this.emit({ type: "merge" });
          }
          this.lastTurn = null;
        }
        else if (ev === "pause") this.spec = this.transcribe(data);
        else if (ev === "turn_end") { const spec = this.spec; this.spec = null; this.finishTurn(data, spec); }
        else if (ev === "discard") this.emit({ type: "vad", state: "end" });
      }
    });
  }

  // WebGPU sessions (onnxruntime-web under Whisper, and WebLLM) must never run two calls at once: an overlapping
  // run unmaps the other's readback buffer -> "Failed to execute 'mapAsync' on 'GPUBuffer': Buffer was unmapped
  // before mapping was resolved". A pause starts a speculative transcription without waiting, so queue them.
  transcribe(pcm) {
    const run = (this.sttQ || Promise.resolve()).then(() => this.stt(pcm)).then((r) => (r.text || "").trim());
    this.sttQ = run.catch(() => {});
    return run;
  }
  // one LLM generation at a time (a reply that is still winding down after "end" vs the feedback request)
  llmLock(fn) {
    const run = (this.llmQ || Promise.resolve()).then(fn);
    this.llmQ = run.catch(() => {});
    return run;
  }
  async finishTurn(pcm, spec) {
    let text = spec ? await spec : await this.transcribe(pcm);
    // whisper emits "[BLANK_AUDIO]" and hallucinates "Thank you." / "you" on noise or silence: not an answer
    if (/^\[.*\]$|^\(.*\)$/.test(text) || /^(thank(s| you)( (so much|very much|for watching|for listening))?|you|bye|okay|ok|so|um+|uh+|hmm+|yeah|oh|\.+|you\.)[.!?, ]*$/i.test(text.trim())) text = "";
    text = `${this.held || ""} ${text}`.trim(); this.held = "";
    // "...and then we, um" / "...because" -> probably still thinking: keep listening 1.5 s more
    const last = text.toLowerCase().replace(/[.!?]+$/, "").split(/\s+/).pop() || "";
    if (text && (/[,…\-—]$/.test(text) || UNFINISHED.has(last))) {
      this.held = text;
      this.holdTimer = setTimeout(() => { const t = this.held; this.held = ""; this.commitTurn(t); }, 1500);
      return;
    }
    this.commitTurn(text);
  }
  async commitTurn(text) {
    this.emit({ type: "vad", state: "end" });
    this.emit({ type: "transcript", role: "user", text });
    if (!text) { this.emit({ type: "state", state: "listening" }); return; }
    if (this.mergePending) {                            // drop the reply they talked over (and their first half)
      this.mergePending = false;
      for (let i = this.history.length - 1; i > 0; i--) if (this.history[i].role === "user") { this.history.splice(i); break; }
    }
    this.lastTurn = { text, t: performance.now() };
    await this.turn(text);
  }

  // If the GPU dropped the LLM ("Device was lost" -> "Model not loaded"), reload it once and retry.
  async withLLM(fn) {
    try { return await fn(); } catch (e) {
      if (!/not loaded|device (was )?lost|GPUDevice|already been disposed|mapAsync|unmapped/i.test(String(e?.message || e))) throw e;
      this.onProgress(0.9, "GPU reset — reloading the language model…");
      await this.llm.reload(this.cfg.llm, LLM_OPTS);      // keep the larger context window
      this.onProgress(1, "Models ready");
      return await fn();
    }
  }

  // AI demo: voice the AI candidate's answer sentence by sentence in another voice
  async speakAs(text, voice, role = "candidate") {
    await this.load();
    const prev = this.cfg.voice; if (voice && /^[a-z]{2}_[a-z]+$/.test(voice)) this.cfg.voice = voice;
    try {
      const parts = text.split(/(?<=[.!?])\s+/).filter((x) => x.trim());
      for (let i = 0; i < parts.length; i++) this.emit({ ...(await this.synthesize(parts[i], "warm")), role, last: i === parts.length - 1 });
    } finally { this.cfg.voice = prev; }
  }

  async complete({ id, prompt, json = true }) {
    await this.load();
    try {
      const r = await this.llmLock(() => this.withLLM(() => this.llm.chat.completions.create({ messages: [{ role: "user", content: String(prompt).slice(0, 24000) }],
        temperature: 0.2, max_tokens: 1200 })));   // no response_format: WebLLM 0.2.86's json_object grammar fails to initialise; the page parses + retries
      this.emit({ type: "completion", id, text: r.choices[0].message.content });
    } catch (e) { this.emit({ type: "completion", id, error: String(e?.message || e) }); }
  }

  // same pacing as backend/pacing.py: after 3 answers without a planned question, steer to the next one
  // same as backend/pacing.py: time cues (last topic ~75%, their questions ~90%, close at 100%) + question pacing
  timeNote() {
    const p = this.pacer; if (!p?.t0) return null;
    const used = (performance.now() - p.t0) / 60000, mins = p.minutes || 10, left = Math.max(0, Math.round(mins - used)), frac = used / Math.max(1, mins);
    if (frac >= 1) return "the scheduled time is up. Close the interview now: if the candidate's last message is a question you have not answered yet, first answer it in 1-2 concrete sentences (never say you would love to answer but time is up); then thank the candidate, say what happens next, say goodbye, and end your last sentence with [end]. Do not ask any more questions.";
    if (frac >= 0.85 && !p.cues.has("questions")) { p.cues.add("questions"); return `about ${left} minute(s) left. Do NOT ask another interview question. Say one short acknowledgement, then ask exactly this: "What questions do you have for me?"`; }
    if (frac >= 0.75 && !p.cues.has("last")) { p.cues.add("last"); return `about ${left} minutes left: make this your last topic, then leave time for their questions.`; }
    return null;
  }
  steer(text) {
    const p = this.pacer; if (!p || !text || text.startsWith("(silence")) return text;
    const clarifying = isClarifyingQuestion(text);
    if (!clarifying) { p.answers++; p.since++; }
    const note = (x) => `${text}\n\n(Interviewer note, not said by the candidate: ${x})`;
    const cov = coveredNote(p.asked || []);
    const t = this.timeNote();
    if (t && /time is up|minute\(s\) left/.test(t)) return note(t);
    if (p.cues.has("closing")) return note(t && /time is up/.test(t) ? t : "you are in the closing Q&A. If the candidate asked a question, answer it in 2-3 concrete sentences and ask if they have anything else. If they say they have no more questions (or after their second question), close now: thank them, say what happens next, say goodbye, and end your last sentence with [end]. Never ask a new interview question now, and never answer a question they already asked earlier.");
    const diag = diagnoseAnswer(text, p.kind || "mixed");
    if (clarifying && diag) return note(([t, diag].filter(Boolean).join(" ") + cov).trim());
    const fu = clarifying ? null : designFollowNote(p.asked || [], p.cues);
    if (fu) { p.since = 0; return note(([t, fu].filter(Boolean).join(" ") + cov).trim()); }
    const frac = p.t0 ? (performance.now() - p.t0) / 60000 / Math.max(1, p.minutes || 10) : 0;
    const d = p.t0 ? designNote(p.kind || "mixed", frac, p.asked || [], p.cues) : null;
    if (d) { p.since = 0; return note(d + cov); }
    if (!p.pending.length || p.answers < 2 || p.since < 3 || p.cues.has("last")) {
      const msg = ([t, diag].filter(Boolean).join(" ") + cov).trim();
      return msg ? note(msg) : text;
    }
    p.since = 0;
    return note(`${t ? t + " " : ""}you've spent enough time on this topic. Move directly to this new question (bridge briefly from the candidate's context or ask it straight — avoid stock transitions like 'switching gears' or 'moving on', and never mention any list or note): "${p.pending[0]}"${cov}`);
  }
  noteReply(reply) {
    const p = this.pacer; if (!p) return;
    const STOP = new Set("a an the and or but to of in on at for with what how why when where who you your me my i we us our is are was were be been do did does have has had that this it its as by from about tell time describe would could should if then so can will just".split(" "));
    const words = (t) => new Set((String(t).toLowerCase().match(/[a-z']+/g) || []).filter((w) => w.length > 2 && !STOP.has(w)));
    rememberQuestions(p.asked ||= [], reply);
    if (/\b(questions? (do|would|might) you have|what would you like to ask|anything (else )?you'?d like to (ask|know))\b/i.test(reply)) p.cues.add("closing");
    const rw = words(reply);
    p.pending = p.pending.filter((q) => { const qw = words(q); const hit = [...qw].filter((w) => rw.has(w)).length;
      const asked = qw.size && hit / qw.size >= 0.4; if (asked) p.since = 0; return !asked; });
  }

  async turn(userText) {
    await this.load();
    userText = this.steer(userText);
    this.busy = true;
    try { return await this._turn(userText); } finally { this.busy = false; }
  }
  async _turn(userText) {
    const my = ++this.gen, cancelled = () => this.gen !== my;
    this.emit({ type: "state", state: "thinking" });
    this.history.push({ role: "user", content: userText ?? "(The candidate has joined the call.)" });
    const spoken = []; let emotion = "neutral", buf = "", first = true;
    const trim = makeAckTrimmer(userText, this.recentAcks ||= []);
    let ttsChain = Promise.resolve();
    let ended = false, asked1 = false, inNote = false;
    const speak = (raw) => {
      raw = raw.replace(/\s*\([^()]{30,}\)/g, "");     // models leak instructions as "(If they ask questions, ...)" - never spoken
      // small models leak "[Interviewer note: ...]" and wrap whole sentences in brackets: drop notes, unwrap the rest
      if (inNote) { const end = raw.indexOf("]"); if (end < 0) return; raw = raw.slice(end + 1); inNote = false; }
      raw = raw.replace(/\s*\[[^\]]*\b(note|instructions?|probe)\b[^\]]*\]/gi, "");
      const open = raw.search(/\[[^\]]*\b(note|instructions?|probe)\b[^\]]*$/i);
      if (open >= 0) { raw = raw.slice(0, open); inNote = true; }      // a note that runs on into the next sentences
      raw = raw.replace(/\[([^\]]{15,})\]?/g, "$1").replace(/^([^\[]*)\]/, "$1");
      if (asked1) return;     // the turn ends at its question: small models stack questions or answer for the candidate
      if (/\[end\]\W*$/i.test(raw.trim())) ended = !/\?\s*$/.test(raw.replace(/\[end\]/gi, "").trim());   // hang up after this sentence, unless it's a question
      const tags = [...raw.matchAll(/\[(\w+)\]/g)].map((m) => m[1].toLowerCase()).filter((t) => EMOTIONS.includes(t));
      if (tags.length) emotion = tags[0];
      const text = raw.replace(/\[(\w+)\]/g, "")
        // small models leave template placeholders ("Hello [Candidate's Name]!") - never speak them
        .replace(/\s*\[[^\]]{0,40}\b(name|candidate|company|role|position|interviewer)\b[^\]]{0,40}\]/gi, "")
        .replace(/\s+([!?.,])/g, "$1").replace(/,+(?=[.!?,])/g, "").replace(/\b(to|at|with|for)([.!?])/gi, "$2").replace(/\s+([!?.,])/g, "$1").replace(/[*_#`]+/g, "").replace(/\s{2,}/g, " ").trim();
      const noRes = (t) => { t = t.replace(/\b(and\s+)?I('ve| have)?\s*(had|have|took|got)?\s*(a|the)?\s*(chance|time|opportunity|look)?\s*(to\s+)?(look(ed)?|review(ed)?|read|go(ne)?|went)\s*(at|over|through)?\s*your (resume|cv|background|profile|application)\b[^,.!?]*[,]?\s*(and\s+)?/gi, "").replace(/\b(on|from|in|per|based on|looking at|reviewing|according to) your (resume|cv|background|profile)\b,?\s*/gi, "").replace(/\byour (resume|cv)\b/gi, "your experience").replace(/\s*[;,]\s*(?=[.!?])/g, "").replace(/\s+(and|but|so)\s*(?=[.!?])/g, "").replace(/([.!?])(\s*[.!?])+/g, "$1").trim(); return t ? t[0].toUpperCase() + t.slice(1) : t; };
      let text2 = trim(dropRejectionLuck(this.noResume ? noRes(text) : text).trim());
      if (!text2) return;
      if (this.pacer && !String(userText || "").startsWith("(silence")) text2 = dedupeQuestion(this.pacer.asked ||= [], this.pacer.pending || [], text2);
      if (/\?\s*$/.test(text2)) asked1 = true;
      spoken.push(`[${emotion}] ${text2}`);
      const emo = emotion;
      ttsChain = ttsChain.then(async () => {                 // synthesize in order, while the LLM keeps going
        if (cancelled()) return;
        const msg = await this.synthesize(text2, emo);
        if (cancelled()) return;
        if (first) { this.emit({ type: "state", state: "speaking" }); first = false; }
        this.emit(msg);
      });
    };
    try {
      // pacing notes are kept on the latest candidate turn only (as on the server)
      const lastUser = this.history.map((m) => m.role).lastIndexOf("user");
      const messages = this.history.map((m, i) => (m.role === "user" && i !== lastUser ? { ...m, content: m.content.split(NOTE_MARK)[0] } : m));
      // stay inside the context window: drop the oldest exchanges (never the system prompt or the latest turn)
      const size = () => messages.reduce((n, m) => n + m.content.length, 0);
      while (size() > 7000 * 3 && messages.length > 3) messages.splice(1, 2);
      const generate = () => this.llmLock(async () => {        // held until the stream is fully drained
        if (cancelled()) return;
        const stream = await this.withLLM(() => this.llm.chat.completions.create({ messages, stream: true, temperature: 0.7, max_tokens: 200,
          stop: ["\nYou:", "\nCandidate:", "\nSam:", "Candidate:"] }));
        for await (const chunk of stream) {
          if (cancelled()) { this.llm.interruptGenerate(); break; }   // barge-in: stop the GPU work, not just our reading
          if (asked1) continue;        // past the question: drain and discard (interrupting leaves a GPU map pending)
          buf += chunk.choices[0]?.delta?.content || "";
          const parts = buf.split(/(?<=[.!?])\s+/);
          buf = parts.pop();
          parts.forEach(speak);
        }
      });
      try { await generate(); }
      catch (e) {   // a barge-in interrupt can leave a stale GPU buffer map that fails the next generation: retry once
        if (!/mapAsync|unmapped/i.test(String(e?.message || e)) || spoken.length || cancelled()) throw e;
        buf = ""; await generate();
      }
      if (!cancelled() && !asked1) speak(buf);
      await ttsChain;
    } catch (e) {
      // interrupting a WebGPU generation (barge-in, or ending the turn at its question) can reject a pending
      // buffer map: harmless, the reply so far is kept
      const msg = String(e?.message || e);
      if (!(/mapAsync|unmapped/i.test(msg) && (cancelled() || asked1 || spoken.length))) this.emit({ type: "error", message: msg });
    }
    this.history.push({ role: "assistant", content: spoken.join(" ") + (cancelled() ? " — (interrupted by the candidate)" : "") });
    this.noteReply(spoken.join(" "));
    this.emit({ type: "transcript", role: "assistant", text: spoken.map((s) => s.replace(/^\[\w+\]\s*/, "")).join(" ") });
    if (ended && !cancelled()) this.emit({ type: "end_call" });
  }

  async synthesize(text, emotion) {
    let phonemes = "", parts = [];
    // kokoro-js 1.2.1: stream(string) never flushes its last sentence (the splitter waits for more
    // text), so the loop hung forever - that was the "stuck at Thinking" bug. Feed a *closed*
    // TextSplitterStream instead; the coverage check is a second safety net. stream() (not
    // generate()) because it also returns the phonemes that drive the visemes.
    const letters = (x) => String(x).replace(/[^\p{L}\p{N}]/gu, "").length;
    const spoken = speakable(text);   // "50,000" -> "50000", "$1.5M" -> "1 point 5 million dollars" (audio only)
    const need = letters(spoken);
    let got = 0;
    const splitter = new this.kokoro.TextSplitterStream();
    splitter.push(spoken); splitter.close();
    for await (const chunk of this.tts.stream(splitter, { voice: this.cfg.voice })) {
      phonemes += chunk.phonemes + " "; parts.push(chunk.audio);
      got += letters(chunk.text ?? "");
      if (got >= need) break;
    }
    const sr = parts[0]?.sampling_rate ?? 24000;
    let pcm = concat(parts.map((a) => a.audio));
    // trim leading/trailing silence (Kokoro pads ~0.4 s) so sentences flow
    let a = 0, b = pcm.length; while (a < b && Math.abs(pcm[a]) < 0.01) a++; while (b > a && Math.abs(pcm[b - 1]) < 0.01) b--;
    pcm = pcm.slice(Math.max(0, a - sr * 0.05), Math.min(pcm.length, b + sr * 0.08));
    const env = envelope(pcm, sr, 50);
    const extra = {};
    if (this.arkit) {
      const frames = await this.arkit.infer(pcm, sr);
      if (frames?.length) Object.assign(extra, { arkit: frames, arkit_fps: this.arkit.fps, arkit_names: this.arkit.names });
    }
    return { type: "say", text, emotion, pcm, sr, duration: pcm.length / sr, env, env_fps: 50,
             visemes: estimateCues([...phonemes], env, 50), ...extra };
  }
}
