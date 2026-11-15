"""Live photoreal avatar service for the browser client (Setup -> Avatar -> Photoreal).

Streams the portrait as JPEG frames at 25 fps over one WebSocket per page (ws://127.0.0.1:8766).

LIVE mode (default): the whole face is driven in real time.
  - behaviour.py turns the conversation state (listening / thinking / speaking / idle), the speech audio
    and each sentence's [emotion] tag into per-frame controls: blinks at natural irregular intervals, eye
    contact while listening, a look-away when she starts to speak, nods on stressed syllables and when the
    candidate pauses, brow raises on emphasis and at the end of a question, a smile for warm sentences.
  - live_face.py (LivePortrait, fp16 + torch.compile, ~60 ms/frame) renders the upper face every 2nd frame
    while idle and every 3rd while speaking; the frames between are crossfades.
  - MuseTalk redraws the mouth on every frame of a sentence (reference latents via the tiny encoder).
  A sentence's frames are generated ahead while the page buffers them; a shadow copy of the behaviour keeps
  the live face still (blinks only) in the meantime, so the switch to the generated frames is seamless.

LIBRARY mode (--library): the earlier approach, pre-rendered clips (motion_library.py) chained at random by a
Director, for GPUs where LivePortrait can't run live.

Protocol:
  page -> service  binary: u32 header length + JSON {"type":"talk","id","sr","emotion","text"} + float32 mono PCM
                   text:   {"type":"play","id"}        the sentence's audio just started
                           {"type":"idle","frame"}     speech over/interrupted
                           {"type":"mood","mood"}      listening | thinking | speaking
                           {"type":"listen","on"}      the candidate is talking (voice activity)
  service -> page  text:   {"type":"hello","frames","fps","batch","width","height"}
                           {"type":"utt","id","start","frames"}   {"type":"done","id"}
                   binary: u8 kind (0 idle, 1 talk), u32 id, u32 index, u32 avatar frame, then JPEG bytes

Run: TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 python photoreal_server.py   (or ./run.sh --photoreal)
"""

import argparse, asyncio, copy, json, math, random, struct, time, traceback
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

import cv2, numpy as np, torch
import websockets
from scipy.signal import resample_poly

from talker import Talker, FPS, DEFAULT_PHOTO, _FaceParsing
from musetalk.utils.blending import get_image_prepare_material

LEAD = 6            # library mode: idle frames still shown after a talk request before its first talking frame
REPEAT_GAP = 10 * FPS   # the LLM tags most sentences of a reply alike: re-show the same emotion at most every 10 s
CHUNK = 12          # live mode: frames generated per GPU job (keeps the idle stream interleaved)


class Director:
    """Library mode: chooses which avatar frame comes next. Clips start and end neutral, so any clip may follow
    any other; picking them at random (never the same clip twice running) keeps the motion unpredictable."""

    def __init__(self, clips):
        self.clips, self.rng = clips, random.Random()
        self.by_kind = defaultdict(list)
        self.clip_of = np.zeros(sum(n for _, n, _ in clips), dtype=np.int32)
        for c, (s, n, kind) in enumerate(clips):
            self.by_kind[kind].append(c)
            self.clip_of[s:s + n] = c
        self.last_clip, self.pending = None, None
        self.last_emotion, self.since_emotion = None, REPEAT_GAP
        self.mood, self.user_talking = "listening", False

    def pick(self, kind):
        options = self.by_kind.get(kind) or self.by_kind.get("idle") or [0]
        c = self.rng.choice([c for c in options if c != self.last_clip] or options)
        self.last_clip = c
        return self.clips[c][0]

    def idle_kind(self):
        if self.pending:
            kind, self.pending = self.pending, None
            return kind
        if self.user_talking and self.rng.random() < 0.6:
            return "listen"
        if self.mood == "thinking" and self.rng.random() < 0.6:
            return "thinking"
        return "idle"

    def after(self, k, talking=False):
        s, n, _ = self.clips[self.clip_of[k]]
        if k + 1 < s + n:
            return k + 1
        if talking:
            kind, self.pending = (self.pending, None) if self.pending else ("talk", None)
            return self.pick(kind)
        return self.pick(self.idle_kind())

    def plan_talk(self, k, T, emotion=None):
        if emotion and emotion in self.by_kind and emotion not in ("talk", "idle", "listen") and \
                (emotion != self.last_emotion or self.since_emotion >= REPEAT_GAP):
            self.pending, self.last_emotion, self.since_emotion = emotion, emotion, 0
        self.since_emotion += T
        plan = []
        for _ in range(T):
            k = self.after(k, talking=True)
            plan.append(k)
        return plan


def prosody(pcm16k, env):
    """Pitch accents (frame indices at 25 fps) and whether the sentence ends on a rise. yin is instant (pyin takes
    seconds); voicing is approximated by loudness."""
    try:
        import librosa
        f0 = librosa.yin(pcm16k.astype(np.float32), fmin=70, fmax=400, sr=16000, frame_length=1024, hop_length=16000 // FPS)
    except Exception:
        return None, False
    n = min(len(f0), len(env))
    f0, e = f0[:n], env[:n]
    voiced = e > max(0.08, 0.35 * np.median(e[e > 0.02])) if np.any(e > 0.02) else e > 0
    if voiced.sum() < 5:
        return None, False
    f = np.where(voiced, f0, np.nan)
    med, sd = np.nanmedian(f), np.nanstd(f)
    accents, last = [], -100
    for i in range(1, n - 1):
        if voiced[i] and f[i] >= np.nan_to_num(f[i - 1]) and f[i] > np.nan_to_num(f[i + 1]) and f[i] > med + 0.5 * sd \
                and e[i] > np.median(e[voiced]) and i - last >= 0.3 * FPS:
            accents.append(i); last = i
    v = np.where(voiced)[0]
    rise = len(v) > 30 and np.nanmean(f[v[-10:]]) > 1.08 * np.nanmean(f[v[-35:-10]])
    return accents, bool(rise)


class LiveRenderer:
    """Live mode: frames from LivePortrait (behaviour-driven) with MuseTalk mouths. All methods that touch
    the GPU run on the single `gpu` executor, in order."""

    def __init__(self, tk, lf, quality):
        from behaviour import Behaviour
        self.tk, self.lf, self.quality = tk, lf, quality
        self.beh = Behaviour()                 # the conversation's behaviour state
        self.live = self.beh                   # what drives the idle stream (a held shadow while a sentence is prepared)
        self.idle_prev = self.talk_prev = None # last LivePortrait render (frame, shift) of each stream, for crossfades
        x1, y1, x2, y2 = lf.mouth_box()
        fp = _FaceParsing(left_cheek_width=90, right_cheek_width=90)
        mask, (cx, cy, _, _) = get_image_prepare_material(lf.neutral, [x1, y1, x2, y2], mode="jaw", fp=fp)
        self.alpha = (mask[y1 - cy:y2 - cy, x1 - cx:x2 - cx].astype(np.float32) / 255)[..., None]
        del fp

    # -- rendering ------------------------------------------------------------------------------------
    def frames_for(self, prev, controls, stride):
        """Upper-face frames for a list of controls: LivePortrait at the last frame of each group of `stride`,
        crossfaded from the previous render. Returns ([(frame, shift)], last render)."""
        out = []
        for g in range(0, len(controls), stride):
            grp = controls[g:g + stride]
            new = self.lf.render(**grp[-1])
            if prev is None:
                prev = new
            for k in range(len(grp)):
                w = (k + 1) / len(grp)
                frame = new[0] if w >= 1 else cv2.addWeighted(prev[0], 1 - w, new[0], w, 0)
                shift = [p + (n - p) * w for p, n in zip(prev[1], new[1])]
                out.append((frame, shift))
            prev = new
        return out, prev

    def idle_group(self, stride):
        controls = [self.live.tick() for _ in range(stride)]
        if self.live.hold and self.idle_prev is not None and not any(c["blink"] for c in controls):
            # a held shadow (sentence being prepared): nothing moves but blinks, so reuse the last frame
            self.held_jpeg = getattr(self, "held_jpeg", None) or self.jpeg(self.idle_prev[0])
            return [self.held_jpeg] * stride
        self.held_jpeg = None
        frames, self.idle_prev = self.frames_for(self.idle_prev, controls, stride)
        return [self.jpeg(f) for f, _ in frames]

    def jpeg(self, frame):
        frame[0] = frame[1]                               # LivePortrait's paste-back leaves a dark top row
        return cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality])[1].tobytes()

    @torch.no_grad()
    def talk_chunk(self, chunks, controls, stride):
        """Generate `len(controls)` talking frames: upper face from LivePortrait, mouth from MuseTalk.
        Tensor shapes are kept fixed (CHUNK frames, batches of tk.batch): the compiled models pay tens of
        seconds for every new shape."""
        tk = self.tk
        n = len(controls)
        controls = list(controls) + [controls[-1]] * (CHUNK - n)
        chunks = torch.cat([chunks, chunks[-1:].expand(CHUNK - n, -1, -1)]) if n < CHUNK else chunks
        ups, self.talk_prev = self.frames_for(self.talk_prev, controls, stride)
        boxes = [self.lf.mouth_box(s) for _, s in ups]
        # reference latents only at the LivePortrait keyframes (last frame of each group); the crossfaded
        # frames between differ by a pixel or two, not worth 4 ms each
        keys = [min(g + stride - 1, CHUNK - 1) for g in range(0, CHUNK, stride)]
        crops = [cv2.resize(ups[k][0][y1:y2, x1:x2], (256, 256), interpolation=cv2.INTER_LANCZOS4) for k, (x1, y1, x2, y2) in ((k, boxes[k]) for k in keys)]
        x = torch.from_numpy(np.stack([cv2.cvtColor(c, cv2.COLOR_BGR2RGB) for c in crops])).permute(0, 3, 1, 2).to(tk.dev, tk.dt).div(255)
        masked = x.clone(); masked[..., 128:, :] = 0
        key_refs = torch.cat([tk.taesd.encode(masked * 2 - 1).latents, tk.taesd.encode(x * 2 - 1).latents], 1)
        refs = key_refs[[min(i // stride, len(keys) - 1) for i in range(CHUNK)]]
        faces = []
        for i in range(0, CHUNK, tk.batch):
            a = tk.pe(chunks[i:i + tk.batch].to(tk.dev, tk.dt)).to(tk.dt)
            pred = tk.unet(refs[i:i + tk.batch], torch.zeros(tk.batch, device=tk.dev, dtype=torch.long), encoder_hidden_states=a).sample
            faces += list(((tk.taesd.decode(pred).sample / 2 + 0.5).clamp(0, 1) * 255).permute(0, 2, 3, 1).byte().cpu().numpy())
        return [(ups[i][0], boxes[i], faces[i]) for i in range(n)]       # blended + encoded off the GPU thread

    def compose(self, item):
        """CPU: paste the MuseTalk mouth into its frame and JPEG-encode."""
        frame, (x1, y1, x2, y2), face = item
        frame = frame.copy()
        face = cv2.resize(cv2.cvtColor(face, cv2.COLOR_RGB2BGR), (x2 - x1, y2 - y1), interpolation=cv2.INTER_LANCZOS4)
        frame[y1:y2, x1:x2] = (frame[y1:y2, x1:x2] * (1 - self.alpha) + face * self.alpha).astype(np.uint8)
        return self.jpeg(frame)

    # -- conversation events ---------------------------------------------------------------------------
    def plan(self, pcm16k, text, emotion, first_of_turn, jaw=None, jaw_fps=30):
        """Per-frame controls for a sentence (advances the behaviour through it). When the face is idling, a
        held shadow keeps streaming meanwhile. Prosody from the audio decides the timing: pitch accents (F0
        peaks on loud, voiced frames) carry the emphasis, a final F0 rise marks a question."""
        hop = 16000 // FPS
        env = np.array([np.sqrt(np.mean(pcm16k[i:i + hop] ** 2)) for i in range(0, max(hop, len(pcm16k) - hop + 1), hop)])
        env = env / max(1e-6, env.max())
        accents, rise = prosody(pcm16k, env)
        if jaw is not None and len(jaw):                  # resample the lip-sync jaw to video frames, smooth a little
            j = np.interp(np.arange(len(env)) / FPS, np.arange(len(jaw)) / jaw_fps, np.asarray(jaw, dtype=np.float32))
            j = np.convolve(j, [0.25, 0.5, 0.25], mode="same")
            # wav2arkit's jawOpen peaks anywhere from ~0.12 to 0.25 depending on the sentence: scale each sentence to
            # its own peak (95th percentile) so open vowels really open; a floor keeps quiet sentences from inflating
            jaw = np.clip(j / max(0.10, float(np.percentile(j, 95))) * 0.9, 0, 1)
        if first_of_turn:
            self.live = copy.deepcopy(self.beh); self.live.hold = True
            self.talk_prev = self.idle_prev
        return self.beh.plan_sentence(env, is_question=text.strip().endswith("?") or rise, emotion=emotion,
                                      first_of_turn=first_of_turn, accents=accents, jaw=jaw)

    def resume_idle(self):
        """Speech over or interrupted: the live stream continues from the conversation's behaviour state."""
        self.live = self.beh
        self.idle_prev = self.talk_prev or self.idle_prev

    def warmup(self):
        t0 = time.time()
        chunks = self.tk.features((np.random.default_rng(0).standard_normal(16000) * 0.05).astype(np.float32))
        ctr = self.plan(np.zeros(16000, np.float32), "warm up?", "warm", True)[:len(chunks)]
        for stride in (3, 4):
            [self.compose(it) for it in self.talk_chunk(chunks[:CHUNK], ctr[:CHUNK], stride)]
            self.talk_chunk(chunks[:3], ctr[:3], stride)  # a short last chunk (padded to CHUNK: same shapes)
        for stride in (2, 3, 4, 5):                  # every idle stride the service may use (fixed shapes)
            self.idle_group(stride)
        self.resume_idle(); self.beh.set_mode("idle")
        torch.cuda.synchronize()
        print(f"live warm-up: {time.time()-t0:.1f}s", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--photo", default=DEFAULT_PHOTO)
    ap.add_argument("--library", action="store_true", help="pre-rendered motion library instead of live LivePortrait")
    ap.add_argument("--still", action="store_true", help="library mode without motion: animate the plain photo")
    ap.add_argument("--width", type=int, default=1280, help="streamed frame width")
    ap.add_argument("--quality", type=int, default=85, help="JPEG quality of talking frames")
    ap.add_argument("--decoder", choices=["tiny", "sd"], default="tiny", help="library mode: tiny = TAESD (faster), sd = SD VAE")
    ap.add_argument("--no-compile", action="store_true", help="live mode: skip torch.compile (slower frames, faster start)")
    ap.add_argument("--talk-stride", type=int, default=4, help="live mode: LivePortrait renders every Nth frame while speaking (3 = smoother, needs more GPU)")
    ap.add_argument("--idle-stride", type=int, default=2, help="live mode: LivePortrait renders every Nth frame while idle; while the LLM is thinking or a sentence is being generated it renders every (N+1)th")
    ap.add_argument("--sensor-log", default="", help="append GPU/CPU temperature and power every 5 s to this file (fsync'd), to diagnose resets under load")
    a = ap.parse_args()

    live = not a.library
    if a.sensor_log:
        import threading
        def sensors():
            import glob
            def read(path):
                try:
                    return int(open(path).read())
                except Exception:
                    return None
            hw = [(h, open(h + "/name").read().strip()) for h in glob.glob("/sys/class/hwmon/hwmon*")]
            with open(a.sensor_log, "a") as f:
                while True:
                    parts = [time.strftime("%H:%M:%S")]
                    for h, n in hw:
                        if n not in ("amdgpu", "k10temp"):
                            continue
                        for t in sorted(glob.glob(h + "/temp*_input")):
                            v = read(t); parts.append(f"{n}.{t.split('/')[-1][:-6]}={v // 1000}C") if v else None
                        for pth in sorted(glob.glob(h + "/power*_average") + glob.glob(h + "/power*_input")):
                            v = read(pth); parts.append(f"{n}.power={v / 1e6:.0f}W") if v else None
                    f.write(" ".join(parts) + "\n"); f.flush(); os.fsync(f.fileno())
                    time.sleep(5)
        import os
        threading.Thread(target=sensors, daemon=True).start()
    tk = Talker(a.photo, still=True, decoder="tiny") if live else Talker(a.photo, a.still, decoder=a.decoder)
    enc = ThreadPoolExecutor(4)        # decode + blend + JPEG (OpenCV releases the GIL)
    gpu = ThreadPoolExecutor(1)        # all GPU work, one job at a time, in order
    if live:
        # Everything that touches the compiled models runs on the single GPU thread, including the compile
        # and the warm-up: the first call from a different thread costs about a minute.
        from live_face import LiveFace
        size = (min(a.width, tk.W) // 2 * 2, round(tk.H * min(a.width, tk.W) / tk.W) // 2 * 2)
        lf = gpu.submit(lambda: LiveFace(a.photo, size=size, compile=not a.no_compile)).result()
        ow, oh = lf.W, lf.H
        renderer = gpu.submit(lambda: LiveRenderer(tk, lf, a.quality)).result()
        gpu.submit(renderer.warmup).result()
    else:
        tk.warmup()
        ow = min(a.width, tk.W) // 2 * 2
        oh = round(tk.H * ow / tk.W) // 2 * 2
    idle_jpeg = {}

    def jpeg(frame, quality=a.quality):
        small = cv2.resize(frame, (ow, oh), interpolation=cv2.INTER_AREA) if ow != tk.W else frame.copy()
        small[0] = small[1]                               # LivePortrait's paste-back leaves a dark top row
        return cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, quality])[1].tobytes()

    def idle_frame(k):                 # library mode: frames are already stream-sized JPEGs
        if ow == tk.W:
            return tk.jpegs[k]
        if k not in idle_jpeg:
            idle_jpeg[k] = jpeg(tk.frame(k))
        return idle_jpeg[k]

    def frame_msg(kind, uid, i, k, data):
        return struct.pack("<BIII", kind, uid, i, k) + data

    async def handler(ws):
        loop = asyncio.get_running_loop()
        director = None if live else Director(tk.clips)
        st = dict(mode="idle", cursor=0 if live else director.pick("idle"), queue=deque(), hold=False, last_end=None,
                  epoch=0, pending=0)
        talk_lock, gen_lock = asyncio.Lock(), asyncio.Lock()      # gen_lock: one sentence generates at a time
        peer = ws.remote_address
        print("page connected", peer, flush=True)
        await ws.send(json.dumps({"type": "hello", "frames": 1 if live else len(tk.jpegs), "fps": FPS, "batch": tk.batch, "width": ow, "height": oh}))

        async def send(data):
            try:
                await ws.send(data)
            except websockets.ConnectionClosed:
                pass

        async def idle_stream_live():
            """Live mode: render groups ahead on the GPU, send their frames on a 25 fps schedule."""
            t0, n = loop.time(), 0
            # share the GPU while a sentence is generated or the LLM is thinking (peak load is what resets the box)
            stride = lambda: a.idle_stride + 2 if st["pending"] else a.idle_stride + (1 if renderer.beh.mode == "thinking" else 0)
            fut = loop.run_in_executor(gpu, renderer.idle_group, stride())
            while True:
                if st["mode"] != "idle":                               # talking: the GPU belongs to the sentence
                    await asyncio.sleep(0.05); n = 0; t0 = loop.time()
                    fut = loop.run_in_executor(gpu, renderer.idle_group, stride())
                    continue
                frames = await fut
                fut = loop.run_in_executor(gpu, renderer.idle_group, stride())
                for data in frames:
                    n += 1
                    await asyncio.sleep(max(0.0, t0 + n / FPS - loop.time()))
                    if loop.time() - (t0 + n / FPS) > 0.5:            # fell behind (GPU busy): re-base the schedule
                        t0 = loop.time() - n / FPS
                    if st["mode"] == "idle":
                        await send(frame_msg(0, 0, 0, 0, data))

        async def idle_stream_library():
            t0, n = loop.time(), 0
            while True:
                n += 1
                await asyncio.sleep(max(0.0, t0 + n / FPS - loop.time()))
                if st["mode"] != "idle":
                    continue
                if st["queue"]:
                    k = st["queue"].popleft()
                elif st["hold"]:
                    continue
                else:
                    k = director.after(st["cursor"])
                st["cursor"] = k
                data = idle_frame(k) if (ow == tk.W or k in idle_jpeg) else await loop.run_in_executor(enc, idle_frame, k)
                await send(frame_msg(0, 0, 0, k, data))

        def generate_library(uid, chunks, plan, epoch, t_recv):
            try:
                t0, futs, first = time.time(), [], []
                alive = lambda: st["epoch"] == epoch
                for i, k, face in tk.faces(chunks, plan, cancelled=lambda: not alive()):
                    f = enc.submit(lambda i=i, k=k, face=face: frame_msg(1, uid, i, k, jpeg(tk.blend(k, face))))
                    f.add_done_callback(lambda f: alive() and asyncio.run_coroutine_threadsafe(send(f.result()), loop))
                    if i == 2 * tk.batch - 1:
                        first.append(time.time() - t_recv)
                    futs.append(f)
                for f in futs:
                    f.result()
                if alive():
                    asyncio.run_coroutine_threadsafe(send(json.dumps({"type": "done", "id": uid})), loop)
                    print(f"  sentence {uid}: {len(chunks)} frames in {time.time()-t0:.2f}s ({len(chunks)/(time.time()-t0):.0f} fps), "
                          f"first {2 * tk.batch} after {first[0] * 1000 if first else 0:.0f} ms", flush=True)
                else:
                    print(f"  sentence {uid}: cancelled", flush=True)
            except Exception:
                print(f"  sentence {uid} failed:"); traceback.print_exc()

        async def generate_live(uid, chunks, controls, epoch, t_recv):
            """Chunked so idle groups interleave on the GPU; frames go out as each chunk finishes."""
            T, first = len(controls), None
            try:
              async with gen_lock:
                t0 = time.time()
                for c0 in range(0, T, CHUNK):
                    if st["epoch"] != epoch:
                        print(f"  sentence {uid}: cancelled", flush=True); return
                    items = await loop.run_in_executor(gpu, renderer.talk_chunk, chunks[c0:c0 + CHUNK], controls[c0:c0 + CHUNK], a.talk_stride)
                    datas = await asyncio.gather(*[loop.run_in_executor(enc, renderer.compose, it) for it in items])
                    if first is None:
                        first = time.time() - t_recv
                    for j, data in enumerate(datas):
                        await send(frame_msg(1, uid, c0 + j, 0, data))
                await send(json.dumps({"type": "done", "id": uid}))
                print(f"  sentence {uid}: {T} frames in {time.time()-t0:.2f}s ({T/(time.time()-t0):.0f} fps), "
                      f"first {CHUNK} after {first * 1000:.0f} ms", flush=True)
            except Exception:
                print(f"  sentence {uid} failed:"); traceback.print_exc()
            finally:
                st["pending"] -= 1

        async def talk(hdr, pcm):
            t_recv = time.time()
            try:
                async with talk_lock:                        # keeps sentences in order
                    epoch = st["epoch"]
                    sr = int(hdr["sr"])
                    if sr != 16000:
                        g = math.gcd(sr, 16000)
                        pcm = resample_poly(pcm, 16000 // g, sr // g).astype(np.float32)
                    chunks = await loop.run_in_executor(gpu, tk.features, pcm)
                    if epoch != st["epoch"]:
                        return
                    first = st["last_end"] is None
                    if live:
                        st["pending"] += 1
                        controls = await loop.run_in_executor(gpu, renderer.plan, pcm, str(hdr.get("text", "")), hdr.get("emotion"), first,
                                                              hdr.get("jaw"), hdr.get("jaw_fps", 30))
                        controls = controls[:len(chunks)] + [controls[-1]] * max(0, len(chunks) - len(controls))
                        st["last_end"] = 1
                        await send(json.dumps({"type": "utt", "id": hdr["id"], "start": 0, "frames": len(chunks)}))
                        asyncio.create_task(generate_live(hdr["id"], chunks, controls, epoch, t_recv))
                    else:
                        if first:
                            q = st["queue"]
                            while len(q) < LEAD:
                                q.append(director.after(q[-1] if q else st["cursor"]))
                            st["hold"], prev = True, q[-1]
                        else:
                            prev = st["last_end"]
                        plan = director.plan_talk(prev, len(chunks), hdr.get("emotion"))
                        st["last_end"] = plan[-1]
                        await send(json.dumps({"type": "utt", "id": hdr["id"], "start": plan[0], "frames": len(chunks)}))
                        loop.run_in_executor(gpu, generate_library, hdr["id"], chunks, plan, epoch, t_recv)
            except Exception:
                print(f"  sentence {hdr.get('id')} failed:"); traceback.print_exc()

        async def guarded(coro):
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:
                print("idle stream failed:"); traceback.print_exc()

        streamer = asyncio.create_task(guarded(idle_stream_live() if live else idle_stream_library()))
        tasks = set()
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    hl = struct.unpack_from("<I", msg)[0]
                    hdr = json.loads(msg[4:4 + hl])
                    pcm = np.frombuffer(msg, dtype=np.float32, offset=4 + hl).copy()
                    t = asyncio.create_task(talk(hdr, pcm))
                    tasks.add(t); t.add_done_callback(tasks.discard)
                    continue
                m = json.loads(msg)
                if m["type"] == "play":
                    st.update(mode="talk", hold=False); st["queue"].clear()
                elif m["type"] == "idle":
                    st["epoch"] += 1                         # cancels any generation still running
                    st.update(mode="idle", hold=False, last_end=None)
                    st["queue"].clear()
                    if live:
                        renderer.resume_idle()
                        renderer.beh.set_mode("listening" if renderer.beh.mode == "listening" else "idle")
                    else:
                        st["cursor"] = int(m.get("frame", st["cursor"])) % len(tk.jpegs)
                elif m["type"] == "mood":
                    if live:
                        mood = m.get("mood")
                        if mood == "thinking":
                            renderer.beh.set_mode("thinking")
                        elif mood == "listening" and renderer.beh.mode != "listening":
                            renderer.beh.set_mode("idle")
                    else:
                        director.mood = m.get("mood")
                elif m["type"] == "listen":
                    on = bool(m.get("on"))
                    if live:
                        if on:
                            renderer.beh.set_mode("listening")
                        elif renderer.beh.mode == "listening":
                            renderer.beh.candidate_paused()
                    else:
                        director.user_talking = on
        finally:
            streamer.cancel()
            st["epoch"] += 1
            print("page disconnected", peer, flush=True)

    async def serve():
        async with websockets.serve(handler, "0.0.0.0", a.port, max_size=64 * 2**20, compression=None):
            what = "live LivePortrait + behaviour" if live else f"{len(tk.jpegs)} frames, {len(tk.clips)} clips"
            print(f"photoreal service ({what}): {ow}x{oh} stream on ws://127.0.0.1:{a.port}", flush=True)
            await asyncio.Future()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
