"""Turn Piper phonemes + audio into viseme cues and a loudness envelope.

Preferred path: `cues_from_alignments` uses Piper's real per-phoneme sample counts
(voice loaded with include_alignments=True; needs the `onnx` package). Each cue has
  t: start time (s), v: Rhubarb letter (2D sprites), o: Oculus viseme (3D blendshapes).

Fallback (`visemes_for_audio`): no alignments -> spread weighted phonemes over the voiced
regions of the audio (RMS envelope). Approximate.
"""

from __future__ import annotations

import numpy as np

# IPA symbol -> viseme (Rhubarb Lip Sync mouth letters A-H, X = rest)
_IPA_TO_VISEME: dict[str, str] = {}
for chars, vis in [
    ("mbp", "A"),
    ("kgtdszʃʒʧʤθðnŋhjɹrɾʔç", "B"),
    ("iɪeɛæ", "C"),
    ("aɑɐʌə", "D"),
    ("ɔɜɚɝ", "E"),
    ("uʊoɒw", "F"),
    ("fv", "G"),
    ("lɫ", "H"),
]:
    for c in chars:
        _IPA_TO_VISEME[c] = vis

_VOWEL_VISEMES = {"C", "D", "E", "F"}
_IGNORE = set("ˈˌːʲ̃ ")
_PAUSE = set(",.;:!?—…")


def phonemes_to_units(phonemes: list[str]) -> list[tuple[str, float]]:
    """IPA phoneme list -> [(viseme, weight)]; pauses become ('X', weight)."""
    units: list[tuple[str, float]] = []
    for p in phonemes:
        if p in _IGNORE:
            if p == "ː" and units and units[-1][0] != "X":  # length mark: stretch previous
                v, w = units[-1]
                units[-1] = (v, w * 1.5)
            continue
        if p in _PAUSE:
            units.append(("X", 1.5))
            continue
        v = _IPA_TO_VISEME.get(p)
        if v is None:
            continue
        units.append((v, 1.4 if v in _VOWEL_VISEMES else 1.0))
    return units


def voiced_segments(audio: np.ndarray, sr: int, win_ms: float = 10.0,
                    thresh_ratio: float = 0.06, min_gap_ms: float = 120.0
                    ) -> list[tuple[float, float]]:
    """Return [(start_s, end_s)] of non-silent regions."""
    win = max(1, int(sr * win_ms / 1000))
    n = len(audio) // win
    if n == 0:
        return []
    frames = audio[: n * win].astype(np.float32).reshape(n, win)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    thresh = rms.max() * thresh_ratio
    voiced = rms > thresh
    segs: list[list[float]] = []
    for i, on in enumerate(voiced):
        t = i * win / sr
        if on:
            if segs and t - segs[-1][1] <= min_gap_ms / 1000:
                segs[-1][1] = t + win / sr
            else:
                segs.append([t, t + win / sr])
    return [(a, b) for a, b in segs if b - a > 0.04]


# Rhubarb mouth letter -> primary Oculus viseme for fallback 3D lip-sync
_RHUBARB_TO_OCULUS = {
    "X": "sil", "A": "PP", "B": "DD", "C": "E", "D": "aa", "E": "O", "F": "U", "G": "FF", "H": "nn"
}


def build_timeline(units: list[tuple[str, float]], segments: list[tuple[float, float]],
                   total_s: float) -> list[dict]:
    """Distribute weighted viseme units across voiced segments.

    Returns [{"t": seconds, "v": viseme, "o": oculus_viseme}] cues, starting and ending with X/sil.
    """
    speech = [(v, w) for v, w in units if v != "X"]
    if not speech or not segments:
        return [{"t": 0.0, "v": "X", "o": "sil"}]

    voiced_total = sum(b - a for a, b in segments)
    weight_total = sum(w for _, w in speech)

    def voiced_to_real(x: float) -> tuple[float, int]:
        """Position along concatenated voiced time -> (real time, segment index)."""
        for i, (a, b) in enumerate(segments):
            if x < b - a or i == len(segments) - 1:
                return a + min(x, b - a), i
            x -= b - a
        raise AssertionError

    raw: list[tuple[float, str]] = []
    acc, prev_seg = 0.0, 0
    for v, w in speech:
        t, seg = voiced_to_real(voiced_total * acc / weight_total)
        if seg != prev_seg:  # close the mouth across silent gaps
            raw.append((segments[prev_seg][1], "X"))
            prev_seg = seg
        raw.append((t, v))
        acc += w
    raw.append((min(segments[-1][1], total_s), "X"))

    # merge repeats and drop flickers shorter than 40 ms
    cues: list[dict] = [{"t": 0.0, "v": "X", "o": "sil"}]
    for i, (t, v) in enumerate(raw):
        nxt = raw[i + 1][0] if i + 1 < len(raw) else t + 1.0
        if v == cues[-1]["v"] or (nxt - t < 0.04 and v != "X"):
            continue
        cues.append({"t": round(t, 3), "v": v, "o": _RHUBARB_TO_OCULUS.get(v, "sil")})
    return cues


def visemes_for_audio(phonemes: list[str], audio_int16: np.ndarray, sr: int) -> list[dict]:
    units = phonemes_to_units(phonemes)
    segs = voiced_segments(audio_int16, sr)
    return build_timeline(units, segs, len(audio_int16) / sr)


# --- exact timings from Piper alignments ------------------------------------------

# IPA -> Oculus viseme (sil PP FF TH DD kk CH SS nn RR aa E I O U)
_IPA_TO_OCULUS: dict[str, str] = {}
for chars, vis in [
    ("pbm", "PP"), ("fv", "FF"), ("θð", "TH"), ("tdɾʔ", "DD"), ("kgŋx", "kk"),
    ("ʃʒʧʤç", "CH"), ("sz", "SS"), ("nlɫ", "nn"), ("ɹrɚɝ", "RR"),
    ("aɑɐʌæ", "aa"), ("ɛeəɜ", "E"), ("iɪj", "I"), ("ɔoɒ", "O"), ("uʊw", "U"),
]:
    for c in chars:
        _IPA_TO_OCULUS[c] = vis

_OCULUS_TO_RHUBARB = {
    "sil": "X", "PP": "A", "FF": "G", "TH": "B", "DD": "B", "kk": "B", "CH": "B", "SS": "B",
    "nn": "H", "RR": "E", "aa": "D", "E": "C", "I": "C", "O": "E", "U": "F",
}
_MODIFIERS = set("ˈˌːʲ̃h")      # stress/length marks + breathy h: time flows into neighbours
_PAUSE_MIN_S = 0.09            # a word gap shorter than this keeps the mouth moving


def cues_from_alignments(alignments, sr: int, offset_s: float = 0.0) -> list[dict]:
    """Piper PhonemeAlignment list -> [{"t", "v", "o"}] with real phoneme start times."""
    cues: list[dict] = []
    t = offset_s
    for al in alignments:
        p, dur = al.phoneme, float(al.num_samples) / sr
        if p in _MODIFIERS:
            pass  # time just flows into the neighbouring phoneme
        elif p in _PAUSE or p in "^$_" or (p == " " and dur >= _PAUSE_MIN_S):
            _push(cues, t, "sil")
        elif p != " ":
            o = _IPA_TO_OCULUS.get(p)
            if o:
                _push(cues, t, o)
        t += dur
    _push(cues, t, "sil")
    return cues


def _push(cues: list[dict], t: float, o: str) -> None:
    if cues and cues[-1]["o"] == o:
        return
    if cues and abs(cues[-1]["t"] - t) < 1e-4:  # zero-length: replace
        cues.pop()
    cues.append({"t": round(t, 3), "o": o, "v": _OCULUS_TO_RHUBARB[o]})


def envelope(audio_int16: np.ndarray, sr: int, fps: int = 50) -> list[float]:
    """Loudness per 1/fps s, 0..1 (sqrt-compressed, normalised to the clip's 98th percentile)."""
    hop = sr // fps
    n = len(audio_int16) // hop
    if n == 0:
        return []
    frames = audio_int16[: n * hop].astype(np.float32).reshape(n, hop)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    ref = np.percentile(rms, 98) or 1.0
    env = np.sqrt(np.clip(rms / ref, 0, 1))
    return [round(float(x), 2) for x in env]
