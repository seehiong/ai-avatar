# Bring a still, front-facing portrait to life before MuseTalk does the mouth: LivePortrait (MIT code; its InsightFace
# detector weights are non-commercial research only) re-poses the head and drives eyes, brows and a faint smile
# from procedural signals -> a seamless "idle" loop video + per-frame 68-point landmarks for MuseTalk's crop.
# Setup: git clone https://github.com/KwaiVGI/LivePortrait ~/models/LivePortrait
#        hf download KwaiVGI/LivePortrait --local-dir ~/models/LivePortrait/pretrained_weights
#        pip install onnxruntime tyro pykalman scikit-image imageio imageio-ffmpeg rich pyyaml ffmpeg-python lmdb
# Run:   python liveportrait_motion.py [--photo assets/ai-interviewer.png] [--seconds 12] [--seed 7] [--sheet]   (--sheet = one frame per control, to calibrate)

import argparse, math, os, subprocess, sys, time
import cv2, numpy as np, torch

LP = os.path.expanduser("~/models/LivePortrait")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, LP)
import types; sys.modules.setdefault("gradio", types.SimpleNamespace(Error=RuntimeError, Info=print))  # UI-only import
from src.config.inference_config import InferenceConfig
from src.config.crop_config import CropConfig
from src.gradio_pipeline import GradioPipeline
from src.config.argument_config import ArgumentConfig
from src.utils.camera import get_rotation_matrix
from src.utils.crop import prepare_paste_back, paste_back
from src.utils.retargeting_utils import calc_eye_close_ratio

FPS = 25


def load_photo(path):
    img = cv2.imread(path)
    return img[: img.shape[0] // 2 * 2, : img.shape[1] // 2 * 2]       # even size for yuv420p video


class Puppet:
    """The source portrait as a LivePortrait puppet: pose(...) -> full-resolution BGR frame."""

    def __init__(self, img_bgr):
        self.p = GradioPipeline(InferenceConfig(), CropConfig(), ArgumentConfig())
        self.w = self.p.live_portrait_wrapper
        self.img = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        crop = self.p.cropper.crop_source_image(self.img, self.p.cropper.crop_cfg)
        self.lmk, self.M = crop["lmk_crop"], crop["M_c2o"]
        self.mask = prepare_paste_back(self.w.inference_cfg.mask_crop, self.M, dsize=(self.img.shape[1], self.img.shape[0]))
        I = self.w.prepare_source(crop["img_crop_256x256"])
        self.info = self.w.get_kp_info(I)
        self.f_s = self.w.extract_feature_3d(I)
        self.x_s = self.w.transform_keypoint(self.info)
        self.eye_open = float(calc_eye_close_ratio(self.lmk[None]).mean())
        print(f"source pose p/y/r = {self.info['pitch'].item():.1f}/{self.info['yaw'].item():.1f}/{self.info['roll'].item():.1f}, "
              f"eyes-open ratio {self.eye_open:.2f}")

    @torch.no_grad()
    def pose(self, pitch=0., yaw=0., roll=0., eyes=None, brow=0., smile=0., gaze_x=0., gaze_y=0.):
        p, i, dev = self.p, self.info, self.w.device
        T = lambda v: torch.tensor(float(v), device=dev)
        R = get_rotation_matrix(i["pitch"] + pitch, i["yaw"] + yaw, i["roll"] + roll)
        exp = i["exp"].clone().to(dev)
        if gaze_x or gaze_y:
            exp = p.update_delta_new_eyeball_direction(T(gaze_x), T(gaze_y), exp)
        if smile:
            exp = p.update_delta_new_smile(T(smile), exp)
        if brow:
            exp = p.update_delta_new_eyebrow(T(brow), exp)
        x_d = i["scale"] * (i["kp"] @ R + exp) + i["t"]
        if eyes is not None and abs(eyes - self.eye_open) > 1e-3:
            d = self.w.retarget_eye(self.x_s, self.w.calc_combined_eye_ratio([[eyes]], self.lmk))
            # the retargeter closes the two eyes unevenly at in-between ratios (reads as a wink mid-blink):
            # mirror one eye's lid keypoints (11 upper, 13 lower) onto the other's (15, 16)
            flip = torch.tensor([-1., 1., 1.], device=d.device)
            d[0, 15], d[0, 16] = d[0, 11] * flip, d[0, 13] * flip
            x_d = x_d + d
        x_d = self.w.stitching(self.x_s, x_d)
        out = self.w.parse_output(self.w.warp_decode(self.f_s, self.x_s, x_d)["out"])[0]
        return cv2.cvtColor(paste_back(out, self.M, self.img, self.mask), cv2.COLOR_RGB2BGR)


def idle_track(n, seed, eye_open):
    """Per-frame controls for a seamless loop of n frames: slow periodic head drift, natural blinks,
    small gaze shifts back to the camera, occasional brow raises and a faint, slowly varying smile."""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / n                                          # 0..1 over the loop -> integer cycles loop cleanly
    wave = lambda amp, *cyc: amp * sum(w * np.sin(2 * math.pi * c * t + rng.uniform(0, 2 * math.pi))
                                       for c, w in zip(cyc, (1, .45, .25)))
    tr = dict(yaw=wave(2.2, 1, 3, 5), pitch=wave(1.3, 2, 5, 7), roll=wave(1.0, 1, 4))
    tr["smile"] = 0.12 + wave(0.08, 1, 2)

    def pulses(times, shape):                                     # add shaped bumps at frame times (wrapping)
        out = np.zeros(n)
        for s in times:
            for k, v in enumerate(shape):
                out[(s + k) % n] = max(out[(s + k) % n], v)
        return out

    def spaced(lo, hi):                                           # random event times lo..hi seconds apart
        out, f = [], rng.uniform(0.6, 1.5) * FPS
        while f < n - 0.6 * FPS:
            out.append(int(f)); f += rng.uniform(lo, hi) * FPS
        return out

    blink = pulses(spaced(2.2, 5.5), [.35, .8, 1, 1, .75, .45, .2, .05])     # ~300 ms: fast close, slower open
    tr["eyes"] = eye_open * (1 - blink)
    brow = np.zeros(n)
    for s in spaced(3.5, 7):
        L = int(rng.uniform(0.6, 1.2) * FPS)
        env = np.sin(np.linspace(0, math.pi, L)) ** 2 * rng.uniform(5, 9)
        for k in range(L):
            brow[(s + k) % n] = max(brow[(s + k) % n], env[k])
    tr["brow"] = brow
    gx, gy = np.zeros(n), np.zeros(n)                             # glance away briefly, mostly hold eye contact
    for s in spaced(2.5, 5):
        L, x, y = int(rng.uniform(0.4, 1.0) * FPS), rng.uniform(-7, 7), rng.uniform(-2, 3)
        ramp = np.minimum(1, np.minimum(np.arange(L) + 1, L - np.arange(L)) / 3)
        for k in range(L):
            gx[(s + k) % n], gy[(s + k) % n] = x * ramp[k], y * ramp[k]
    tr["gaze_x"], tr["gaze_y"] = gx, gy
    return tr


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=12)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--sheet", action="store_true")
    ap.add_argument("--sheet-out", default="controls_sheet.jpg")
    ap.add_argument("--photo", default=f"{REPO}/assets/ai-interviewer.png")
    a = ap.parse_args()
    img = load_photo(a.photo)
    stem = f"{REPO}/assets/photoreal/{os.path.splitext(os.path.basename(a.photo))[0]}"
    a.out = f"{stem}_idle.mp4"
    os.makedirs(os.path.dirname(stem), exist_ok=True)
    pup = Puppet(img)

    if a.sheet:
        tests = [("neutral", {}), ("blink", dict(eyes=0.0)), ("brow +8", dict(brow=8)), ("smile .5", dict(smile=.5)),
                 ("gaze x+7", dict(gaze_x=7)), ("yaw+4 pitch-3", dict(yaw=4, pitch=-3))]
        cx, cy = pup.M[:2, 2] + pup.M[:2, :2] @ np.array([256, 256])     # face-crop centre in the photo
        r = int(np.linalg.norm(pup.M[:2, 0]) * 230)
        y0, x0 = max(0, int(cy) - r), max(0, int(cx) - r)
        tiles = []
        for name, kw in tests:
            f = cv2.resize(pup.pose(**kw)[y0:int(cy) + r, x0:int(cx) + r], (400, 400))
            cv2.putText(f, name, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 0, 255), 2)
            tiles.append(f)
        cv2.imwrite(a.sheet_out, np.hstack(tiles)); print("wrote", a.sheet_out); sys.exit()

    n = int(a.seconds * FPS)
    tr = idle_track(n, a.seed, pup.eye_open)
    import face_alignment
    fa = face_alignment.FaceAlignment(face_alignment.LandmarksType.TWO_D, device="cuda")
    np.save(f"{stem}_landmarks.npy", fa.get_landmarks_from_image(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))[0])  # for --still
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                           "-s", f"{img.shape[1]}x{img.shape[0]}", "-r", str(FPS), "-i", "-",
                           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "16", a.out], stdin=subprocess.PIPE)
    lms, t0 = [], time.time()
    for k in range(n):
        f = pup.pose(**{key: v[k] for key, v in tr.items()})
        ff.stdin.write(f.tobytes())
        lms.append(fa.get_landmarks_from_image(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))[0])
    ff.stdin.close(); ff.wait()
    lms = np.array(lms, dtype=np.float32)
    # light temporal smoothing (wrapping) so MuseTalk's crop box doesn't jitter
    lms = (np.roll(lms, 1, 0) + 2 * lms + np.roll(lms, -1, 0)) / 4
    np.save(os.path.splitext(a.out)[0] + "_landmarks.npy", lms)
    print(f"{n} frames in {time.time()-t0:.1f}s ({n/(time.time()-t0):.1f} fps incl. landmarks) -> {a.out}")
