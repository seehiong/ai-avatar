"""Piper TTS -> (wav bytes, viseme cues, loudness envelope)."""

from __future__ import annotations

import io
import re
import logging
import wave
from pathlib import Path

import numpy as np
from piper import PiperVoice

from speechtext import ints_to_words, speakable
from visemes import cues_from_alignments, envelope, visemes_for_audio

log = logging.getLogger("avatar.tts")
logging.getLogger("phonemizer").setLevel(logging.ERROR)     # "words count mismatch" on every line: espeak noise, harmless
ENV_FPS = 50


class Speaker:
    def __init__(self, model_path: str | Path):
        try:  # real phoneme timings need the model patched in memory (onnx package)
            self.voice = PiperVoice.load(str(model_path), include_alignments=True)
            self.alignments = True
        except Exception as e:  # pragma: no cover
            log.warning("phoneme alignments unavailable (%s); using estimated timings", e)
            self.voice = PiperVoice.load(str(model_path))
            self.alignments = False

    def synthesize(self, text: str, voice: str | None = None) -> tuple[bytes, list[dict], float, list[float]]:
        """Return (16-bit mono WAV bytes, viseme cues, duration_s, envelope@ENV_FPS). `voice` ignored (Piper)."""
        text = speakable(text)              # "50,000" -> "50000", "$1.5M" -> "1 point 5 million dollars", ...
        chunks = list(self.voice.synthesize(text, include_alignments=self.alignments))
        if not chunks:
            return b"", [{"t": 0.0, "v": "X", "o": "sil"}], 0.0, []
        sr = chunks[0].sample_rate
        audio = np.concatenate([np.frombuffer(c.audio_int16_bytes, dtype=np.int16) for c in chunks])

        if self.alignments and all(c.phoneme_alignments for c in chunks):
            cues, offset = [], 0.0
            for c in chunks:
                part = cues_from_alignments(c.phoneme_alignments, sr, offset)
                if cues and part and cues[-1]["o"] == part[0]["o"]:
                    part = part[1:]
                cues += part
                offset += len(c.audio_int16_bytes) / 2 / sr
        else:
            phonemes = [p for sent in self.voice.phonemize(text) for p in sent + ["."]]
            cues = visemes_for_audio(phonemes, audio, sr)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(audio.tobytes())
        return buf.getvalue(), cues, len(audio) / sr, envelope(audio, sr, ENV_FPS)


class KokoroSpeaker:
    """Kokoro-82M (Apache-2.0) via ONNX Runtime, with exact per-phoneme durations.

    The stock ONNX export only returns the waveform; `ensure_duration_model` adds the duration
    predictor's output (/encoder/Clip: frames per input token, 600 samples @ 24 kHz each) as a
    second graph output, so lip sync gets real phoneme timings - same as Piper's alignments.
    """

    SR = 24000
    HOP = 600

    def __init__(self, model_dir: str | Path, voice: str = "am_michael", speed: float = 1.0):
        import onnxruntime as ort
        from kokoro_onnx.tokenizer import Tokenizer
        model_dir = Path(model_dir)
        path = ensure_duration_model(model_dir / "model.onnx")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 8
        self.session = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
        self.tok = Tokenizer()
        self.vocab = self.tok.vocab
        self.model_dir, self.default_voice = model_dir, voice
        self._styles: dict[str, np.ndarray] = {}
        self.styles = self.voice_styles(voice)
        self.speed = speed
        self.alignments = True

    VOICE_URL = "https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX/resolve/main/voices/{}.bin"

    def voice_styles(self, voice: str) -> np.ndarray:
        """Style vectors for a Kokoro voice (e.g. am_michael, af_heart); downloaded once if missing."""
        if voice not in self._styles:
            if not re.fullmatch(r"[a-z]{2}_[a-z]+", voice):
                raise ValueError(f"bad voice name {voice!r}")
            path = self.model_dir / f"{voice}.bin"
            if not path.exists():
                import urllib.request
                log.info("downloading Kokoro voice %s", voice)
                urllib.request.urlretrieve(self.VOICE_URL.format(voice), path)
            self._styles[voice] = np.fromfile(path, dtype=np.float32).reshape(-1, 1, 256)
        return self._styles[voice]

    def synthesize(self, text: str, voice: str | None = None) -> tuple[bytes, list[dict], float, list[float]]:
        text = speakable(text)              # "50,000" -> "50000", "$1.5M" -> "1 point 5 million dollars", ...
        try:
            styles = self.voice_styles(voice) if voice else self.styles
        except Exception as e:
            log.warning("voice %r unavailable (%s); using %s", voice, e, self.default_voice)
            styles = self.styles
        phonemes = self.tok.phonemize(text, "en-us")
        ids = self.tok.tokenize(phonemes)[:510]
        wav, dur = self.session.run(None, {
            "input_ids": np.array([[0, *ids, 0]], dtype=np.int64),
            "style": styles[min(len(ids), len(styles) - 1)],
            "speed": np.array([self.speed], dtype=np.float32),
        })
        audio = (np.clip(wav.reshape(-1), -1, 1) * 32767).astype(np.int16)
        frames = np.asarray(dur).reshape(-1).astype(int)
        kept = [c for c in phonemes if c in self.vocab][: len(ids)]   # tokenizer drops unknown symbols
        symbols = ["^"] + kept + ["$"]
        # Kokoro opens every clip with ~0.45 s of silence (the pad token); trim it to 50 ms so
        # sentences flow and the first reply starts sooner. Same for the tail.
        lead = max(0, int(frames[0]) * self.HOP - int(0.05 * self.SR))
        tail = max(0, int(frames[-1]) * self.HOP - int(0.08 * self.SR))
        audio = audio[lead: len(audio) - tail if tail else None]
        aligned = [_Al(p, int(n) * self.HOP) for p, n in zip(symbols, frames)]
        cues = cues_from_alignments(aligned, self.SR, offset_s=-lead / self.SR)
        for c in cues:
            c["t"] = max(0.0, c["t"])

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.SR)
            w.writeframes(audio.tobytes())
        return buf.getvalue(), cues, len(audio) / self.SR, envelope(audio, self.SR, ENV_FPS)


class _Al:  # minimal stand-in for piper's PhonemeAlignment
    __slots__ = ("phoneme", "num_samples")

    def __init__(self, phoneme, num_samples):
        self.phoneme, self.num_samples = phoneme, num_samples


def ensure_duration_model(model: Path) -> Path:
    out = model.with_name(model.stem + "_durations.onnx")
    if not out.exists():
        import onnx
        m = onnx.load(str(model))
        m.graph.output.append(onnx.helper.make_tensor_value_info("/encoder/Clip_output_0", onnx.TensorProto.FLOAT, None))
        onnx.save(m, str(out))
    return out


class Zonos3Speaker:
    """Zyphra ZONOS3-Mini (Apache-2.0) via its native ggml server (zonos3.cpp): ~3x faster than real time on the
    CPU here, 44.1 kHz, 27 preset voices. Runs outside this process, so no torch/GPU is needed in the backend.

    The server is https://github.com/Zyphra/ZONOS3-Mini (servers/ggml), built and given its GGUF files once (see the
    README). If $ZONOS3_URL doesn't answer, it is started from $ZONOS3_HOME (default ~/github/ZONOS3-Mini).
    Visemes are estimated from the audio (the server returns no phoneme timings); the neural lip sync still runs
    on the audio as usual.
    """

    # Kokoro voice names -> the closest ZONOS3 preset voice (labels as listed by GET /v2/speakers)
    VOICES = {"af": "Savannah en American F 1", "am": "Jett en energetic M 1",
              "bf": "Imogen en British F 1", "bm": "Rupert en British M 1"}

    def __init__(self):
        import os
        self.url = os.environ.get("ZONOS3_URL", "http://127.0.0.1:1919").rstrip("/")
        self.home = Path(os.environ.get("ZONOS3_HOME", Path.home() / "github" / "ZONOS3-Mini"))
        self._proc = None
        self._ids: dict[str, str] = {}
        self._tok = None
        try:
            from kokoro_onnx.tokenizer import Tokenizer
            self._tok = Tokenizer()
        except Exception:
            pass
        self._ensure_server()

    def _get(self, path: str, timeout: float = 2):
        import json
        import urllib.request
        with urllib.request.urlopen(self.url + path, timeout=timeout) as r:
            return json.load(r)

    def _ensure_server(self):
        import subprocess
        import time
        try:
            self._get("/v2/health")
        except Exception:
            ggml = self.home / "servers" / "ggml"
            exe, backbone = ggml / "build" / "zonos3-server", ggml / "gguf" / "backbone-ptq.gguf"
            if not exe.exists() or not backbone.exists():
                raise RuntimeError(f"ZONOS3 server not reachable at {self.url} and not built in {ggml} (see README)")
            port = self.url.rsplit(":", 1)[-1]
            log.info("Starting ZONOS3 server (%s, port %s) ...", exe, port)
            self._proc = subprocess.Popen([str(exe), str(backbone), "--tts-default-voices-dir", str(ggml / "gguf" / "voices"),
                                           "--port", port], cwd=ggml, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(60):
                time.sleep(0.5)
                try:
                    self._get("/v2/health"); break
                except Exception:
                    if self._proc.poll() is not None:
                        raise RuntimeError("ZONOS3 server exited during startup")
            else:
                raise RuntimeError("ZONOS3 server did not start within 30 s")
        self._ids = {s["label"]: s["id"] for s in self._get("/v2/speakers", 10)["speakers"]}
        log.info("TTS: ZONOS3-Mini at %s (%d voices)", self.url, len(self._ids))

    def _voice_id(self, voice: str | None) -> str | None:
        label = self.VOICES.get((voice or "af")[:2], self.VOICES["af"])
        return self._ids.get(label) or next(iter(self._ids.values()), None)

    def _phonemize(self, text: str) -> list[str]:
        if self._tok is not None:
            try:
                return list(self._tok.phonemize(text, "en-us"))
            except Exception:
                pass
        return list(text.lower())

    def synthesize(self, text: str, voice: str | None = None) -> tuple[bytes, list[dict], float, list[float]]:
        import json
        import urllib.request
        text = ints_to_words(speakable(text))     # ZONOS3 reads "40000" as "four zero zero zero zero"
        if not text.strip():
            return b"", [{"t": 0.0, "v": "X", "o": "sil"}], 0.0, []
        body = {"model": "zonos3", "input": text, "voice": self._voice_id(voice), "response_format": "wav"}
        req = urllib.request.Request(self.url + "/v1/audio/speech", json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            wav_bytes = r.read()
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            sr = w.getframerate()
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        cues = visemes_for_audio(self._phonemize(text), audio, sr)
        return wav_bytes, cues, len(audio) / sr, envelope(audio, sr, ENV_FPS)


class MultiSpeaker:
    """Router that holds the primary speaker and lazily initializes alternate TTS providers (kokoro, piper, zonos =
    ZONOS3-Mini)."""

    def __init__(self, default_kind: str, piper_voice: str | Path, kokoro_dir: str | Path, kokoro_voice: str):
        self.piper_voice = piper_voice
        self.kokoro_dir = kokoro_dir
        self.kokoro_voice = kokoro_voice
        self.speakers: dict[str, object] = {}
        self.default_kind = "kokoro" if (default_kind == "kokoro" or (default_kind == "auto" and (Path(kokoro_dir) / "model.onnx").exists())) else ("zonos" if default_kind == "zonos" else "piper")
        self.speakers[self.default_kind] = self._load(self.default_kind)

    def _load(self, kind: str):
        if kind == "kokoro":
            log.info("TTS: Kokoro (%s)", self.kokoro_voice)
            return KokoroSpeaker(self.kokoro_dir, self.kokoro_voice)
        if kind == "zonos":
            return Zonos3Speaker()
        log.info("TTS: Piper (%s)", Path(self.piper_voice).stem)
        return Speaker(self.piper_voice)

    def get(self, kind: str | None):
        k = kind if kind in ("kokoro", "piper", "zonos") else self.default_kind
        if k not in self.speakers:
            try:
                self.speakers[k] = self._load(k)
            except Exception as e:
                log.warning("TTS provider %r unavailable (%s); using %s", k, e, self.default_kind)
                return self.speakers[self.default_kind]
        return self.speakers[k]

    def check(self, kind: str | None) -> tuple[bool, str]:
        """Can provider `kind` be used? Loads it if needed (ZONOS3 starts its server). Unlike get(), a failure is
        reported instead of silently falling back, so the page can tell the learner before an interview starts."""
        k = kind if kind in ("kokoro", "piper", "zonos") else self.default_kind
        if k in self.speakers:
            return True, ""
        try:
            self.speakers[k] = self._load(k)
            return True, ""
        except Exception as e:
            return False, str(e)

    def synthesize(self, text: str, voice: str | None = None, tts_provider: str | None = None) -> tuple[bytes, list[dict], float, list[float]]:
        sp = self.get(tts_provider)
        wav, cues, dur, env = sp.synthesize(text, voice)
        return level_wav(wav), cues, dur, env


TARGET_RMS = 0.1                         # about -20 dBFS: every provider/voice plays at the same loudness


def level_wav(wav_bytes: bytes) -> bytes:
    """Scale a 16-bit mono WAV so its speech RMS (silence ignored) hits TARGET_RMS, peaks kept below clipping.
    Kokoro, Piper and ZONOS3 come out at very different levels, so the AI candidate sounded much louder."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            params, raw = w.getparams(), w.readframes(w.getnframes())
        if params.sampwidth != 2 or not raw:
            return wav_bytes
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
        loud = a[np.abs(a) > 0.01]
        if loud.size < 100:
            return wav_bytes
        gain = TARGET_RMS / float(np.sqrt(np.mean(loud ** 2)))
        gain = min(gain, 0.97 / max(1e-6, float(np.abs(a).max())), 8.0)
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setparams(params)
            w.writeframes((np.clip(a * gain, -1, 1) * 32767).astype(np.int16).tobytes())
        return out.getvalue()
    except Exception:
        return wav_bytes


def make_speaker(kind: str, piper_voice: str | Path, kokoro_dir: str | Path, kokoro_voice: str):
    return MultiSpeaker(kind, piper_voice, kokoro_dir, kokoro_voice)
