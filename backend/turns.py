"""Open-mic turn taking: streaming Silero VAD + end-of-turn detection.

Feed 16 kHz mono int16 PCM in any chunk size; get events back:
  ("speech_start", None)          candidate started talking (barge-in point)
  ("pause", pcm)                  short pause mid-turn -> good moment to transcribe speculatively
  ("turn_end", pcm)               candidate finished; pcm = whole utterance incl. pre-roll
  ("discard", None)               blip too short to be speech
"""

from __future__ import annotations

import numpy as np
from faster_whisper.vad import get_vad_model

SR = 16000
FRAME = 512                 # Silero's native frame at 16 kHz (32 ms)
CONTEXT = 64


class StreamingVAD:
    """Silero VAD with recurrent state carried across frames (true streaming)."""

    def __init__(self):
        self.session = get_vad_model().session
        self.reset()

    def reset(self):
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.ctx = np.zeros(CONTEXT, dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        x = np.concatenate([self.ctx, frame])[None, :].astype(np.float32)
        out, self.h, self.c = self.session.run(None, {"input": x, "h": self.h, "c": self.c})
        self.ctx = frame[-CONTEXT:]
        return float(np.asarray(out).reshape(-1)[0])


class TurnDetector:
    def __init__(self, end_silence_s: float = 0.55, pause_s: float = 0.3, min_speech_s: float = 0.25,
                 preroll_s: float = 0.3):
        self.vad = StreamingVAD()
        self.end_frames = int(end_silence_s * SR / FRAME)
        self.pause_frames = int(pause_s * SR / FRAME)
        self.min_speech_frames = int(min_speech_s * SR / FRAME)
        self.preroll = int(preroll_s * SR / FRAME)
        self.avatar_speaking = False   # raises the bar for barge-in (echo / backchannel noise)
        self._buf = np.zeros(0, dtype=np.int16)
        self._reset_turn()

    def set_end_silence(self, seconds: float) -> None:
        self.end_frames = int(seconds * SR / FRAME)

    def _reset_turn(self):
        self.in_speech = False
        self.frames: list[np.ndarray] = []      # utterance frames (int16)
        self.history: list[np.ndarray] = []     # recent frames for pre-roll
        self.run_on = 0                          # consecutive speech frames before start
        self.silence = 0
        self.speech_frames = 0
        self.paused_sent = False

    def feed(self, pcm: np.ndarray):
        self._buf = np.concatenate([self._buf, pcm])
        while len(self._buf) >= FRAME:
            frame, self._buf = self._buf[:FRAME], self._buf[FRAME:]
            yield from self._frame(frame)

    def _frame(self, frame: np.ndarray):
        p = self.vad(frame.astype(np.float32) / 32768.0)
        # while the avatar talks, require louder/longer speech before interrupting it
        thresh, start_frames = (0.8, 8) if self.avatar_speaking else (0.5, 3)

        if not self.in_speech:
            self.history = (self.history + [frame])[-self.preroll:]
            self.run_on = self.run_on + 1 if p >= thresh else 0
            if self.run_on >= start_frames:
                self.in_speech = True
                self.frames = list(self.history)
                self.speech_frames = self.run_on
                self.silence = 0
                yield ("speech_start", None)
            return

        self.frames.append(frame)
        if p >= 0.35:                       # hysteresis: lower bar to stay in speech
            self.speech_frames += 1
            self.silence = 0
            self.paused_sent = False
            return
        self.silence += 1
        if self.silence == self.pause_frames and not self.paused_sent:
            self.paused_sent = True
            yield ("pause", np.concatenate(self.frames))
        # adaptive patience: the longer someone has been explaining, the longer the pauses they make
        # mid-thought (+1 frame per ~0.4 s of speech, capped at +0.8 s)
        if self.silence >= self.end_frames + min(25, self.speech_frames // 12):
            pcm = np.concatenate(self.frames)
            enough = self.speech_frames >= self.min_speech_frames
            self._reset_turn()
            self.vad.reset()
            yield ("turn_end", pcm) if enough else ("discard", None)
