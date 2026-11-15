"""faster-whisper speech-to-text (CPU int8; small.en transcribes ~5 s of speech in ~0.4 s)."""

from __future__ import annotations

import io
import os
import wave

import numpy as np


class Transcriber:
    def __init__(self, model_size: str = "small.en", device: str = "cpu", compute_type: str = "int8"):
        from faster_whisper import WhisperModel  # heavy import, keep lazy
        threads = min(16, os.cpu_count() or 4)
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type, cpu_threads=threads)

    vocabulary = ""   # names/jargon from the interview setup, e.g. "ShopCo, Kafka, PostgreSQL"

    def transcribe_pcm16k(self, pcm: np.ndarray, vocabulary: str | None = None) -> str:
        """16 kHz mono int16 (already VAD-trimmed, so no vad_filter pass).

        `vocabulary` primes Whisper with the names and jargon likely to come up (from the resume / JD),
        which fixes most mis-hearings of company names and technologies.
        """
        audio = pcm.astype(np.float32) / 32768.0
        vocab = self.vocabulary if vocabulary is None else vocabulary
        prompt = f"Interview answer. Terms: {vocab}." if vocab else None
        segments, _ = self.model.transcribe(audio, language="en", beam_size=1, vad_filter=False,
                                            condition_on_previous_text=False, initial_prompt=prompt)
        return " ".join(s.text.strip() for s in segments).strip()

    def transcribe_wav(self, wav_bytes: bytes) -> str:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            sr, ch = w.getframerate(), w.getnchannels()
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if ch > 1:
            audio = audio.reshape(-1, ch).mean(axis=1)
        if sr != 16000:  # simple linear resample; good enough for speech
            n = int(len(audio) * 16000 / sr)
            audio = np.interp(np.linspace(0, len(audio), n, endpoint=False), np.arange(len(audio)), audio)
        return self.transcribe_pcm16k(audio.astype(np.int16))
