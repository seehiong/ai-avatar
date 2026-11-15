# Make the interviewer portrait speak a WAV with MuseTalk v1.5 -> mp4 (offline test of talker.py).
# It talks over a LivePortrait idle loop (assets/photoreal/<photo>_idle.mp4: head drift, blinks, gaze, brows) if one
# has been made with liveportrait_motion.py, otherwise over the plain photo; --still always uses the plain photo.
# Run: TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 python musetalk_talk.py speech.wav out.mp4 [--still] [--photo assets/ai-interviewer.png]

import argparse, os, subprocess, time
import cv2, librosa, torch
from talker import Talker, FPS, DEFAULT_PHOTO

ap = argparse.ArgumentParser()
ap.add_argument("wav"); ap.add_argument("out")
ap.add_argument("--still", action="store_true")
ap.add_argument("--photo", default=DEFAULT_PHOTO)
ap.add_argument("--batch", type=int, default=4)
a = ap.parse_args()

tk = Talker(a.photo, a.still, a.batch)
audio, _ = librosa.load(a.wav, sr=16000)
t1 = time.time(); chunks = tk.features(audio); T = len(chunks)
print(f"{T} frames ({T/FPS:.1f}s audio), whisper {time.time()-t1:.2f}s")

torch.cuda.synchronize(); t2 = time.time()
plan = [j % len(tk.jpegs) for j in range(T)]           # play the avatar's frames in order
faces = list(tk.faces(chunks, plan))
torch.cuda.synchronize(); gen = time.time() - t2
print(f"generation: {gen:.2f}s -> {T/gen:.1f} fps (batch {a.batch})")

ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{tk.W}x{tk.H}",
                       "-r", str(FPS), "-i", "-", "-i", a.wav, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                       "-c:a", "aac", "-shortest", a.out], stdin=subprocess.PIPE)
t3 = time.time()
for i, k, f in faces:
    frame = tk.blend(k, f)
    if i == T // 2:
        cv2.imwrite(os.path.splitext(a.out)[0] + "_mid.png", frame)
    ff.stdin.write(frame.tobytes())
blend = time.time() - t3
ff.stdin.close(); ff.wait()
print(f"blending: {blend:.2f}s -> {T/blend:.0f} fps")
print("wrote", a.out)
