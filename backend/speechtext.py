"""Make text speakable before TTS: espeak (Kokoro's phonemizer) reads "50,000" as "fifty, zero zero zero",
"$1.5M" as "dollar one . five M", "40k" as "forty kay", "900ms" as "nine hundred em ess".

Only the audio uses this; transcripts keep the original text. Mirrored in web/speech-text.js.
"""

from __future__ import annotations

import re

_CURRENCY = {"S$": "Singapore dollars", "US$": "US dollars", "A$": "Australian dollars", "$": "dollars",
             "€": "euros", "£": "pounds", "¥": "yen", "₹": "rupees"}
_SCALE = {"k": "thousand", "m": "million", "b": "billion", "bn": "billion", "t": "trillion"}
_UNITS = {"ms": "milliseconds", "µs": "microseconds", "us": "microseconds", "ns": "nanoseconds", "s": "seconds",
          "min": "minutes", "h": "hours", "hrs": "hours", "kb": "kilobytes", "mb": "megabytes", "gb": "gigabytes",
          "tb": "terabytes", "rps": "requests per second", "qps": "queries per second", "tps": "transactions per second",
          "ghz": "gigahertz", "mhz": "megahertz", "kbps": "kilobits per second", "mbps": "megabits per second",
          "gbps": "gigabits per second"}

_NUM = r"\d+(?:,\d{3})*(?:\.\d+)?"


def _num(n: str) -> str:
    n = n.replace(",", "")                                    # 50,000 -> 50000 (espeak reads it fine)
    if "." in n:
        whole, frac = n.split(".", 1)
        return f"{whole} point {' '.join(frac)}"              # 1.5 -> 1 point 5 ; 99.99 -> 99 point 9 9
    return n


def speakable(text: str) -> str:
    t = text
    # currency with optional scale: S$4B, $1.5M, US$ 40k, €2 million
    cur = "|".join(re.escape(c) for c in sorted(_CURRENCY, key=len, reverse=True))
    t = re.sub(rf"({cur})\s?({_NUM})\s?(bn|[kKmMbBtT](?![a-zA-Z])|thousand|million|billion|trillion)?",
               lambda m: f"{_num(m[2])}{' ' + _SCALE.get(m[3].lower(), m[3]) if m[3] else ''} {_CURRENCY[m[1]]}", t)
    # bare scale suffix: 40k, 1.5M, 2B (not 4G/5G etc.)
    t = re.sub(rf"\b({_NUM})(bn|[kKmMbB])\b", lambda m: f"{_num(m[1])} {_SCALE[m[2].lower()]}", t)
    # units glued or spaced: 900ms, 120 ms, 10GB, 3,000 TPS  ("s" only when glued, to keep "5 s..." sentences intact)
    units = "|".join(sorted((u for u in _UNITS if u != "s"), key=len, reverse=True))
    t = re.sub(rf"\b({_NUM})\s?({units})\b", lambda m: f"{_num(m[1])} {_UNITS[m[2].lower()]}", t, flags=re.I)
    t = re.sub(rf"\b({_NUM})s\b(?!\w)", lambda m: f"{_num(m[1])} seconds", t)
    t = re.sub(r"/s\b", " per second", t)
    # multipliers: 3x, 10x faster
    t = re.sub(rf"\b({_NUM})x\b", lambda m: f"{_num(m[1])} times", t)
    # p99 latency -> p ninety-nine
    t = re.sub(r"\bp(\d{2,3}(?:\.\d+)?)\b", lambda m: f"p {_num(m[1])}", t)
    # remaining numbers: thousands separators and decimals
    t = re.sub(_NUM, lambda m: _num(m[0]), t)
    # ranges and approximations
    t = re.sub(r"(\d)\s?[-–]\s?(\d)", r"\1 to \2", t)
    t = t.replace("~", "about ").replace("≈", "about ")
    return re.sub(r"\s{2,}", " ", t).strip()


_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")
    if n < 1000:
        return _ONES[n // 100] + " hundred" + (" " + _words(n % 100) if n % 100 else "")
    for size, name in ((10**9, "billion"), (10**6, "million"), (1000, "thousand")):
        if n >= size:
            return _words(n // size) + " " + name + (" " + _words(n % size) if n % size else "")
    return str(n)


def ints_to_words(text: str) -> str:
    """40000 -> "forty thousand" for TTS engines that read long digit strings digit by digit (ZONOS3).
    Years (1900-2099) and decimals' digits are left alone."""
    def rep(m):
        n = int(m[0])
        return m[0] if 1900 <= n <= 2099 or n >= 10**12 else _words(n)
    return re.sub(r"(?<![\d.])\d{3,}(?![\d.])", rep, text)
