"""MuseTalk v1.5 talking head over a portrait (or its LivePortrait idle loop) - shared by
musetalk_talk.py (offline mp4) and photoreal_server.py (live stream to the browser).

Reuses MuseTalk's (MIT) audio features + face-parsing mask; our face_alignment landmarks replace its
mmpose/DWPose preprocessing. Setup: see musetalk_bench.py, plus
  pip install librosa einops gdown websockets ; git clone https://github.com/TMElyralab/MuseTalk ~/models/musetalk/MuseTalk
  ~/models/musetalk/face-parse-bisent/{79999_iter.pth,resnet18-5c106cde.pth}  (see MuseTalk/download_weights.sh)
Run with TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 on AMD (about 1.5x faster attention).
"""

import glob, json, os, sys, time
import cv2, numpy as np, torch

W = os.path.expanduser("~/models/musetalk"); HF = os.path.expanduser("~/models/hf")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, f"{W}/MuseTalk")
from diffusers import UNet2DConditionModel, AutoencoderKL, AutoencoderTiny
from transformers import WhisperModel
from musetalk.utils.audio_processor import AudioProcessor
from musetalk.utils.blending import get_image_prepare_material
from musetalk.utils.face_parsing import FaceParsing
from musetalk.models.unet import PositionalEncoding

FPS = 25


class _FaceParsing(FaceParsing):
    """MuseTalk's face parser loads its weights from ./models/...; point it at ~/models/musetalk instead
    (no working-directory tricks or symlinks, which Windows restricts)."""

    def model_init(self, *args, **kwargs):
        # torch>=2.6 defaults torch.load to weights_only=True, which rejects the legacy-.tar resnet18
        # checkpoint (torchvision's own file, trusted); MuseTalk's resnet.py calls torch.load without the flag.
        load = torch.load
        torch.load = lambda *a, **k: load(*a, **{"weights_only": False, **k})
        try:
            return super().model_init(f"{W}/face-parse-bisent/resnet18-5c106cde.pth", f"{W}/face-parse-bisent/79999_iter.pth")
        finally:
            torch.load = load
DEFAULT_PHOTO = f"{REPO}/assets/ai-interviewer.png"


def even(img):
    return img[: img.shape[0] // 2 * 2, : img.shape[1] // 2 * 2]      # even size for yuv420p video


LIB_DIR = os.path.expanduser("~/models/musetalk/avatars")


class Talker:
    """Avatar frames are kept as JPEG bytes (a motion library is ~2,400 frames) and decoded on demand.
    `clips` = [(start, length, kind)]: one 'idle' clip for a loop or still, or the motion library's clips."""

    def __init__(self, photo=DEFAULT_PHOTO, still=False, batch=4, dev="cuda", library=True, decoder="sd"):
        """decoder: "sd" = Stable Diffusion VAE (sharpest; ~23 ms/frame on a Radeon 8060S) or
        "tiny" = TAESD (MIT, madebyollin/taesd; ~1.5 ms/frame, slightly softer teeth and lip edges)."""
        self.dev, self.dt, self.batch = dev, torch.float16, batch
        name = os.path.splitext(os.path.basename(photo))[0]
        stem, lib = f"{REPO}/assets/photoreal/{name}", f"{LIB_DIR}/{name}/library.npz"
        enc = lambda img: cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()
        if library and not still and os.path.exists(lib):       # motion library (motion_library.py)
            src, d = lib, np.load(lib)
            buf, off = d["jpeg"].tobytes(), d["offsets"]
            self.jpegs = [buf[off[i]:off[i + 1]] for i in range(len(off) - 1)]
            lms = d["landmarks"]
            self.clips = [(int(a), int(n), str(k)) for a, n, k in zip(d["clip_start"], d["clip_len"], d["clip_kind"])]
        elif still or not os.path.exists(f"{stem}_idle.mp4"):
            src, self.jpegs, lms = photo, [enc(even(cv2.imread(photo)))], np.load(f"{stem}_landmarks.npy")[None]
            self.clips = [(0, 1, "idle")]
        else:                                                    # single idle loop (liveportrait_motion.py)
            src, cap, self.jpegs = f"{stem}_idle.mp4", cv2.VideoCapture(f"{stem}_idle.mp4"), []
            while (ok := cap.read())[0]:
                self.jpegs.append(enc(ok[1]))
            lms = np.load(f"{stem}_idle_landmarks.npy")
            self.clips = [(0, len(self.jpegs), "idle")]
        self.H, self.W = self.frame(0).shape[:2]
        self.boxes = [self._box(l) for l in lms]
        print(f"avatar: {len(self.jpegs)} frames {self.W}x{self.H} in {len(self.clips)} clip(s) from {src}")

        t0 = time.time()
        unet = UNet2DConditionModel(**{k: v for k, v in json.load(open(f"{W}/musetalk.json")).items() if not k.startswith("_")})
        unet.load_state_dict(torch.load(f"{W}/unet.pth", map_location="cpu"), strict=False)
        self.unet = unet.to(dev, self.dt).eval()
        self.vae = AutoencoderKL.from_pretrained(glob.glob(f"{HF}/models--stabilityai--sd-vae-ft-mse/snapshots/*")[0]).to(dev, self.dt).eval()
        self.taesd = AutoencoderTiny.from_pretrained("madebyollin/taesd", cache_dir=HF).to(dev, self.dt).eval() \
            if decoder == "tiny" else None
        wdir = glob.glob(f"{HF}/models--openai--whisper-tiny/snapshots/*")[0]
        self.whisper = WhisperModel.from_pretrained(wdir).to(dev, self.dt).eval()
        self.ap = AudioProcessor(feature_extractor_path=wdir)
        self.pe = PositionalEncoding(d_model=384).to(dev)
        print(f"models loaded in {time.time()-t0:.1f}s")
        self._prep(src)

    def frame(self, k):
        return cv2.imdecode(np.frombuffer(self.jpegs[k], np.uint8), cv2.IMREAD_COLOR)

    def _box(self, lm):
        """MuseTalk's crop: from nose bridge (lm 29) mirrored up, down to the chin, +10 px margin (v1.5)."""
        mid, bottom = lm[29, 1], lm[:, 1].max()
        return int(lm[:, 0].min()), int(max(0, mid - (bottom - mid))), int(lm[:, 0].max()), int(min(self.H, bottom + 10))

    @torch.no_grad()
    def _prep(self, src):
        """Per avatar frame, once: reference latents [lower-half-masked crop, full crop] and the feathered
        jaw/mouth blend mask. Cached on disk (keyed by the source file's mtime)."""
        os.makedirs(f"{W}/cache", exist_ok=True)
        key = f"{W}/cache/{os.path.basename(src)}.{int(os.path.getmtime(src))}.v2.pt"
        if os.path.exists(key):
            d = torch.load(key, weights_only=True)
            self.refs, alphas = d["refs"].to(self.dev, self.dt), [a.numpy() for a in d["alphas"]]
        else:
            t1 = time.time()
            fp = _FaceParsing(left_cheek_width=90, right_cheek_width=90)
            enc = lambda t: self.vae.encode((t * 2 - 1).to(self.dev, self.dt)).latent_dist.mode() * self.vae.config.scaling_factor
            refs, alphas = [], []
            for k, (x1, y1, x2, y2) in enumerate(self.boxes):
                img = self.frame(k)
                crop = cv2.resize(img[y1:y2, x1:x2], (256, 256), interpolation=cv2.INTER_LANCZOS4)
                x = torch.from_numpy(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)).permute(2, 0, 1).float().div(255)[None]
                masked = x.clone(); masked[..., 128:, :] = 0
                refs.append(torch.cat([enc(masked), enc(x)], 1))          # [1, 8, 32, 32]
                mask, (cx, cy, _, _) = get_image_prepare_material(img, [x1, y1, x2, y2], mode="jaw", fp=fp)
                alphas.append(mask[y1 - cy:y2 - cy, x1 - cx:x2 - cx].copy())
            self.refs = torch.cat(refs)
            torch.save({"refs": self.refs.cpu(), "alphas": [torch.from_numpy(a) for a in alphas]}, key)
            print(f"avatar prep: {time.time()-t1:.1f}s (cached to {key})")
        self.alphas = [np.ascontiguousarray(a)[..., None] for a in alphas]     # uint8; float per frame in blend()

    @torch.no_grad()
    def features(self, audio16k: np.ndarray):
        """whisper-tiny features per video frame: [T, 50, 384]."""
        seg = 30 * 16000
        feats = [self.ap.feature_extractor(audio16k[i:i + seg], return_tensors="pt", sampling_rate=16000).input_features
                 for i in range(0, len(audio16k), seg)]
        return self.ap.get_whisper_chunk(feats, self.dev, self.dt, self.whisper, len(audio16k), fps=FPS)

    @torch.no_grad()
    def faces(self, chunks, plan, cancelled=lambda: False):
        """Yield (i, avatar_frame, 256x256 RGB mouth-region face) for each audio frame, batch by batch.
        plan[i] = avatar frame to animate for audio frame i."""
        for i in range(0, len(chunks), self.batch):
            if cancelled():
                return
            a = self.pe(chunks[i:i + self.batch].to(self.dev, self.dt)).to(self.dt)
            ks = plan[i:i + len(a)]
            pred = self.unet(self.refs[ks], torch.zeros(len(a), device=self.dev, dtype=torch.long), encoder_hidden_states=a).sample
            im = self.taesd.decode(pred).sample if self.taesd is not None else \
                self.vae.decode(pred / self.vae.config.scaling_factor).sample    # TAESD takes scaled latents
            out = ((im / 2 + 0.5).clamp(0, 1) * 255).permute(0, 2, 3, 1).byte().cpu().numpy()
            for j, f in enumerate(out):
                yield i + j, ks[j], f

    def warmup(self, seconds=1.5):
        """Run the whole per-sentence path once on noise. The first call of each new layer shape makes the
        GPU libraries search for kernels (MIOpen on ROCm), which otherwise slows the interview's first reply."""
        t0 = time.time()
        rng = np.random.default_rng(0)
        chunks = self.features((rng.standard_normal(int(16000 * seconds)) * 0.05).astype(np.float32))
        for _ in range(2):
            for i, k, face in self.faces(chunks, [j % len(self.jpegs) for j in range(len(chunks))]):
                if i == 0:
                    cv2.imencode(".jpg", self.blend(k, face))
        torch.cuda.synchronize()
        print(f"warm-up: {time.time()-t0:.1f}s")

    def blend(self, k, face):
        """Paste a generated face into avatar frame k (BGR, full size)."""
        x1, y1, x2, y2 = self.boxes[k]; al = self.alphas[k].astype(np.float32) * (1 / 255)
        face = cv2.resize(cv2.cvtColor(face, cv2.COLOR_RGB2BGR), (x2 - x1, y2 - y1), interpolation=cv2.INTER_LANCZOS4)
        frame = self.frame(k)
        frame[y1:y2, x1:x2] = (frame[y1:y2, x1:x2] * (1 - al) + face * al).astype(np.uint8)
        return frame
