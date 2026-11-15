"""Question-bank pacing: make sure the interviewer actually gets to the planned questions.

Fast LLMs happily drill into one topic for the whole interview. The page sends the planned
questions (must-ask first, then early -> middle -> late); after every EVERY candidate answers
without a planned question being asked, the next one is attached to the candidate's turn as a
hidden note to the interviewer. A question counts as asked once a reply shares enough of its
content words.
"""

from __future__ import annotations

import re
import time

EVERY = 3
COVERED_MAX = 30                         # questions listed back to the interviewer as "already asked"
FALLBACKS = ["What's a technical decision you'd make differently today, and why?",
             "Tell me about a time you disagreed with a teammate on a design. How was it resolved?",
             "What's the hardest bug you've tracked down, and how did you find it?",
             "How do you decide what to test, and what not to?"]
_DESIGN = re.compile(r"\bhow would you (approach )?(design(ing)?|build(ing)?|architect(ing)?)\b|\bdesigning (a|an)\b|\bdesign (a|an|the|your)\b|\bsystem design\b", re.I)
_STOP = set("a an the and or but to of in on at for with what how why when where who you your yours me my i we us our "
            "is are was were be been do did does have has had that this it its as by from about tell time describe "
            "would could should if then so can will just".split())
_CHECKIN_RE = re.compile(
    r"[^.!?]*\b(would you like (me )?to|is there (a|any) (particular|specific)|does (that|this) (answer|align|make sense|help|cover)|"
    r"shall i|should i (go|dive|elaborate)|want me to|happy to (go|dive|elaborate))\b[^.!?]*\?\s*$",
    re.I,
)
_INVITE = re.compile(r"\b(questions? (do|would|might) you have|what would you like to ask|anything (else )?you'?d like to (ask|know))\b", re.I)
_I_WORDS = re.compile(r"\b(i|my|myself|i'd|i've|i'm)\b", re.I)
_WE_WORDS = re.compile(r"\b(we|our|ours|we've|we'd|team)\b", re.I)
_METRIC_WORDS = re.compile(
    r"(\b\d+(?:\.\d+)?\s*(?:%|x|ms|s|sec|seconds|min|minutes|hours|days|weeks|months|years|k|m|b|bn|gb|mb|tb|qps|rps|tps|users|requests|services|engineers)?\b"
    r"|\b(?:percent|million|billion|thousand|double|triple|half|latency|p99|p95|slo|sla)\b)",
    re.I,
)
_TRADEOFF_WORDS = re.compile(r"\b(because|trade-?off|instead|chose|decided|alternative|option|versus|vs|downside|risk|bottleneck|constraint)\b", re.I)


def is_clarifying_question(user_text: str) -> bool:
    """True when the candidate asked a short clarifying question instead of finishing a full answer."""
    raw = (user_text or "").strip()
    if not raw or raw.startswith("(silence"):
        return False
    cleaned = _CHECKIN_RE.sub("", raw).strip()
    if not cleaned.endswith("?"):
        return False
    return 2 <= len(cleaned.split()) < 45


def diagnose_answer(user_text: str, kind: str = "mixed") -> str | None:
    """Inspect a candidate answer for probing signals (weak attribution, missing metrics/trade-offs, brevity)."""
    raw = (user_text or "").strip()
    if not raw or raw.startswith("(silence"):
        return None
    if is_clarifying_question(raw):
        return (
            "the candidate asked a clarifying question. Answer it directly and concretely in 1-2 sentences "
            "(pick realistic numbers or constraints for this role), then let them continue their answer without changing topics."
        )
    cleaned = _CHECKIN_RE.sub("", raw).strip()
    words = re.findall(r"[A-Za-z0-9']+", cleaned)
    w_count = len(words)
    if w_count < 22:
        return "their answer was very brief — ask for a concrete example or ask them to walk through how it worked step by step before moving on."
    i_cnt = len(_I_WORDS.findall(cleaned))
    we_cnt = len(_WE_WORDS.findall(cleaned))
    if we_cnt >= 3 and we_cnt > i_cnt * 1.5:
        return "they used 'we/our' repeatedly without clear personal ownership — probe what THEY personally designed, decided, or implemented."
    if w_count >= 30 and not _METRIC_WORDS.search(cleaned) and not _TRADEOFF_WORDS.search(cleaned):
        return (
            "they described actions without concrete metrics or trade-offs — ask for measurable impact "
            "(scale, latency, error rate, timeline) or why they chose that approach over alternatives."
        )
    return None


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", text.lower()) if w not in _STOP and len(w) > 2}


NOTE = "\n\n(Interviewer note, not said by the candidate: {})"


class Pacer:
    def __init__(self, questions: list[str] | None = None):
        self.set_questions(questions or [])
        self.minutes = 20.0
        self.t0: float | None = None
        self.cues: set[str] = set()
        self.kind = "mixed"                  # interview type from the page; technical/mixed get one design question
        self.asked: list[str] = []

    def start(self, minutes: float | None = None) -> None:
        """Interview clock starts (the interviewer's opening turn)."""
        if minutes:
            self.minutes = float(minutes)
        self.t0, self.cues, self.asked, self.design_tries = time.monotonic(), set(), [], 0

    def time_note(self) -> str | None:
        """Time cues so the interviewer wraps up on schedule instead of running over."""
        if self.t0 is None:
            return None
        used = (time.monotonic() - self.t0) / 60
        left = max(0, round(self.minutes - used))
        frac = used / max(1.0, self.minutes)
        if frac >= 1.0:
            return ("the scheduled time is up. Close the interview now: if the candidate's last message is a question you have not answered yet, first answer it in 1-2 concrete sentences (never say you would love to answer but time is up); then thank the candidate, say what happens next, say goodbye, and end your last sentence with [end]. Do not ask any more questions.")
        if frac >= 0.85 and "questions" not in self.cues:
            self.cues.add("questions")
            return (f"about {left} minute(s) left. Do NOT ask another interview question. Say one short acknowledgement, "
                    "then ask exactly this: \"What questions do you have for me?\"")
        if frac >= 0.75 and "last" not in self.cues:
            self.cues.add("last")
            return f"about {left} minutes left: make this your last topic, then leave time for their questions."
        return None

    def set_questions(self, questions: list[str]) -> None:
        self.pending = [q for q in questions if q.strip()][:12]
        self.answers_since = 0
        self.answers = 0

    def note_reply(self, reply: str) -> None:
        """Mark a planned question as asked if the interviewer's reply covers it, and remember the question."""
        for q in re.findall(r"[^.!?]*\?", re.sub(r"\[\w+\]\s*", "", reply)):
            q = q.strip(" -—–")
            if len(q.split()) >= 4:
                self.asked.append(" ".join(q.split()[:30]))
        if _INVITE.search(reply):
            self.cues.add("closing")            # he invited their questions: the interview part is over
        rw = _words(reply)
        for q in list(self.pending):
            qw = _words(q)
            if qw and len(qw & rw) / len(qw) >= 0.4:
                self.pending.remove(q)
                self.answers_since = 0

    def dedupe(self, sentence: str) -> str:
        """A question that repeats one asked in an earlier turn (the model re-reads its list) -> a different one."""
        if not sentence.rstrip().endswith("?"):
            return sentence
        sw = _words(sentence)
        for q in self.asked:
            qw = _words(q)
            hit = len(qw & sw)
            if (len(qw) >= 4 and hit / len(qw) >= 0.75 and hit / max(1, len(sw)) >= 0.6) or \
                    (hit >= 6 and hit / min(len(qw), len(sw)) >= 0.65):
                new = self.pending[0] if self.pending else next((f for f in FALLBACKS if f not in self.asked), FALLBACKS[0])
                self.asked.append(new)
                return new
        return sentence

    def covered(self) -> str:
        """The questions already asked, so the model doesn't loop back to an old story or ask the same thing twice."""
        earlier = self.asked[:-1][-COVERED_MAX:]          # the latest question is the topic in progress
        if not earlier:
            return ""
        return (" Topics you already covered earlier (follow-ups on the current topic are fine, but never go back to "
                "these or ask about them again; when you move on, pick a NEW topic): " + " | ".join(earlier))

    def design_note(self) -> str | None:
        """Technical and mixed interviews for engineers should include one system-design discussion."""
        if self.kind not in ("technical", "mixed") or self.t0 is None or "design" in self.cues:
            return None
        frac = (time.monotonic() - self.t0) / 60 / max(1.0, self.minutes)
        if not 0.35 <= frac < 0.75:
            return None
        if any(_DESIGN.search(q) for q in self.asked):
            self.cues.add("design")
            return None
        self.design_tries = getattr(self, "design_tries", 0) + 1     # models sometimes ignore the cue: repeat it (3x)
        if self.design_tries >= 3:
            self.cues.add("design")
        return ("time for a system-design question. Say one short acknowledgement, then ask a NEW question that "
                "starts with the words \"How would you design\" - a service or component this team might own, not "
                "something they already built. Do not ask about their past projects in this turn. Use your next "
                "follow-ups on requirements, scale, data model, failure modes and trade-offs.")

    def design_followup(self) -> str | None:
        """Right after the system-design question: two follow-ups on it before any new topic (models tend to say
        "Thanks, let me switch to a different area" after a design answer)."""
        if "designfu" not in self.cues:
            if not self.asked or not _DESIGN.search(self.asked[-1]):
                return None
            self.cues |= {"design", "designfu"}
            self.design_left = 2
        if getattr(self, "design_left", 0) <= 0:
            return None
        self.design_left -= 1
        return ("the candidate is answering your system-design question. Do NOT switch topics yet: skip praise and ask "
                "ONE follow-up on the weakest or vaguest part of their design (requirements, scale numbers, data model, "
                "a specific failure mode, or a trade-off they skipped).")

    def steer(self, user_text: str) -> str:
        """Candidate answered: append a hidden note for the interviewer (pacing, diagnostics, plus what was already asked)."""
        clarifying = is_clarifying_question(user_text)
        if not clarifying:
            self.answers += 1
            self.answers_since += 1
        cov = self.covered()
        t = self.time_note()
        if t and ("time is up" in t or "minute(s) left" in t):       # time cues win over everything else
            return user_text + NOTE.format(t)
        if "closing" in self.cues:
            return user_text + NOTE.format(t if t and "time is up" in t else "you are in the closing Q&A. If the candidate asked a question, answer it in 2-3 concrete sentences and ask if they have anything else. If they say they have no more questions (or after their second question), close now: thank them, say what happens next, say goodbye, and end your last sentence with [end]. Never ask a new interview question now, and never answer a question they already asked earlier.")
        diag = diagnose_answer(user_text, self.kind)
        if clarifying and diag:
            parts = " ".join(p for p in (t, diag) if p).strip()
            return user_text + NOTE.format((parts + cov).strip())
        if not clarifying and (fu := self.design_followup()):
            self.answers_since = 0
            return user_text + NOTE.format(" ".join(p for p in (t, fu) if p) + cov)
        if d := self.design_note():
            self.answers_since = 0
            return user_text + NOTE.format(d + cov)
        if not self.pending or self.answers < 2 or self.answers_since < EVERY or (self.t0 and "last" in self.cues):
            parts = " ".join(p for p in (t, diag) if p).strip()
            msg = (parts + cov).strip()
            return user_text + (NOTE.format(msg) if msg else "")
        self.answers_since = 0
        return user_text + NOTE.format(
            (t + " " if t else "") + "you've spent enough time on this topic. Move directly to this new question "
            "(bridge briefly from the candidate's context or ask it straight — avoid stock transitions like 'switching gears' "
            f"or 'moving on', and never mention any list or note): \"{self.pending[0]}\"" + cov)
