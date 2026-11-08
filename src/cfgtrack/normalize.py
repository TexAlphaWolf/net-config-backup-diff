"""Turn what a device printed into the text that is stored: no noise, no secrets, stable line endings."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

from cfgtrack.profiles import Profile

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
TAG = re.compile(r"<secret:[0-9a-f]{10}>")
TAG_LEN = 10


def tag(key: bytes, value: str) -> str:
    """A stand-in for a secret: the same secret always gives the same tag, a different one a different tag, and
    without the key the tag cannot be used to guess the secret (HMAC-SHA256, 40 bits shown)."""
    digest = hmac.new(key, value.encode("utf-8", "replace"), hashlib.sha256).hexdigest()
    return f"<secret:{digest[:TAG_LEN]}>"


def redact_line(line: str, profile: Profile, key: bytes) -> tuple[str, int]:
    """Replace every secret the profile recognises in one line; returns the new line and how many were replaced."""
    n = 0
    for pat in profile.redact:
        pos = 0
        while (m := pat.search(line, pos)) is not None:
            value = m.group("secret")
            if value is None:
                break
            if TAG.fullmatch(value):  # already redacted: running twice must change nothing
                pos = m.end("secret")
                if pos >= len(line):
                    break
                continue
            t = tag(key, value)
            line = line[: m.start("secret")] + t + line[m.end("secret") :]
            pos = m.start("secret") + len(t)
            n += 1
    return line, n


@dataclass(frozen=True)
class Normalized:
    text: str  # one line per configuration line, "\n" endings, final newline
    lines: int
    dropped: int  # lines removed as noise
    redacted: int  # secrets replaced by a tag


def normalize(raw: str, profile: Profile, key: bytes) -> Normalized:
    out: list[str] = []
    dropped = redacted = 0
    for line in _CONTROL.sub("", _ANSI.sub("", raw.replace("\r\n", "\n").replace("\r", "\n"))).split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        if any(p.search(line) for p in profile.ignore):
            dropped += 1
            continue
        line, n = redact_line(line, profile, key)
        redacted += n
        out.append(line)
    return Normalized("\n".join(out) + "\n" if out else "", len(out), dropped, redacted)


def implausible(norm: Normalized, profile: Profile) -> str | None:
    """Why this is not a usable configuration (empty, an error message, cut off), or None if it looks complete."""
    if not norm.lines:
        return "empty output"
    lines = norm.text.splitlines()
    for first in lines[:5]:
        for pat in profile.errors:
            if pat.search(first):
                return f"device error: {first.strip()[:80]}"
    if profile.complete is not None and not profile.complete.search(lines[-1]):
        return "output does not end like a complete configuration"
    return None
