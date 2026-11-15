// Option 1, Photoreal: a real portrait animated live on the local GPU by tools/photoreal/photoreal_server.py
// (LivePortrait for head, eyes and brows + MuseTalk lip sync), streamed as JPEG frames over a WebSocket.
// Same interface as the 3D rigs (startSpeech / endSpeech / update / setMood), plus:
//   prepare(msg) - send a decoded sentence to the service as soon as it arrives, so frames are generated
//                  while earlier sentences still play;
//   ready(msg)   - resolves once enough frames are buffered to start the audio in sync.
// Talking frames are shown by the audio playback clock (frame = clock x 25), never by arrival time.

const WAIT_MS = 2500;
const DEBUG = new URLSearchParams(location.search).has("debug");   // ?debug: keep a timeline in rig.events          // never hold a sentence's audio longer than this waiting for frames

export class GpuPhotoRig {
  constructor(tile, url, onStatus) {
    this.canvas = document.createElement("canvas");
    this.canvas.className = "photo";
    tile.prepend(this.canvas);
    this.g = this.canvas.getContext("2d");
    this.url = url; this.onStatus = onStatus;
    this.utts = new Map(); this.cur = null; this.pid = 0;
    this.lastLoop = 0; this.seq = 0; this.drawn = 0; this.shown = -1;
    this._userTalking = false; this.closed = false;
    this.events = [];                                  // timeline for tests: [ms, what, ...]
    this.connect();
  }

  get live() { return this.ws?.readyState === 1 && !!this.info; }

  connect() {
    if (this.closed) return;
    const ws = this.ws = new WebSocket(this.url);
    ws.binaryType = "arraybuffer";
    ws.onmessage = (e) => (typeof e.data === "string" ? this.onText(JSON.parse(e.data)) : this.onFrame(e.data));
    ws.onclose = () => {
      this.info = null;
      for (const u of this.utts.values()) u.wake?.();
      if (this.closed) return;
      this.onStatus?.("Photoreal service offline — start it with ./run.sh --photoreal (retrying)");
      setTimeout(() => this.connect(), 2000);
    };
  }

  onText(m) {
    if (m.type === "hello") {
      this.info = m; this.canvas.width = m.width; this.canvas.height = m.height;
      this.onStatus?.(null);
    } else if (m.type === "utt") {
      const u = this.utts.get(m.id); if (u) { u.start = m.start; u.T = m.frames; }
    } else if (m.type === "done") {
      const u = this.utts.get(m.id); if (u) { u.done = true; this.log("done", m.id, u.n); u.wake?.(); }
    }
  }

  onFrame(buf) {
    const h = new DataView(buf), kind = h.getUint8(0), id = h.getUint32(1, true), i = h.getUint32(5, true), k = h.getUint32(9, true);
    const blob = new Blob([new Uint8Array(buf, 13)], { type: "image/jpeg" });
    if (kind === 0) {                                  // idle loop frame: show it unless speaking
      if (this.cur == null) { this.lastLoop = k; this.draw(blob); }
      return;
    }
    const u = this.utts.get(id); if (!u) return;       // interrupted / unknown sentence
    const now = performance.now();
    if (!u.n) u.t1 = now;
    u.frames.set(i, { blob, k }); u.n++;
    if (u.wake && this.enough(u, now)) u.wake();
  }

  // Start the audio once generation can stay ahead of playback for the whole sentence. Frames arrive in
  // batches, so the rate is measured from the first batch's arrival; while the LLM shares the GPU it can
  // drop below 25 fps, and then more frames are buffered first.
  enough(u, now = performance.now()) {
    if (u.done) return true;
    const batch = this.info?.batch || 4, fps = this.info?.fps || 25;
    if (u.n < 2 * batch || !u.T) return false;
    const rate = ((u.n - batch) / Math.max(1, now - u.t1)) * 1000;
    return rate >= fps * 1.05 || u.n >= u.T * (1 - rate / fps) + batch;
  }

  async draw(blob) {
    const my = ++this.seq;
    const bmp = await createImageBitmap(blob);
    if (my > this.drawn) {                             // decodes can finish out of order: never draw an older frame
      this.drawn = my;
      this.g.drawImage(bmp, 0, 0, this.canvas.width, this.canvas.height);
    }
    bmp.close();
  }

  prepare(msg) {
    if (!this.live || msg.role === "candidate" || !msg.buffer) return;
    const id = msg.__pid = ++this.pid;
    this.utts.set(id, { frames: new Map(), n: 0, done: false });
    const pcm = msg.buffer.getChannelData(0);
    // jawOpen from the neural lip sync (30 fps) drives a real jaw under the generated mouth
    const j = msg.arkit_names ? msg.arkit_names.indexOf("jawOpen") : -1;
    const jaw = j >= 0 && msg.arkit?.length ? msg.arkit.map((f) => Math.round(f[j] * 100) / 100) : null;
    let hdr = JSON.stringify({ type: "talk", id, sr: msg.buffer.sampleRate, emotion: msg.emotion || null, text: String(msg.text || "").slice(0, 400),
                               jaw, jaw_fps: msg.arkit_fps || 30 });
    hdr += " ".repeat((4 - (hdr.length % 4)) % 4);    // keep the float32 payload 4-byte aligned
    const head = new TextEncoder().encode(hdr);
    const out = new Uint8Array(4 + head.length + pcm.byteLength);
    new DataView(out.buffer).setUint32(0, head.length, true);
    out.set(head, 4);
    out.set(new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength), 4 + head.length);
    this.ws.send(out);
    this.log("prepare", id, msg.buffer.duration.toFixed(2));
  }

  log(...e) { if (DEBUG && this.events.length < 500) this.events.push([Math.round(performance.now()), ...e]); }

  ready(msg) {
    const u = this.utts.get(msg.__pid);
    this.log("ready?", msg.__pid, u?.n, u?.done);
    if (!u || !this.live || this.enough(u)) return Promise.resolve();
    return new Promise((res) => {
      const t = setTimeout(res, WAIT_MS);
      u.wake = () => { u.wake = null; clearTimeout(t); this.log("woke", msg.__pid, u.n); res(); };
    });
  }

  startSpeech(msg, clock) {
    if (!this.utts.has(msg.__pid)) return;
    for (const id of this.utts.keys()) if (id < msg.__pid) this.utts.delete(id);
    this.cur = msg.__pid; this.clock = clock; this.shown = -1;
    this.log("play", this.cur, this.utts.get(this.cur).n, this.utts.get(this.cur).T);
    if (this.live) this.ws.send(JSON.stringify({ type: "play", id: this.cur }));
  }

  endSpeech() {
    const wasTalking = this.cur != null || this.utts.size > 0;
    this.log("end", this.cur);
    this.cur = null; this.utts.clear();
    // resume idling right after the last frame shown, so the head carries on smoothly
    if (wasTalking && this.live) this.ws.send(JSON.stringify({ type: "idle", frame: this.lastLoop }));
  }

  update() {
    if (this.cur == null) return;
    const u = this.utts.get(this.cur); if (!u) return;
    let idx = Math.max(0, Math.floor(this.clock() * (this.info?.fps || 25)));
    if (u.T) idx = Math.min(idx, u.T - 1);
    while (idx > this.shown && !u.frames.has(idx)) idx--;   // newest frame not ahead of the audio
    if (idx <= this.shown) return;
    const f = u.frames.get(idx);
    this.shown = idx; this.lastLoop = f.k;
    this.draw(f.blob);
    for (const j of u.frames.keys()) if (j < idx) u.frames.delete(j);
  }

  // listening nods while the candidate talks; thinking looks while the LLM works
  setMood(m) { if (this.live && m !== this.mood) this.ws.send(JSON.stringify({ type: "mood", mood: m })); this.mood = m; }
  get userTalking() { return this._userTalking; }
  set userTalking(on) {
    on = !!on;
    if (on !== this._userTalking && this.live) this.ws.send(JSON.stringify({ type: "listen", on }));
    this._userTalking = on;
  }

  dispose() {
    this.closed = true;
    this.ws?.close();
    this.canvas.remove();
  }
}
