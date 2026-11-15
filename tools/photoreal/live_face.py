"""Live LivePortrait renderer: the portrait re-posed per frame from a small control vector (head, eyes,
brows, smile, gaze), fast enough to run during the interview instead of pre-rendered clips.

Speed on a Radeon 8060S: ~100 ms/frame stock (fp16 autocast), ~55 ms with explicit fp16 + torch.compile.
That is 18 fps, so the service renders the upper face every 2nd frame and crossfades the frame between
(the mouth is redrawn at 25 fps by MuseTalk anyway). Also provides what MuseTalk needs per frame: the mouth
crop box (tracked from LivePortrait's keypoint motion, no landmark detector) and the blend mask.
"""

import os, sys, time
import cv2, numpy as np, torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from liveportrait_motion import Puppet, load_photo, REPO            # noqa: E402  (imports LivePortrait)
from src.utils.camera import get_rotation_matrix                   # noqa: E402
from src.utils.crop import paste_back                              # noqa: E402

CONTROLS = ("pitch", "yaw", "roll", "blink", "brow", "smile", "gaze_x", "gaze_y", "jaw")


class LiveFace:
    def __init__(self, photo, size=(1280, 720), compile=True):
        img = cv2.resize(load_photo(photo), size, interpolation=cv2.INTER_AREA)
        self.pup = Puppet(img)
        self.w, self.info = self.pup.w, self.pup.info
        self.H, self.W = img.shape[:2]
        self.neutral = img
        dev = self.w.device
        self.wm = self.w.warping_module.half()
        self.sg = self.w.spade_generator.half()
        self.f16, self.x_s16 = self.pup.f_s.half(), self.pup.x_s.half()
        self.eye_open = self.pup.eye_open
        # face-crop scale: LivePortrait keypoints are in a normalised crop (~[-1,1]); M maps crop px -> photo px
        self.px_per_unit = float(np.linalg.norm(self.pup.M[:2, 0])) * 128 * 3.4   # 3.4: measured vs face_alignment
        self.landmarks = np.load(f"{REPO}/assets/photoreal/{os.path.splitext(os.path.basename(photo))[0]}_landmarks.npy") \
            * (self.W / load_photo(photo).shape[1])          # 68 landmarks on the neutral photo, at stream size
        if compile:
            try:
                import triton  # noqa: F401  (torch.compile's GPU backend; not bundled with Windows torch)
            except ImportError:
                print("triton not installed - running LivePortrait uncompiled (pip install triton-windows to speed up)", flush=True)
                compile = False
        if compile:
            t0 = time.time()
            self.wm = torch.compile(self.wm, mode="max-autotune-no-cudagraphs")
            self.sg = torch.compile(self.sg, mode="max-autotune-no-cudagraphs")
            self.render()                                   # triggers compilation
            print(f"LivePortrait compiled in {time.time()-t0:.0f}s", flush=True)

    @torch.no_grad()
    def keypoints(self, pitch=0., yaw=0., roll=0., blink=0., brow=0., smile=0., gaze_x=0., gaze_y=0., jaw=0.):
        p, i, dev = self.pup.p, self.info, self.w.device
        T = lambda v: torch.tensor(float(v), device=dev)
        R = get_rotation_matrix(i["pitch"] + pitch, i["yaw"] + yaw, i["roll"] + roll)
        exp = i["exp"].clone()
        if gaze_x or gaze_y:
            exp = p.update_delta_new_eyeball_direction(T(gaze_x), T(gaze_y), exp)
        if smile:
            exp = p.update_delta_new_smile(T(smile), exp)
        if brow:
            exp = p.update_delta_new_eyebrow(T(brow), exp)
        x_d = i["scale"] * (i["kp"] @ R + exp) + i["t"]
        if jaw > 1e-3:                                   # 0..1 -> lip-open ratio up to 0.32 (chin and cheeks follow)
            x_d = x_d + self.w.retarget_lip(self.pup.x_s, self.w.calc_combined_lip_ratio([[min(0.32, 0.32 * jaw)]], self.pup.lmk)).reshape(1, -1, 3)
        if blink > 1e-3:
            eyes = self.eye_open * (1 - min(1.0, blink))
            d = self.w.retarget_eye(self.pup.x_s, self.w.calc_combined_eye_ratio([[eyes]], self.pup.lmk))
            flip = torch.tensor([-1., 1., 1.], device=d.device)        # symmetric lids (see liveportrait_motion.py)
            d[0, 15], d[0, 16] = d[0, 11] * flip, d[0, 13] * flip
            x_d = x_d + d
        return self.w.stitching(self.pup.x_s, x_d)

    @torch.no_grad()
    def render(self, **controls):
        """Full-size BGR frame plus the 2D shift (px) of the face versus the neutral photo."""
        x_d = self.keypoints(**controls)
        out = self.sg(feature=self.wm(self.f16, kp_source=self.x_s16, kp_driving=x_d.half())["out"])
        face = self.w.parse_output(out.float())[0]
        frame = cv2.cvtColor(paste_back(face, self.pup.M, self.pup.img, self.pup.mask), cv2.COLOR_RGB2BGR)
        shift = ((x_d - self.pup.x_s)[0, :, :2].mean(0) * self.px_per_unit).tolist()
        return frame, shift

    def mouth_box(self, shift=(0., 0.)):
        """MuseTalk's crop box: the neutral landmarks' box, translated by the tracked face shift (same size
        every frame, so one blend mask fits all frames)."""
        if not hasattr(self, "_box0"):
            lm = self.landmarks
            mid, bottom = lm[29, 1], lm[:, 1].max()
            self._box0 = (int(lm[:, 0].min()), int(max(0, mid - (bottom - mid))), int(lm[:, 0].max()), int(min(self.H - 1, bottom + 10)))
        x1, y1, x2, y2 = self._box0
        dx = int(round(max(-x1, min(self.W - x2, shift[0]))))
        dy = int(round(max(-y1, min(self.H - y2, shift[1]))))
        return (x1 + dx, y1 + dy, x2 + dx, y2 + dy)


if __name__ == "__main__":                                       # benchmark + tracking check
    import face_alignment
    lf = LiveFace(f"{REPO}/assets/ai-interviewer.png")
    def bench(fn, n=20):
        for _ in range(3): fn()
        torch.cuda.synchronize(); t = time.time()
        for _ in range(n): fn()
        torch.cuda.synchronize(); return (time.time() - t) / n * 1000
    print(f"render(): {bench(lambda: lf.render(yaw=1.5, blink=0.3, brow=4)):.1f} ms/frame")
    f0, _ = lf.render(); f1, _ = lf.render(yaw=1.2, pitch=0.6)
    print(f"crossfade 1280x720: {bench(lambda: cv2.addWeighted(f0, 0.5, f1, 0.5, 0)):.2f} ms")
    fa = face_alignment.FaceAlignment(face_alignment.LandmarksType.TWO_D, device="cuda")
    for kw in (dict(yaw=4), dict(pitch=-3), dict(yaw=-3, pitch=2, roll=2)):
        frame, shift = lf.render(**kw)
        lm = fa.get_landmarks_from_image(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))[0]
        actual = (lm[27:36].mean(0) - lf.landmarks[27:36].mean(0)).tolist()     # nose region shift
        print(f"{kw}: predicted shift {np.round(shift, 1)}, measured {np.round(actual, 1)}")
