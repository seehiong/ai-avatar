"""Interviewer persona on top of Ollama's streaming chat API (stdlib only)."""

from __future__ import annotations

import json
import random
import re
import urllib.request
from typing import Callable, Iterator

EMOTIONS = ("neutral", "warm", "curious", "impressed", "skeptical", "thinking", "encouraging", "amused")

DEFAULT_PERSONA = """You are Alex, a friendly but rigorous job interviewer on a video call.
Interview the candidate for the role: {role}.
Rules:
- Speak naturally, as spoken dialogue. No markdown, lists, emojis or stage directions.
- Keep each turn short: one or two sentences, then ask exactly one question.
- Vary your openings and transitions: ask the next question directly or bridge from what the candidate just said, and never repeat transition phrases ("Switching gears...", "Moving on...", "Out of curiosity...") or identical question openers across turns.
- Probe each topic in an escalating 4-stage arc (anchor question -> personal "I" ownership & concrete metrics -> failure mode or constraint pressure test -> reflection on what they'd do differently), at most 2 follow-ups before moving on.
- If the candidate asks a clarifying question, answer it directly in 1-2 concrete sentences with realistic constraints and return the floor to them.
- Start by greeting the candidate and asking them to introduce themselves.
- Begin EVERY sentence with your facial expression tag in square brackets, chosen from:
  {emotions}. Example: "[warm] Nice to meet you. [curious] What drew you to backend work?"
- If the candidate interrupts you, acknowledge it briefly and respond to what they said."""

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
NOTE_MARK = "\n\n(Interviewer note"          # hidden pacing notes appended to the candidate's turn (pacing.py)

# --- acknowledgement trimming (mirrored in web/persona.js: makeAckTrimmer) ----------------------------------
# Models praise and summarise every answer ("That's a substantial migration - moving Rails to Go while keeping
# billing up is no small feat.") even when the prompt forbids it. It sounds fake and leaks the verdict, so before
# the first question of a reply a long praising/summarising sentence becomes a short neutral acknowledgement.
_ACKS = ["Got it.", "Okay.", "Thanks.", "Understood.", "Makes sense.", "Right.", "", ""]
_EVAL = re.compile(r"\b(great|excellent|solid|clever|impressive|fantastic|amazing|awesome|brilliant|wonderful|terrific|"
                   r"outstanding|nice|smart|thoughtful|strong|healthy|insightful|self-aware|systematic|substantial|significant|"
                   r"clear|interesting|fair|valid|reasonable|compelling|important|helpful|good|"
                   r"no small feat|exactly (the|what|how)|really (good|important|interesting|helpful)|"
                   r"well (done|thought|put|handled)|love (that|how|the)|i like (that|how)|i appreciate|textbook|"
                   r"spot[- ]on|perfect|clean|elegant|neat|sensible|robust|pragmatic|confiden(ce|t)|impressed|great fit)\b", re.I)
# a short verdict is still a verdict: "That's a ... approach." / "Sounds like a ..."
_VERDICT = re.compile(r"^(that('s| is| was| sounds like) (a|an|really|very|quite|such)|sounds like (a|an))\b", re.I)
_ACK_START = re.compile(r"^(that('s| is| was| sounds| makes| must)|this (is|sounds)|it sounds|sounds like|so,? you|"
                        r"what (a|an)|wow|great|excellent|nice|good|perfect|absolutely|definitely)\b", re.I)
_CLOSING = re.compile(r"\b(talking|speaking|chatting|meeting) with you\b|\bthanks? (you )?for (your|joining|taking)|"
                      r"\bnext steps?\b|\bbe in touch\b|\bgoodbye\b", re.I)


_TRANSITION_SENT = re.compile(
    r"^(?:now\s+)?(?:(?:let'?s|i'?d like to|we can)\s+)?"
    r"(?:(?:switch|shift|change)(?:ing)?\s+gears(?:\s+(?:a little|a bit|slightly|for a (?:moment|second)|here|now))?"
    r"|(?:switch|change)(?:ing)?\s+(?:topics|tack)(?:\s+(?:a little|a bit|for a (?:moment|second)|here|now))?"
    r"|mov(?:e|ing)\s+on(?:\s+(?:to\s+(?:another|a different|the next)\s+(?:topic|area|subject|question)|for a (?:moment|second)))?"
    r"|step(?:ping)?\s+back(?:\s+(?:a little|a bit|for a (?:moment|second)))?"
    r"|pivot(?:ing)?(?:\s+(?:a little|a bit|slightly|to\s+(?:another|a different|the next)\s+(?:topic|area|subject)))?)[.!…]*$",
    re.I,
)
_TRANSITION_PREFIX = re.compile(
    r"^(?:(?:now|okay|alright|well|so),?\s+)?(?:(?:let'?s|to)\s+)?"
    r"(?:(?:switch|shift|change)(?:ing)?\s+gears(?:\s+(?:a little|a bit|slightly|for a (?:moment|second)|here|now))?"
    r"|(?:switch|change)(?:ing)?\s+(?:topics|tack)(?:\s+(?:a little|a bit|slightly|for a (?:moment|second)|here|now))?"
    r"|mov(?:e|ing)\s+on(?:\s+(?:to\s+(?:another|a different|the next|something else)(?:\s+(?:topic|area|subject|question))?|for a (?:moment|second)))?"
    r"|on\s+(?:another|a (?:different|separate|related))\s+note"
    r"|pivot(?:ing)?(?:\s+(?:a little|a bit|slightly|over))?"
    r"|step(?:ping)?\s+back(?:\s+(?:a little|a bit|for a (?:moment|second)))?"
    r"|(?:just\s+)?(?:out of curiosity|i'?m curious)"
    r"|(?:building|following up|piggybacking)\s+on\s+that"
    r"|(?:going|digging|diving)\s+(?:a (?:little|bit)\s+)?deeper(?:\s+(?:here|on that))?)\s*(?:,|—|–|:|\s+-|\.{2,}|…)\s+",
    re.I,
)


def _tx_key(phrase: str) -> str:
    p = phrase.lower()
    if re.search(r"\bgears\b", p):
        return "tx:gears"
    if re.search(r"\bmov(?:e|ing) on\b", p):
        return "tx:move"
    if re.search(r"\bnote\b", p):
        return "tx:note"
    if re.search(r"\bpivot", p):
        return "tx:pivot"
    if re.search(r"\bstep(?:ping)? back\b", p):
        return "tx:step"
    if re.search(r"\b(?:topics|tack)\b", p):
        return "tx:turn"
    if "curious" in p or "curiosity" in p:
        return "tx:curious"
    if "building" in p or "following up" in p or "piggybacking" in p:
        return "tx:build"
    if "deeper" in p:
        return "tx:deeper"
    return ""


INVITE = re.compile(r"\b(questions? (do|would|might) you have|what would you like to ask|anything (else )?you'?d like to (ask|know)|"
                    r"(any|other|more) questions)\b", re.I)
_READY_Q = re.compile(r"\b(do you have a (few|couple of) minutes|are you ready|shall we (get started|begin|dive in)|"
                      r"ready to (get started|begin|dive in))\b[^?]*\?\s*$", re.I)
_PRAISE_Q_SENT = re.compile(r"^[^.!?]{0,40}\b(great|good|excellent|fantastic|interesting|fair) question\b[^.!?]{0,20}[.!]?$", re.I)
_PRAISE_Q_PREFIX = re.compile(r"^(that'?s|what) an? (great|good|excellent|fantastic|interesting|fair) question[,.!—–-]*\s*", re.I)


class AckTrimmer:
    """`recent` (shared across turns) holds the last 2 acknowledgements plus transition families (`tx:*`,
    whole interview) so the interviewer never repeats 'Switching gears a little...' or 'Understood.' every turn."""

    def __init__(self, active: bool, recent: list[str] | None = None, reply_to_question: bool = False,
                 opening: bool = False):
        self.active, self.done, self.acked, self.closing = active, False, False, False
        self.recent = recent if recent is not None else []
        # answering the candidate's own question after inviting questions: no "great question", no question back
        self.opening = opening
        self.answering = reply_to_question and not active and "tx:invited" in self.recent

    def _use(self, ack: str) -> str:
        key = ack.lower().strip(" .!")
        if not key:
            return ""
        if key in self.recent:
            return ""                            # same as one of the last two: just go straight to the question
        tx = [x for x in self.recent if x.startswith("tx:")]
        acks = [x for x in self.recent if not x.startswith("tx:")][-1:] + [key]
        self.recent[:] = tx + acks
        return ack

    def _dedupe_transition(self, s: str) -> str:
        trimmed = s.strip()
        if _TRANSITION_SENT.match(trimmed):
            k = _tx_key(trimmed)
            if k and k in self.recent:
                return ""
            if k:
                self.recent.append(k)
            return s
        if m := _TRANSITION_PREFIX.match(trimmed):
            k = _tx_key(m.group(0))
            if k and k in self.recent:
                rest = trimmed[m.end():].lstrip()
                return (rest[0].upper() + rest[1:]) if rest else ""
            if k:
                self.recent.append(k)
        return s

    def __call__(self, s: str) -> str:
        s = self._dedupe_transition(s)
        if not s:
            return ""
        if self.opening and _READY_Q.search(s):
            return ""                            # opening: "do you have a few minutes?" is a second question
        if self.answering:
            if _PRAISE_Q_SENT.match(s.strip()):
                return ""
            s = _PRAISE_Q_PREFIX.sub("", s)
            if not s:
                return ""
            s = s[0].upper() + s[1:]
            if s.rstrip().endswith("?") and not INVITE.search(s) and not _CLOSING.search(s):
                return ""
        if INVITE.search(s) and s.rstrip().endswith("?") and "tx:invited" not in self.recent:
            self.recent.append("tx:invited")
        if _CLOSING.search(s) or re.match(r"thanks?( you)?\b", s, re.I):
            self.closing = True
        elif self.closing and (_EVAL.search(s) or _VERDICT.search(s)) and not s.rstrip().endswith("?"):
            return ""                               # a goodbye that praises the answers leaks the verdict too
        if not self.active or self.done:
            return s
        if s.rstrip().endswith("?"):
            self.done = True                    # the question itself is kept, whatever it starts with
            return s
        if _CLOSING.search(s):
            return s
        # praise of any length gives the verdict away ("That's a solid choice."); other short acks ("Got it.") stay
        if _EVAL.search(s) or _VERDICT.search(s) or (len(s.split()) > 6 and _ACK_START.search(s)):
            if self.acked:
                return ""
            self.acked = True
            return self._use(random.choice([a for a in _ACKS if a.lower().strip(" .!") not in self.recent]))
        if len(s.split()) <= 3:                 # the model's own short acknowledgement (one per reply)
            if self.acked:
                return ""
            self.acked = True
            return self._use(s)
        return s


# "Does that answer your question?" / "Would you like me to go deeper?" - a check-in, not a question to answer
_CHECKIN = re.compile(r"[^.!?]*\b(would you like (me )?to|is there (a|any) (particular|specific)|does (that|this) (answer|align|make sense|help|cover)|"
                      r"shall i|should i (go|dive|elaborate)|want me to|happy to (go|dive|elaborate))\b[^.!?]*\?\s*$", re.I)


def trims_acks(user_text: str | None) -> bool:
    """Trim only replies to an answer: not the opening, silence nudges, or replies to the candidate's own questions."""
    if not user_text or user_text.startswith("(silence"):
        return False
    said = _CHECKIN.sub("", user_text.split(NOTE_MARK)[0].strip())
    return "?" not in said
_TAG = re.compile(r"\[(\w+)\]")
_PAREN = re.compile(r"\s*\([^()]{30,}\)")       # parenthetical asides / leaked stage directions - never spoken


class Interviewer:
    def __init__(self, model: str, host: str = "http://localhost:11434",
                 role: str = "Software Engineer", persona: str = DEFAULT_PERSONA):
        self.model = model
        self.host = host.rstrip("/")
        self.recent_acks: list[str] = []
        self.history: list[dict] = [{"role": "system", "content": persona.format(
            role=role, emotions=", ".join(EMOTIONS))}]

    def warm_up(self) -> None:
        """Load the model into memory so the first real turn isn't slow."""
        # Use the same chat endpoint as interviews to warm the selected model.
        body = json.dumps({"model": self.model, "messages": [{"role": "user", "content": "Hi."}],
                           "stream": False, "think": False, "keep_alive": "30m",
                           "options": {"num_predict": 1}}).encode()
        req = urllib.request.Request(f"{self.host}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=300).read()

    def reset(self) -> None:
        self.history = self.history[:1]
        self.recent_acks.clear()

    def drop_last_exchange(self) -> str:
        """Remove the last candidate turn and anything after it (a reply they talked over because they
        weren't finished). Returns the removed candidate text (without any pacing note)."""
        for i in range(len(self.history) - 1, 0, -1):
            if self.history[i]["role"] == "user":
                text = self.history[i]["content"].split("\n\n(Interviewer note")[0]
                del self.history[i:]
                return text
        return ""

    no_resume = False      # the page sent no resume: never let the model claim to have read one
    question_filter = None  # set by the server: Pacer.dedupe

    def set_system(self, prompt: str) -> None:
        """Replace the persona/system prompt (interview setup from the page) and start fresh."""
        self.history = [{"role": "system", "content": prompt}]
        self.recent_acks.clear()

    def _messages(self) -> list[dict]:
        """History as sent: pacing notes are kept on the latest candidate turn only (they're per-turn
        instructions; repeating every old one would grow the prompt each turn)."""
        last = max((i for i, m in enumerate(self.history) if m["role"] == "user"), default=-1)
        return [m if i == last or m["role"] != "user" else {**m, "content": m["content"].split(NOTE_MARK)[0]}
                for i, m in enumerate(self.history)]

    def _stream(self) -> Iterator[str]:
        body = json.dumps({
            "model": self.model,
            "messages": self._messages(),
            "stream": True,
            "think": False,
            "keep_alive": "30m",
            "options": {"temperature": 0.7, "num_ctx": 32768},   # model default is 262k: a 26 GB footprint for nothing
        }).encode()
        req = urllib.request.Request(f"{self.host}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            for line in resp:
                if not line.strip():
                    continue
                msg = json.loads(line)
                if piece := msg.get("message", {}).get("content"):
                    yield piece
                if msg.get("done"):
                    break

    def complete(self, prompt: str, json_mode: bool = True, timeout: int = 300, model: str | None = None) -> str:
        """One-shot completion outside the interview history (e.g. the feedback report)."""
        body = {"model": model or self.model, "messages": [{"role": "user", "content": prompt}], "stream": False,
                "think": False, "keep_alive": "30m", "options": {"temperature": 0.2, "num_ctx": 16384}}
        if json_mode:
            body["format"] = "json"
        req = urllib.request.Request(f"{self.host}/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())["message"]["content"]

    def reply_sentences(self, user_text: str | None,
                        cancelled: Callable[[], bool] = lambda: False) -> Iterator[tuple[str, str]]:
        """Stream the reply as (emotion, sentence) so TTS can start early.

        Pass user_text=None to have the interviewer open the conversation. If `cancelled()`
        turns true (barge-in), generation stops and only the sentences already yielded are
        kept in the history, marked as interrupted.
        """
        self.history.append({"role": "user", "content": user_text or "(The candidate has joined the call.)"})
        buf, emotion, spoken = "", "neutral", []
        trim = AckTrimmer(trims_acks(user_text), self.recent_acks,
                          reply_to_question=bool(user_text) and not user_text.startswith("(silence"), opening=not user_text)

        self.ended = False

        def emit(raw: str):
            nonlocal emotion
            raw = _PAREN.sub("", raw)               # models leak instructions as "(If they ask questions, ...)"
            if re.search(r"\[end\]\W*$", raw.strip(), re.I):    # interviewer hangs up after this sentence...
                self.ended = not re.sub(r"\[end\]", "", raw, flags=re.I).strip().endswith("?")   # ...unless it's a question
            tags = [t.lower() for t in _TAG.findall(raw) if t.lower() in EMOTIONS]
            if tags:
                emotion = tags[0]
            if s := trim(_clean(raw, self.no_resume)):
                if self.question_filter and not (user_text or "").startswith("(silence"):
                    s = self.question_filter(s)          # a question already asked earlier -> a new one
                spoken.append(f"[{emotion}] {s}")
                return emotion, s
            return None

        stream = self._stream()
        try:
            for piece in stream:
                if cancelled():
                    break
                buf += piece
                parts = _SENTENCE_END.split(buf)
                for sent in parts[:-1]:
                    if out := emit(sent):
                        yield out
                        if cancelled():
                            break
                buf = parts[-1]
            else:
                if out := emit(buf):
                    yield out
        finally:
            stream.close()
            text = " ".join(spoken)
            if cancelled():
                text += " — (interrupted by the candidate)"
            self.history.append({"role": "assistant", "content": text})


_RESUME = re.compile(r"\b(on|from|in|per|based on|looking at|reviewing|according to) your (resume|cv|background|profile)\b,?\s*", re.I)
# "I've had a chance to look over your background and ..." - drop the clause up to the next comma/and
_RESUME_CLAUSE = re.compile(r"\b(and\s+)?I('ve| have)?\s*(had|have|took|got)?\s*(a|the)?\s*(chance|time|opportunity|look)?\s*(to\s+)?(look(ed)?|review(ed)?|read|go(ne)?|went)\s*(at|over|through)?\s*your (resume|cv|background|profile|application)\b[^,.!?]*[,]?\s*(and\s+)?", re.I)


def _clean(text: str, no_resume: bool = False) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if no_resume:   # "the most recent role on your resume" -> "your most recent role"
        text = _RESUME.sub("", _RESUME_CLAUSE.sub("", text)).strip()
        text = re.sub(r"\byour (resume|cv)\b", "your experience", text, flags=re.I)
        text = re.sub(r"\s*[;,]\s*(?=[.!?])", "", text)          # "joining;." -> "joining."
        text = re.sub(r"\s+(and|but|so)\s*(?=[.!?])", "", text)   # "today's interview and." -> "today's interview."
        text = re.sub(r"([.!?])(\s*[.!?])+", r"\1", text)
        text = text[:1].upper() + text[1:]
    text = _PAREN.sub("", text)
    text = _TAG.sub("", text)
    text = re.sub(r"^\s*\[[a-z' ]{2,24}\]\s*", "", text, flags=re.I)   # a leaked multi-word tag ("[thank you]")
    # template placeholders a model may leave in ("Hello [Candidate's Name]!") must never be spoken
    text = re.sub(r"\s*\[[^\]]{0,40}\b(name|candidate|company|role|position|interviewer)\b[^\]]{0,40}\]", "", text, flags=re.I)
    text = re.sub(r"\s+([!?.,])", r"\1", text)
    text = re.sub(r",+(?=[.!?,])", "", text)
    text = re.sub(r"\b(to|at|with|for)([.!?])", r"\2", text, flags=re.I)
    text = re.sub(r"\s+([!?.,])", r"\1", text)
    text = re.sub(r"[*_#`]+", "", text)
    # a closing that sounds like a rejection ("best of luck with your search") - real interviewers stay neutral
    text = re.sub(r",?\s*(and\s+)?(I\s+)?(wish(ing)? you\s+)?(the\s+)?(very\s+)?(best of|good) luck\b[^.!?]*", "", text, flags=re.I)
    text = re.sub(r"(^|[.!?]\s+)all the best\b[^.!?]*[.!]?", r"\1", text, flags=re.I).strip()
    text = re.sub(r"\bif (the|our|a) recruiter (reaches out|gets in touch|contacts you),?\s*", "", text, flags=re.I)   # "if" hints at a rejection
    text = text[:1].upper() + text[1:]
    return re.sub(r"\s{2,}", " ", text).strip()
