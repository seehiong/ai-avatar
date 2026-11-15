# Feasibility benchmark: MuseTalk v1.5 real-time loop (UNet + VAE decode) on an AMD GPU via ROCm.
# Setup (gfx1151 / Strix Halo):
#   conda create -n avatar-gpu python=3.10 && conda activate avatar-gpu
#   pip install --index-url https://rocm.nightlies.amd.com/v2/gfx1151/ torch torchvision torchaudio
#   pip install diffusers==0.33.1 transformers==4.51.3 accelerate safetensors
#   weights: ~/models/musetalk/{unet.pth,musetalk.json} from TMElyralab/MuseTalk (MIT); sd-vae-ft-mse + whisper-tiny via HF
# Run: TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 python musetalk_bench.py

import json, os, time, glob, torch
from diffusers import UNet2DConditionModel, AutoencoderKL
from transformers import WhisperModel
W = os.path.expanduser("~/models/musetalk"); HF = os.path.expanduser("~/models/hf")
dev, dt = "cuda", torch.float16
unet = UNet2DConditionModel(**{k: v for k, v in json.load(open(f"{W}/musetalk.json")).items() if not k.startswith("_")})
sd = torch.load(f"{W}/unet.pth", map_location="cpu"); missing, unexpected = unet.load_state_dict(sd, strict=False)
print(f"UNet params {sum(p.numel() for p in unet.parameters())/1e6:.0f}M | missing {len(missing)} unexpected {len(unexpected)}")
unet = unet.to(dev, dt).eval()
vae = AutoencoderKL.from_pretrained(glob.glob(f"{HF}/models--stabilityai--sd-vae-ft-mse/snapshots/*")[0]).to(dev, dt).eval()
whisper = WhisperModel.from_pretrained(glob.glob(f"{HF}/models--openai--whisper-tiny/snapshots/*")[0]).encoder.to(dev, dt).eval()
def bench(fn, n=10):
    for _ in range(3): fn()
    torch.cuda.synchronize(); t = time.time()
    for _ in range(n): fn()
    torch.cuda.synchronize(); return (time.time() - t) / n
with torch.no_grad():
    mel = torch.randn(1, 80, 3000, device=dev, dtype=dt)            # whisper input (30 s window)
    s = bench(lambda: whisper(mel)); print(f"whisper-tiny encoder (30 s window, once per utterance): {s*1000:.0f} ms")
    for B in (1, 4, 8, 16):
        lat = torch.randn(B, 8, 32, 32, device=dev, dtype=dt)       # masked target + reference latents (256x256 face)
        aud = torch.randn(B, 50, 384, device=dev, dtype=dt)         # audio features per frame
        t0 = torch.zeros(B, device=dev, dtype=torch.long)
        su = bench(lambda: unet(lat, t0, encoder_hidden_states=aud).sample)
        sv = bench(lambda: vae.decode(torch.randn(B, 4, 32, 32, device=dev, dtype=dt) / 0.18215).sample)
        tot = su + sv
        print(f"batch {B:2d}: UNet {su*1000:6.1f} ms + VAE decode {sv*1000:6.1f} ms = {tot*1000:6.1f} ms  ->  {B/tot:5.1f} frames/s")
print("peak GPU memory (GB):", round(torch.cuda.max_memory_allocated() / 2**30, 2))
