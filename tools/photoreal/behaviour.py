"""Nonverbal behaviour for the photoreal interviewer: a continuous controller that turns the state of the
conversation (listening, thinking, speaking, idle), the speech audio and the sentence's emotion into per-frame
controls for live_face.LiveFace (head pitch/yaw/roll, blink, brow, smile, gaze). Everything eases through
springs, so nothing snaps. Timings follow what conversation research reports for people on video calls:

- Blinks: about 15/min listening, 20/min speaking, fewer (8/min) while thinking; irregular (gamma-like) gaps,
  never under 0.6 s; more likely at the end of a sentence and right after a gaze shift; ~12% are double blinks.
- Gaze: listeners hold eye contact nearly all the time; speakers look away briefly when they start a turn
  (planning) and glance aside occasionally, but look back when yielding the turn (end of a question).
  Thinking = look up and to the side for a moment with the brows slightly knit.
- Head: small emphasis nods on stressed syllables while speaking; backchannel nods when the candidate pauses.
- Brows: raise on stressed words and at the end of a question; the emotion sets a baseline (curious up,
  skeptical down).
- Smile: a social smile for warm/encouraging/amused sentences that eases in over ~0.5 s and fades over ~3 s;
  a faint smile while listening.
"""

import math
import random

FPS = 25
EMOTION = {   # baseline targets while that emotion is active: smile, brow, roll (deg), pitch (deg)
    "neutral": (0.0, 0, 0, 0), "warm": (0.55, 3, 2.0, 0), "encouraging": (0.45, 2, 0, 0.5),
    "curious": (0.1, 14, 3.5, -0.5), "impressed": (0.35, 18, 0, 0), "skeptical": (-0.15, -12, 0, -2.0),
    "thinking": (0.0, -6, 1.5, 0), "amused": (0.9, 2, 2.5, 0),
}
BLINK_SHAPE = [.3, .8, 1, 1, .7, .4, .15]          # 7 frames = 280 ms: fast close, slower open


class Spring:
    """Critically damped approach to a target (time constant tau, seconds)."""
    def __init__(self, value=0.0, tau=0.3):
        self.v, self.target, self.tau = value, value, tau

    def step(self, dt):
        self.v += (self.target - self.v) * (1 - math.exp(-dt / self.tau))
        return self.v


class Behaviour:
    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.s = {"pitch": Spring(tau=0.35), "yaw": Spring(tau=0.4), "roll": Spring(tau=0.5),
                  "brow": Spring(tau=0.18), "smile": Spring(tau=0.6), "gaze_x": Spring(tau=0.1), "gaze_y": Spring(tau=0.1)}
        self.mode, self.emotion = "idle", "neutral"
        self.t = 0.0                                      # behaviour clock (s)
        self.next_blink, self.blink_at = 2.0, None        # blink_at: frame counter into BLINK_SHAPE
        self.last_blink = -10.0
        self.drift_phase = [self.rng.uniform(0, 6.3) for _ in range(6)]
        self.glance_until, self.glance_next = 0.0, 3.0
        self.pulses = []                                  # (end_time, control, amplitude, duration)
        self.emotion_until = 0.0
        self.hold = False                                 # shadow mode: keep the pose (blinks only) while a sentence is prepared

    # ---- events from the conversation -------------------------------------------------------------
    def set_mode(self, mode):
        """idle | listening (candidate talking) | thinking (LLM working) | speaking"""
        if mode == self.mode:
            return
        self.mode = mode
        if mode == "thinking":                            # planning look: up and aside, brows knit
            self.glance(self.rng.choice([-1, 1]) * self.rng.uniform(12, 18), self.rng.uniform(8, 14), self.rng.uniform(0.9, 2.0))
            self.set_emotion("thinking", 2.5)
        elif mode == "listening":
            self.glance_until = 0                         # back to eye contact
            if self.emotion == "thinking":
                self.emotion = "neutral"
        elif mode == "idle":
            self.glance_until = 0

    def set_emotion(self, emotion, hold=3.0):
        if emotion in EMOTION:
            self.emotion, self.emotion_until = emotion, self.t + hold

    def candidate_paused(self):
        """The candidate stopped talking for a moment: a backchannel nod (sometimes a double), maybe a blink."""
        if self.rng.random() < 0.65:
            self.pulse("pitch", self.rng.uniform(2.0, 3.5), 0.5)
            if self.rng.random() < 0.3:
                self.pulse("pitch", self.rng.uniform(1.5, 2.5), 0.45, delay=0.5)
        self.blink_soon(0.15, 0.3)

    def blink_soon(self, delay=0.1, chance=1.0):
        """Ask for a blink now-ish (gaze shift, sentence end, candidate pause), keeping a 0.7 s gap from the last."""
        if self.rng.random() < chance and self.t - self.last_blink > 0.7:
            self.next_blink = min(self.next_blink, self.t + delay)

    def glance(self, x, y, seconds):
        self.s["gaze_x"].target, self.s["gaze_y"].target = x, y
        self.glance_until = self.t + seconds
        self.blink_soon(0.1, 0.5)                         # gaze shifts often come with a blink

    def pulse(self, control, amp, seconds, delay=0.0):
        self.pulses.append([self.t + delay, self.t + delay + seconds, control, amp])

    # ---- per-frame update --------------------------------------------------------------------------
    def tick(self, dt=1 / FPS):
        self.t += dt
        t, rng = self.t, self.rng
        base = EMOTION[self.emotion if t < self.emotion_until or self.mode == "speaking" else "neutral"]
        if t >= self.emotion_until and self.emotion != "neutral" and self.mode != "speaking":
            self.emotion = "neutral"
        # slow head drift (sum of sines, small; smaller while listening - people hold still when attending)
        amp = {"listening": 0.6, "thinking": 1.0, "speaking": 1.0, "idle": 1.0}[self.mode]
        ph = self.drift_phase
        if not self.hold:
            self.s["yaw"].target = amp * (1.6 * math.sin(0.23 * t + ph[0]) + 0.6 * math.sin(0.61 * t + ph[1]))
            self.s["pitch"].target = base[3] + amp * (0.9 * math.sin(0.31 * t + ph[2]) + 0.4 * math.sin(0.83 * t + ph[3]))
            self.s["roll"].target = base[2] + amp * 0.7 * math.sin(0.19 * t + ph[4])
            self.s["brow"].target = base[1]
            self.s["smile"].target = base[0] + (0.12 if self.mode == "listening" else 0.0)
        # gaze: eye contact unless a glance is running; occasional small glances while speaking or idle
        if self.hold:
            pass
        elif t >= self.glance_until:
            self.s["gaze_x"].target = self.s["gaze_y"].target = 0.0
            if self.mode in ("speaking", "idle") and t >= self.glance_next:
                if rng.random() < 0.5:
                    self.glance(rng.choice([-1, 1]) * rng.uniform(4, 8), rng.uniform(-2, 3), rng.uniform(0.3, 0.7))
                self.glance_next = t + rng.uniform(3.5, 7.0)
            elif self.mode in ("listening", "thinking"):
                self.glance_next = t + rng.uniform(4.0, 8.0)
        # pulses (nods, brow raises): sin^2 bumps added on top of the spring targets
        extra = {}
        for p in list(self.pulses):
            t0, t1, c, a = p
            if t > t1:
                self.pulses.remove(p)
            elif t >= t0:
                extra[c] = extra.get(c, 0) + a * math.sin(math.pi * (t - t0) / (t1 - t0)) ** 2
        out = {k: sp.step(dt) for k, sp in self.s.items()}
        for c, v in extra.items():                        # pulses are already smooth: add them after the spring
            out[c] += v
        # blinks: gamma-like gaps around the mode's rate, never under 0.6 s
        blink = 0.0
        if self.blink_at is not None:
            blink = BLINK_SHAPE[self.blink_at]
            self.blink_at += 1
            if self.blink_at >= len(BLINK_SHAPE):
                self.blink_at = None
                if rng.random() < 0.12:                   # double blink
                    self.next_blink = t + 0.25
        elif t >= self.next_blink:
            self.blink_at, self.last_blink = 0, t
            mean = 60 / {"listening": 15, "speaking": 20, "thinking": 8, "idle": 15}[self.mode]
            self.next_blink = t + max(0.6, rng.gammavariate(2.5, mean / 2.5))
        out["blink"] = blink
        return out

    # ---- speech ------------------------------------------------------------------------------------
    def plan_sentence(self, envelope, is_question=False, emotion=None, first_of_turn=False, accents=None, jaw=None):
        """Controls for every frame of a sentence, computed ahead of time so the frames can be generated before
        the audio plays. envelope: loudness per video frame (0..1). accents: frame indices of pitch accents
        (stressed syllables, from F0) - emphasis nods and brow raises land there; without them, loudness peaks
        are used. jaw: 0..1 per frame from the neural lip sync, so the chin moves with the speech. A question
        ends with raised brows and eye contact."""
        T = len(envelope)
        self.set_mode("speaking")
        if emotion:
            self.set_emotion(emotion, T / FPS + 3.0)
        if first_of_turn:                                 # planning look-away at the start of the turn
            self.glance(self.rng.choice([-1, 1]) * self.rng.uniform(6, 10), self.rng.uniform(2, 6), self.rng.uniform(0.4, 0.8))
        if accents is None:                               # fallback: loudness peaks, at least 0.35 s apart
            env = list(envelope)
            mean = sum(env) / max(1, T)
            accents, last = [], -100
            for i in range(1, T - 1):
                if env[i] >= env[i - 1] and env[i] > env[i + 1] and env[i] > 1.25 * mean and i - last >= 0.35 * FPS:
                    accents.append(i); last = i
        for i in accents:
            # people nod or lift their brows on roughly half of their pitch accents; brows lead the accent slightly
            r = self.rng.random()
            if r < 0.3:
                self.pulses.append([self.t + i / FPS - 0.1, self.t + i / FPS + 0.3, "pitch", self.rng.uniform(1.2, 2.4)])
            elif r < 0.6:
                self.pulses.append([self.t + i / FPS - 0.14, self.t + i / FPS + 0.32, "brow", self.rng.uniform(5, 11)])
            elif r < 0.7:                                 # a brow raise with a small nod together
                self.pulses.append([self.t + i / FPS - 0.14, self.t + i / FPS + 0.32, "brow", self.rng.uniform(4, 8)])
                self.pulses.append([self.t + i / FPS - 0.1, self.t + i / FPS + 0.3, "pitch", self.rng.uniform(0.8, 1.6)])
        if is_question and T > 0.8 * FPS:                 # turn-yielding: brows up, look at them
            self.pulses.append([self.t + (T - 0.8 * FPS) / FPS, self.t + T / FPS + 0.3, "brow", 9])
            self.pulses.append([self.t + (T - 0.6 * FPS) / FPS, self.t + T / FPS + 0.2, "roll", self.rng.choice([-1, 1]) * 2.0])
            self.glance_next = self.t + T / FPS + 1.0
        frames = []
        for i in range(T):
            if i == T - 2:                                # sentence ends are a favourite moment to blink
                self.blink_soon(0.05 + self.rng.random() * 0.2, 0.7)
            f = self.tick()
            f["jaw"] = float(jaw[i]) if jaw is not None and i < len(jaw) else 0.0
            frames.append(f)
        return frames
