"""Neural lip sync: speech audio -> 52 ARKit blendshape weights at 30 fps.

Model: wav2arkit_cpu (Apache-2.0, https://huggingface.co/myned-ai/wav2arkit_cpu): Wav2Vec2 audio encoder +
LAM Audio2Expression decoder (3DAIGC/LAM_audio2exp), fused into one ONNX graph. ~40 ms of CPU per second
of audio. Learned from captured faces, so it produces real coarticulation and the subtle lip
roll/press/funnel/stretch and cheek motion that a 15-viseme table can't.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

log = logging.getLogger("avatar.arkit")
MODEL_URL = "https://huggingface.co/myned-ai/wav2arkit_cpu/resolve/main/"
FPS = 30


class Audio2Arkit:
    def __init__(self, model_dir: str | Path):
        import onnxruntime as ort
        model_dir = Path(model_dir)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        self.session = ort.InferenceSession(str(model_dir / "wav2arkit_cpu.onnx"), opts,
                                            providers=["CPUExecutionProvider"])
        self.names: list[str] = json.load(open(model_dir / "config.json"))["blendshape_names"]

    def infer(self, audio_int16: np.ndarray, sr: int) -> list[list[float]]:
        """int16 mono audio at `sr` -> frames (30 fps) x 52 weights, rounded for the wire."""
        x = audio_int16.astype(np.float32) / 32768.0
        if sr != 16000:
            x = np.interp(np.arange(0, len(x), sr / 16000), np.arange(len(x)), x).astype(np.float32)
        out = self.session.run(None, {"audio_waveform": x[None, :]})[0][0]
        return np.round(np.clip(out, 0, 1), 3).tolist()


def load(model_dir: str | Path) -> Audio2Arkit | None:
    model_dir = Path(model_dir)
    if not (model_dir / "wav2arkit_cpu.onnx").exists():
        log.info("neural lip sync off: %s not found (run.sh downloads it)", model_dir)
        return None
    try:
        a = Audio2Arkit(model_dir)
        log.info("neural lip sync: wav2arkit (52 ARKit blendshapes @ %d fps)", FPS)
        return a
    except Exception as e:  # pragma: no cover
        log.warning("neural lip sync unavailable: %s", e)
        return None
