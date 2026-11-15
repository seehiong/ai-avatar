"""WebSocket bridge between the avatar clients and the STT -> LLM -> TTS pipeline.

Protocol
  client -> server (JSON text frames)
    {"type": "start"}                     interviewer opens the conversation
    {"type": "text",  "text": "..."}      candidate typed something
    {"type": "audio", "wav_b64": "..."}   candidate utterance (push-to-talk), 16-bit PCM WAV
    {"type": "playback", "playing": b}    client is/isn't playing avatar speech (barge-in bar)
    {"type": "reset"}                     clear conversation history
    {"type": "config", "system": "...", "model": "...", "patience": "quick|normal|patient"}
    {"type": "nudge", "text": "(silence: ...)"}  candidate has been quiet: interviewer prompts them
    {"type": "end"}                       candidate ended the interview: cancel everything in flight
    {"type": "complete", "id": n, "prompt": "...", "json": true}  one-shot completion (feedback)
  client -> server (binary frames)
    16 kHz mono int16 PCM, streamed continuously while the open mic is on
  server -> client
    {"type": "vad", "state": "speech|end"}   open mic: candidate started / stopped talking
    {"type": "partial", "text": "..."}    live caption of what the candidate is saying (replaced by transcript)
    {"type": "end_call"}                  the interviewer said goodbye ([end]): hang up after the audio finishes
    {"type": "merge"}                     candidate continued their previous answer: drop their last line and
                                          the reply they talked over; the next user transcript is the whole answer
    {"type": "state", "state": "listening|thinking|speaking"}
    {"type": "transcript", "role": "user|assistant", "text": "..."}
    {"type": "say", "text": "...", "emotion": "warm|curious|...", "wav_b64": "...", "duration": s,
     "visemes": [{"t": s, "v": "A".."H"|"X", "o": oculus viseme}, ...],
     "env": [0..1 loudness], "env_fps": 50,
     "arkit": [[52 weights] x frames], "arkit_fps": 30, "arkit_names": [...]}   (arkit: neural lip sync, optional)
                                          one per sentence, play in order
    {"type": "stop"}                      drop queued/playing speech (barge-in or superseded)
    {"type": "completion", "id": n, "text": "..."} | {"type": "completion", "id": n, "error": "..."}
    {"type": "error", "message": "..."}
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import logging
import threading
import time
import urllib.error
from pathlib import Path

import numpy as np
import websockets

from llm import Interviewer
from pacing import Pacer
from tts import ENV_FPS, make_speaker

log = logging.getLogger("avatar")

# Silence that ends the candidate's turn. Interview answers have thinking pauses, so the default
# is much longer than chit-chat; an answer that sounds unfinished is held for HOLD_S more.
PATIENCE = {"quick": 0.7, "normal": 1.2, "patient": 2.0}
# If the candidate starts talking again within this long after their turn ended (i.e. while the
# interviewer is thinking or has only just started replying), they weren't finished: cancel the reply,
# drop it from the history and merge both parts into one answer.
MERGE_WINDOW_S = 4.0
HOLD_S = 1.5
HALLUCINATION = re.compile(r"^(thank(s| you)( (so much|very much|for watching|for listening))?|you|bye|okay|ok|so|um+|uh+|hmm+|yeah|oh|\.+)[.!?, ]*$", re.I)
_TRAILING = {"and", "but", "so", "because", "or", "um", "uh", "erm", "like", "the", "a", "an", "to", "of",
             "with", "that", "which", "then", "if", "when", "i", "we", "my", "our", "is", "was", "were", "for",
             "in", "on", "at", "by", "as", "also", "basically", "actually"}


def looks_unfinished(text: str) -> bool:
    t = text.strip().lower()
    if not t:
        return False
    if t.endswith((",", "...", "…", "-", "—")):
        return True
    last = t.rstrip(".!?").split()[-1] if t.rstrip(".!?").split() else ""
    return last in _TRAILING
HERE = Path(__file__).parent


class Session:
    def __init__(self, ws, args, speaker, transcriber, arkit=None):
        self.ws = ws
        self.speaker = speaker
        self.arkit = arkit                       # neural lip sync (audio -> ARKit blendshapes), optional
        self.transcriber = transcriber
        self.llm = Interviewer(args.model, args.ollama, role=args.role)
        self.busy = asyncio.Lock()
        self.gen = 0                 # bumped to cancel the in-flight reply
        self.turns = None            # TurnDetector, created on first audio frame
        self.spec: asyncio.Task | None = None   # speculative transcription from the last pause
        self.end_silence_s = PATIENCE["normal"]
        self.held_text = ""                     # unfinished thought, waiting to see if they go on
        self.last_turn: tuple[str, float] | None = None   # (text, time) of the last finished answer
        self.merge_pending = False
        self.hold_task: asyncio.Task | None = None
        # live captions while the candidate speaks: text locked in at pauses + re-transcribed tail
        self.vocab = ""                          # STT priming terms from the interview setup
        self.voice = None                        # Kokoro voice for this session (matches the avatar)
        self.tts_provider = None                 # TTS provider override (kokoro | piper | zonos)
        self.pacer = Pacer()                     # gets the interviewer to the planned bank questions
        self.llm.question_filter = self.pacer.dedupe
        self.committed: list[str] = []
        self.commit_frame = 0
        self.commit_chain: asyncio.Task | None = None
        self.partial_task: asyncio.Task | None = None
        self.last_partial = 0.0

    async def send(self, **msg) -> None:
        try:
            await self.ws.send(json.dumps(msg))
        except websockets.ConnectionClosed:
            pass

    def cancel(self) -> None:
        self.gen += 1

    def _arkit_frames(self, wav: bytes) -> list | None:
        """Neural lip-sync frames for a synthesized sentence (None if the model isn't available)."""
        if self.arkit is None or not wav:
            return None
        try:
            import io
            import wave
            with wave.open(io.BytesIO(wav)) as w:
                audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
                return self.arkit.infer(audio, w.getframerate())
        except Exception as e:
            log.warning("arkit inference failed: %s", e)
            return None

    async def respond(self, user_text: str | None) -> None:
        if user_text and not user_text.startswith("(silence"):
            user_text = self.pacer.steer(user_text)
        self.gen += 1
        my_gen = self.gen
        cancelled = lambda: self.gen != my_gen  # noqa: E731
        await self.send(type="state", state="thinking")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        t0 = time.perf_counter()

        def produce() -> None:
            try:
                for emotion, sentence in self.llm.reply_sentences(user_text, cancelled):
                    if cancelled():
                        break
                    wav, cues, dur, env = self.speaker.synthesize(sentence, self.voice, self.tts_provider)
                    frames = self._arkit_frames(wav)
                    loop.call_soon_threadsafe(queue.put_nowait, (emotion, sentence, wav, cues, dur, env, frames))
            except Exception as e:  # surface to client
                log.exception("pipeline failed")
                loop.call_soon_threadsafe(queue.put_nowait, e)
            loop.call_soon_threadsafe(queue.put_nowait, None)

        worker = threading.Thread(target=produce, daemon=True)
        worker.start()
        first, spoken = True, []
        while (item := await queue.get()) is not None:
            if cancelled():
                continue                      # drain until the worker stops
            if isinstance(item, Exception):
                await self.send(type="error", message=str(item))
                continue
            emotion, sentence, wav, cues, dur, env, frames = item
            if first:
                log.info("first audio after %.2fs", time.perf_counter() - t0)
                await self.send(type="state", state="speaking")
                first = False
            spoken.append(sentence)
            extra = {"arkit": frames, "arkit_fps": 30, "arkit_names": self.arkit.names} if frames else {}
            await self.send(type="say", text=sentence, emotion=emotion, duration=dur, visemes=cues,
                            env=env, env_fps=ENV_FPS, wav_b64=base64.b64encode(wav).decode(), **extra)
        await asyncio.to_thread(worker.join)  # history is final before the next turn starts
        self.pacer.note_reply(" ".join(spoken))
        await self.send(type="transcript", role="assistant", text=" ".join(spoken))
        if getattr(self.llm, "ended", False) and not cancelled():
            log.info("interviewer ended the call")
            await self.send(type="end_call")             # page hangs up once the goodbye has played
        if cancelled():
            log.info("reply interrupted after %d sentence(s)", len(spoken))

    # ---- open mic ----------------------------------------------------------------

    def on_audio(self, data: bytes) -> None:
        """Binary frame: 16 kHz int16 PCM. Runs VAD inline (cheap) and schedules work."""
        if self.transcriber is None:
            return
        if self.turns is None:
            from turns import TurnDetector
            self.turns = TurnDetector(end_silence_s=self.end_silence_s)
        for event, pcm in self.turns.feed(np.frombuffer(data, dtype=np.int16)):
            if event == "speech_start":
                log.info("vad: speech")
                self.cancel()                                   # barge-in
                if self.last_turn and time.perf_counter() - self.last_turn[1] < MERGE_WINDOW_S and not self.held_text:
                    # "I wasn't done": continue the previous answer instead of starting a new turn
                    log.info("candidate continued %.1fs after their turn ended: merging", time.perf_counter() - self.last_turn[1])
                    self.held_text, self.merge_pending = self.last_turn[0], True
                    asyncio.create_task(self.send(type="merge"))
                self.last_turn = None
                if self.hold_task:                              # they carried on: merge with held text
                    self.hold_task.cancel()
                    self.hold_task = None
                asyncio.create_task(self.send(type="stop"))
                asyncio.create_task(self.send(type="vad", state="speech"))
                self.spec = None
                self._reset_live()
            elif event == "pause":
                self.spec = asyncio.create_task(asyncio.to_thread(self.transcriber.transcribe_pcm16k, pcm, self.vocab))
                self._commit_segment()
            elif event == "turn_end":
                asyncio.create_task(self.end_of_turn(pcm, self.spec))
                self.spec = None
                self._reset_live()
            elif event == "discard":
                asyncio.create_task(self.send(type="vad", state="end"))
        if self.turns.in_speech:
            self._maybe_partial()

    # ---- live captions -------------------------------------------------------------
    PARTIAL_EVERY_S = 0.7

    def _reset_live(self) -> None:
        self.committed, self.commit_frame, self.commit_chain = [], 0, None

    def _commit_segment(self) -> None:
        """At a pause, transcribe the audio since the last pause once and lock that text in."""
        frames = self.turns.frames
        seg, self.commit_frame = frames[self.commit_frame:], len(frames)
        if not seg:
            return
        pcm, prev, committed = np.concatenate(seg), self.commit_chain, self.committed

        async def run():
            if prev:
                await prev                                   # keep segments in order
            text = await asyncio.to_thread(self.transcriber.transcribe_pcm16k, pcm, self.vocab)
            if text:
                committed.append(text)
        self.commit_chain = asyncio.create_task(run())

    def _maybe_partial(self) -> None:
        now = time.perf_counter()
        if now - self.last_partial < self.PARTIAL_EVERY_S or (self.partial_task and not self.partial_task.done()):
            return
        tail = self.turns.frames[self.commit_frame:]
        if len(tail) * 512 / 16000 < 0.4:
            return
        self.last_partial = now
        pcm, committed, held = np.concatenate(tail), self.committed, self.held_text

        async def run():
            text = await asyncio.to_thread(self.transcriber.transcribe_pcm16k, pcm, self.vocab)
            if self.turns and self.turns.in_speech and committed is self.committed:   # still the same turn
                live = " ".join(x for x in [held, *committed, text] if x)
                await self.send(type="partial", text=live)
        self.partial_task = asyncio.create_task(run())

    async def end_of_turn(self, pcm: np.ndarray, spec: asyncio.Task | None) -> None:
        t0 = time.perf_counter()
        # Only trailing silence follows the last pause, so its speculative transcript is final.
        text = await spec if spec else await asyncio.to_thread(self.transcriber.transcribe_pcm16k, pcm, self.vocab)
        log.info("stt %.2fs after end of turn%s: %r", time.perf_counter() - t0,
                 " (speculative)" if spec else "", text)
        if HALLUCINATION.match(text.strip()):   # Whisper "hears" "Thank you." / "you" in noise or silence
            log.info("dropping likely Whisper hallucination: %r", text)
            text = ""
        text = (self.held_text + " " + text).strip()
        self.held_text = ""
        if looks_unfinished(text):
            # "...and then we, um" -> they're probably still thinking: keep listening a bit longer
            log.info("turn sounds unfinished; holding %.1fs", HOLD_S)
            self.held_text = text
            self.hold_task = asyncio.create_task(self._release_after(HOLD_S))
            return
        await self.finish_turn(text)

    async def _release_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        text, self.held_text, self.hold_task = self.held_text, "", None
        await self.finish_turn(text)

    async def finish_turn(self, text: str) -> None:
        await self.send(type="vad", state="end")
        await self.send(type="transcript", role="user", text=text)
        if not text:
            await self.send(type="state", state="listening")
            return
        async with self.busy:
            if self.merge_pending:           # the reply they talked over is gone from the conversation
                self.merge_pending = False
                self.llm.drop_last_exchange()
            self.last_turn = (text, time.perf_counter())
            await self.respond(text)

    # ---- JSON messages -------------------------------------------------------------

    async def handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "reset":
            self.cancel()
            self.llm.reset()
            return
        if kind == "check":               # pre-flight from the page: can this TTS provider be used right now?
            tts = str(msg.get("tts") or "")[:20]
            ok, err = await asyncio.to_thread(self.speaker.check, tts)
            await self.send(type="check", id=msg.get("id"), tts=tts, ok=ok, error=err[:300])
            return
        if kind == "config":
            # interview setup from the page: system prompt (persona + JD + resume), model, patience
            if msg.get("system"):
                self.llm.set_system(str(msg["system"])[:24000])
            if msg.get("model"):
                self.llm.model = str(msg["model"])
            if msg.get("minutes"):
                self.pacer.minutes = float(msg["minutes"])
            if msg.get("kind"):
                self.pacer.kind = str(msg["kind"])
            self.llm.no_resume = bool(msg.get("no_resume"))
            if isinstance(msg.get("questions"), list):
                self.pacer.set_questions([str(q)[:400] for q in msg["questions"]])
            if msg.get("voice"):
                self.voice = str(msg["voice"])[:40]
            if "tts" in msg:
                self.tts_provider = str(msg.get("tts") or "")[:20] or None
            if "vocabulary" in msg:          # per session: the Whisper model is shared between clients
                self.vocab = str(msg.get("vocabulary") or "")[:800]
            if msg.get("patience") in PATIENCE:
                self.end_silence_s = PATIENCE[msg["patience"]]
                if self.turns:
                    self.turns.set_end_silence(self.end_silence_s)
            log.info("config: model=%s tts=%s patience=%s prompt=%d chars", self.llm.model,
                     self.tts_provider or "default", msg.get("patience"), len(self.llm.history[0]["content"]))
            return
        if kind == "playback":
            if self.turns:
                self.turns.avatar_speaking = bool(msg.get("playing"))
            return
        if kind == "end":                       # interview ended by the candidate
            self.cancel()
            if self.hold_task:
                self.hold_task.cancel()
                self.hold_task = None
            self.held_text = ""
            return
        if kind == "complete":                  # one-shot completion (feedback report)
            rid = msg.get("id")
            try:
                model = str(msg.get("model") or "") or None
                t0 = time.perf_counter()
                text = await asyncio.to_thread(self.llm.complete, str(msg.get("prompt", ""))[:60000],
                                               bool(msg.get("json", True)), 300, model)
                log.info("completion (%s) %.1fs", model or self.llm.model, time.perf_counter() - t0)
                await self.send(type="completion", id=rid, text=text)
            except Exception as e:
                log.exception("completion failed")
                await self.send(type="completion", id=rid, error=str(e))
            return
        if kind == "speak":                     # AI demo: voice the AI candidate's answer (no lip sync on the avatar)
            text, voice = str(msg.get("text", ""))[:3000], str(msg.get("voice") or "") or self.voice
            role = "interviewer" if msg.get("role") == "interviewer" else "candidate"
            sentences = [x for x in re.split(r"(?<=[.!?])\s+", text) if x.strip()]
            for i, sentence in enumerate(sentences):
                wav, cues, dur, env = await asyncio.to_thread(self.speaker.synthesize, sentence, voice,
                                                         "kokoro" if role == "candidate" and self.tts_provider == "piper" else self.tts_provider)   # Piper has one voice: the candidate needs a different one
                extra = {}
                if role == "interviewer":                # lip-synced: include neural lip-sync frames
                    frames = self._arkit_frames(wav)
                    if frames:
                        extra = {"arkit": frames, "arkit_fps": 30, "arkit_names": self.arkit.names}
                await self.send(type="say", role=role, text=sentence, duration=dur, visemes=cues, env=env,
                                env_fps=ENV_FPS, wav_b64=base64.b64encode(wav).decode(), last=i == len(sentences) - 1, **extra)
            return
        if kind == "nudge":                     # candidate silent: let the interviewer prompt them
            if self.busy.locked():
                return
            async with self.busy:
                await self.respond(str(msg.get("text", "(silence)"))[:500])
            return
        if kind in ("start", "text", "audio"):
            self.cancel()                      # a new turn supersedes any reply in flight
        async with self.busy:
            if kind == "start":
                self.pacer.start()
                await self.respond(None)
            elif kind == "text":
                text = msg.get("text", "").strip()
                if text:
                    await self.send(type="transcript", role="user", text=text)
                    await self.respond(text)
            elif kind == "audio":
                if self.transcriber is None:
                    await self.send(type="error", message="STT disabled (server started with --no-stt)")
                    return
                await self.send(type="state", state="thinking")
                wav = base64.b64decode(msg["wav_b64"])
                text = await asyncio.to_thread(self.transcriber.transcribe_wav, wav)
                await self.send(type="transcript", role="user", text=text)
                if text:
                    await self.respond(text)
                else:
                    await self.send(type="state", state="listening")
            else:
                await self.send(type="error", message=f"unknown message type {kind!r}")


def start_web(host: str, port: int) -> None:
    """Static HTTP for the browser client: / -> web/, /assets/ -> assets/."""
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    root = HERE.parent

    class Handler(SimpleHTTPRequestHandler):
        def translate_path(self, path):
            p = path.split("?", 1)[0]
            if not p.startswith("/assets/"):
                p = "/web" + p
            return super().translate_path(p)

        def end_headers(self):
            # Cross-origin isolation -> SharedArrayBuffer -> multi-threaded WASM for the in-browser engine
            # (Kokoro has to run on CPU: its WebGPU path produces muffled/NaN audio on some GPUs).
            # "credentialless" still allows CDN / Hugging Face downloads.
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Embedder-Policy", "credentialless")
            # Always revalidate: the browser keeps its copy but asks each load, and gets a 304 (no body) if the file is
            # unchanged. A day-long max-age on models kept showing a rebuilt avatar GLB as the old one until the cache
            # expired.
            self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer((host, port), functools.partial(Handler, directory=str(root)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log.info("web client on http://%s:%d", host, port)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--model", default="nemotron-3.5-lightning:30b", help="Ollama model name")
    p.add_argument("--ollama", default="http://localhost:11434")
    p.add_argument("--tts", default="auto", choices=["auto", "kokoro", "piper", "zonos"],
                   help="auto = Kokoro if voices/kokoro/model.onnx exists, else Piper; zonos = Zyphra ZONOS3-Mini (CPU server, see README)")
    p.add_argument("--kokoro-voice", default="am_michael", help="voices/kokoro/<name>.bin")
    p.add_argument("--voice", default=str(HERE / "voices/en_US-lessac-medium.onnx"), help="Piper voice")
    p.add_argument("--whisper", default="small.en")
    p.add_argument("--no-stt", action="store_true", help="text input only")
    p.add_argument("--no-neural-lipsync", action="store_true",
                   help="don't run wav2arkit (audio -> ARKit blendshapes); clients fall back to phoneme visemes")
    p.add_argument("--role", default="Software Engineer")
    p.add_argument("--web-port", type=int, default=8080,
                   help="serve web/ (and /assets) over HTTP on this port; 0 to disable")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "huggingface_hub", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.web_port:
        start_web(args.host, args.web_port)
    speaker = make_speaker(args.tts, args.voice, HERE / "voices/kokoro", args.kokoro_voice)
    arkit = None
    if not args.no_neural_lipsync:
        import arkit as arkit_mod
        arkit = arkit_mod.load(HERE / "models/wav2arkit")
    log.info("warming up %s ...", args.model)
    try:
        Interviewer(args.model, args.ollama).warm_up()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace").strip()
        if e.code == 404:
            log.error("Ollama returned 404 for model %r at %s. Check that Ollama is running and that the "
                      "model is installed (`ollama pull %s`). Response: %s",
                      args.model, args.ollama, args.model, detail or e.reason)
        else:
            log.error("Ollama warm-up failed (HTTP %s) at %s: %s", e.code, args.ollama, detail or e.reason)
        raise SystemExit(1) from None
    except (urllib.error.URLError, TimeoutError) as e:
        log.error("Cannot reach Ollama at %s: %s. Start Ollama or set --ollama to its base URL.",
                  args.ollama, getattr(e, "reason", e))
        raise SystemExit(1) from None
    transcriber = None
    if not args.no_stt:
        from stt import Transcriber
        transcriber = Transcriber(args.whisper)

    async def handler(ws):
        log.info("client connected")
        session = Session(ws, args, speaker, transcriber, arkit)
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    session.on_audio(raw)
                    continue
                msg = json.loads(raw)
                if msg.get("type") != "playback":
                    log.info("<- %s", msg.get("type"))
                asyncio.create_task(session.handle(msg))
        except websockets.ConnectionClosed:
            pass
        session.cancel()
        log.info("client disconnected")

    async with websockets.serve(handler, args.host, args.port, max_size=32 * 2**20):
        log.info("listening on ws://%s:%d", args.host, args.port)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
