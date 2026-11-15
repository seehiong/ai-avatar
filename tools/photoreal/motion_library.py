# Motion library for the photoreal avatar: many short LivePortrait clips of the portrait, each starting and
# ending at the neutral pose, so the live service can chain them in any order (a random "motion graph").
# A fixed loop is predictable - you learn when she glances or blinks; random clips with random blink
# times are not. Emotion clips (warm, curious, ...) match the interviewer's [emotion] tags and are chosen
# per sentence; listening clips (nods) play while the candidate talks; thinking clips while the LLM works.
#
# Run once per portrait (~12 min on a Radeon 8060S; LivePortrait ~0.23 s/frame + landmarks):
#   TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 python motion_library.py [--photo assets/ai-interviewer.png] [--sheet]
# Output: ~/models/musetalk/avatars/<photo>/library.npz (JPEG frames + landmarks + clip table), ~25 MB,
# a local build artifact (not committed). photoreal_server.py uses it automatically when present.

import argparse, math, os, sys, time
import cv2, numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from liveportrait_motion import Puppet, load_photo, FPS, REPO

OUT_W, OUT_H = 1280, 720
# kind: (variants, seconds)
CLIPS = {"idle": (8, 2.4), "talk": (8, 2.0), "listen": (4, 2.0), "thinking": (3, 3.0),
         "warm": (2, 2.8), "encouraging": (2, 2.8), "curious": (2, 2.8), "impressed": (2, 2.8),
         "skeptical": (2, 2.8), "amused": (2, 2.8)}
LIB_DIR = os.path.expanduser("~/models/musetalk/avatars")


def clip_track(kind, n, rng, eye_open):
    """Per-frame controls for one clip of n frames. Everything is 0 (neutral) at both ends with zero
    velocity (sin^2 envelopes), so clips join seamlessly in any order."""
    t = np.arange(n) / (n - 1)
    win = np.sin(math.pi * t) ** 2
    z = lambda: np.zeros(n)
    tr = {k: z() for k in ("pitch", "yaw", "roll", "brow", "smile", "gaze_x", "gaze_y")}
    blink = z()

    def drift(amp):                     # slow, random head motion, zero at the ends
        f1, f2 = rng.uniform(0.5, 1.5), rng.uniform(1.5, 3.0)
        return amp * win * (np.sin(2 * math.pi * f1 * t + rng.uniform(0, 6.3)) + 0.4 * np.sin(2 * math.pi * f2 * t + rng.uniform(0, 6.3)))

    def bump(center_s, width_s, amp):   # smooth pulse (nod, brow raise, glance)
        c, w = center_s * FPS, max(2.0, width_s * FPS)
        x = np.clip((np.arange(n) - (c - w / 2)) / w, 0, 1)
        return amp * np.sin(math.pi * x) ** 2

    def plateau(amp, ramp_s=0.45):      # ease in, hold, ease out (emotion expressions)
        r = int(ramp_s * FPS); env = np.ones(n)
        env[:r] = np.sin(np.linspace(0, math.pi / 2, r)) ** 2
        env[-r:] = env[:r][::-1]
        env[:3] = 0; env[-3:] = 0
        return amp * env

    def at(lo=0.15, hi=0.85):          # random time inside the clip (seconds), away from the joins
        return rng.uniform(lo, hi) * n / FPS

    tr["yaw"] += drift(rng.uniform(1.2, 2.4)); tr["pitch"] += drift(rng.uniform(0.6, 1.3)); tr["roll"] += drift(rng.uniform(0.5, 1.1))
    sign = rng.choice([-1, 1])
    if kind == "idle":
        if rng.random() < 0.55:                                       # glance away and back
            d = rng.uniform(0.4, 0.9)
            tr["gaze_x"] += bump(at(0.3, 0.7), d, rng.uniform(7, 12) * sign)
            tr["gaze_y"] += bump(at(0.3, 0.7), d, rng.uniform(-2, 3))
        if rng.random() < 0.3:
            tr["brow"] += bump(at(), rng.uniform(0.6, 1.0), rng.uniform(6, 10))
    elif kind == "talk":                                              # speakers nod and lift brows on emphasis
        for _ in range(rng.integers(1, 3)):
            tr["pitch"] += bump(at(), rng.uniform(0.35, 0.55), rng.uniform(1.5, 3.0))
        if rng.random() < 0.7:
            tr["brow"] += bump(at(), rng.uniform(0.4, 0.8), rng.uniform(9, 16))
        if rng.random() < 0.25:
            tr["gaze_x"] += bump(at(0.3, 0.7), rng.uniform(0.3, 0.5), rng.uniform(5, 8) * sign)
    elif kind == "listen":                                            # attentive nods, slight smile
        for _ in range(rng.integers(1, 3)):
            tr["pitch"] += bump(at(), rng.uniform(0.4, 0.6), rng.uniform(2.5, 4.0))
        tr["smile"] += bump(n / FPS / 2, n / FPS * 0.8, rng.uniform(0.25, 0.4))
        tr["roll"] += plateau(rng.uniform(1, 2.5) * sign)
    elif kind == "thinking":                                          # look up and aside, brows knit
        tr["gaze_x"] += plateau(rng.uniform(14, 20) * sign, 0.35)
        tr["gaze_y"] += plateau(rng.uniform(10, 16), 0.35)
        tr["brow"] += plateau(rng.uniform(-10, -7))
        tr["roll"] += plateau(rng.uniform(1.5, 3) * -sign)
    elif kind == "warm":
        tr["smile"] += plateau(rng.uniform(0.55, 0.75)); tr["roll"] += plateau(rng.uniform(3, 4) * sign); tr["brow"] += plateau(rng.uniform(4, 7))
    elif kind == "encouraging":
        tr["smile"] += plateau(rng.uniform(0.45, 0.6))
        for c in (0.3, 0.6):
            tr["pitch"] += bump(c * n / FPS, 0.45, rng.uniform(2.5, 3.5))
    elif kind == "curious":
        tr["brow"] += plateau(rng.uniform(16, 22)); tr["roll"] += plateau(rng.uniform(4, 6) * sign); tr["yaw"] += plateau(rng.uniform(1.5, 2.5) * sign)
    elif kind == "impressed":
        tr["brow"] += plateau(rng.uniform(18, 24), 0.3); tr["smile"] += plateau(rng.uniform(0.4, 0.55))
        tr["pitch"] += bump(at(0.4, 0.6), 0.5, 3.0)
    elif kind == "skeptical":
        tr["brow"] += plateau(rng.uniform(-16, -11)); tr["yaw"] += plateau(rng.uniform(3, 5) * sign)
        tr["pitch"] += plateau(rng.uniform(-3.5, -2)); tr["smile"] += plateau(-0.2)
        blink += plateau(0.35)                                        # slight squint
    elif kind == "amused":
        tr["smile"] += plateau(rng.uniform(0.85, 1.05)); tr["roll"] += plateau(rng.uniform(3, 4) * sign)
        tr["pitch"] += bump(at(0.3, 0.5), 0.4, 2.0)

    # blinks at random times (natural rate ~15-20/min; fewer while thinking), never at the joins
    rate = {"thinking": 0.15, "talk": 0.4}.get(kind, 0.3)
    shape = np.array([.35, .8, 1, 1, .75, .45, .2, .05])
    s = int(rng.uniform(0.2, 1.2) * FPS)
    while s < n - len(shape) - 2:
        if rng.random() < rate:                                       # chance per ~1 s step
            blink[s:s + len(shape)] = np.maximum(blink[s:s + len(shape)], shape)
            s += int(rng.uniform(1.6, 3.5) * FPS)
        else:
            s += int(rng.uniform(0.6, 1.2) * FPS)
    tr["eyes"] = eye_open * (1 - np.clip(blink, 0, 1))
    return tr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--photo", default=f"{REPO}/assets/ai-interviewer.png")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--sheet", default=None, help="only render one mid-clip frame per kind to this jpg")
    a = ap.parse_args()
    img = cv2.resize(load_photo(a.photo), (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)
    pup = Puppet(img)
    rng = np.random.default_rng(a.seed)

    if a.sheet:
        tiles = []
        for kind in CLIPS:
            n = int(CLIPS[kind][1] * FPS); tr = clip_track(kind, n, rng, pup.eye_open)
            k = n // 2
            f = pup.pose(**{key: v[k] for key, v in tr.items()})[60:520, 420:860]
            f = cv2.resize(f, (330, 345)); cv2.putText(f, kind, (6, 22), cv2.FONT_HERSHEY_SIMPLEX, .65, (0, 0, 255), 2)
            tiles.append(f)
        rows = [np.hstack(tiles[i:i + 5]) for i in range(0, len(tiles), 5)]
        cv2.imwrite(a.sheet, np.vstack(rows)); print("wrote", a.sheet); return

    import face_alignment
    fa = face_alignment.FaceAlignment(face_alignment.LandmarksType.TWO_D, device="cuda")
    jpegs, lms, starts, lens, kinds = [], [], [], [], []
    total = sum(v * int(s * FPS) for v, s in CLIPS.values()); t0 = time.time()
    for kind, (variants, secs) in CLIPS.items():
        for _ in range(variants):
            n = int(secs * FPS); tr = clip_track(kind, n, rng, pup.eye_open)
            starts.append(len(jpegs)); lens.append(n); kinds.append(kind)
            for k in range(n):
                f = pup.pose(**{key: v[k] for key, v in tr.items()})
                lms.append(fa.get_landmarks_from_image(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))[0])
                jpegs.append(cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes())
            done = len(jpegs); el = time.time() - t0
            print(f"{kind:12s} clip {len(starts):2d}: {done}/{total} frames, ~{el / done * (total - done) / 60:.1f} min left", flush=True)
    lms = np.array(lms, dtype=np.float32)
    for s, n in zip(starts, lens):                                     # light smoothing inside each clip
        seg = lms[s:s + n].copy()
        lms[s + 1:s + n - 1] = (seg[:-2] + 2 * seg[1:-1] + seg[2:]) / 4
    offsets = np.cumsum([0] + [len(j) for j in jpegs]).astype(np.int64)
    stem = os.path.splitext(os.path.basename(a.photo))[0]
    os.makedirs(f"{LIB_DIR}/{stem}", exist_ok=True)
    out = f"{LIB_DIR}/{stem}/library.npz"
    np.savez(out, jpeg=np.frombuffer(b"".join(jpegs), dtype=np.uint8), offsets=offsets, landmarks=lms,
             clip_start=np.array(starts), clip_len=np.array(lens), clip_kind=np.array(kinds), fps=FPS)
    print(f"wrote {out}: {len(jpegs)} frames in {len(starts)} clips, {offsets[-1] / 2**20:.0f} MB, {(time.time()-t0)/60:.1f} min")


if __name__ == "__main__":
    main()
