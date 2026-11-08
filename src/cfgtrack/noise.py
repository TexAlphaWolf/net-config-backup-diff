"""Finding the lines a new kind of device prints differently on every run, so that they can be ignored.

A profile knows the noise of the devices it was written for. For anything else the first symptom is a commit on every
backup. `suggest_ignores` looks at the last commits of a device, finds the kinds of line that were added and removed
in most of them (a line of the same shape, with other numbers) and proposes a pattern for each. It only proposes: the
patterns are printed for a person to read and paste into the configuration.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from cfgtrack.diffing import diff_text
from cfgtrack.store import Repo


@dataclass(frozen=True)
class Suggestion:
    pattern: str
    share: float  # the share of the examined commits in which a line of this shape changed
    matches_now: int  # lines of the current configuration the pattern would hide


def shape(line: str) -> str:
    """A regular expression for 'this line with other numbers'."""
    body = re.sub(r"\d+", r"\\d+", re.escape(line.strip()).replace("\\ ", " "))
    return rf"^\s*{body}\s*$"


def suggest_ignores(repo: Repo, name: str, style: str, last: int = 20, min_share: float = 0.7) -> list[Suggestion]:
    commits = repo.log(name, limit=last + 1)
    pairs = []
    for c in commits[:-1]:  # the oldest of them has no parent to compare with
        new, old = repo.read(name, c.id), repo.read(name, f"{c.id}~1")
        if new is not None and old is not None:
            pairs.append((old, new))
    if len(pairs) < 3:
        return []
    seen: Counter[str] = Counter()
    both: Counter[str] = Counter()
    for old, new in pairs:
        kinds: dict[str, set[str]] = {}
        for ch in diff_text(old, new, style):
            kinds.setdefault(shape(ch.line), set()).add(ch.kind)
        for s, k in kinds.items():
            seen[s] += 1
            if k == {"added", "removed"}:  # replaced by another value: the signature of a counter or a clock
                both[s] += 1
    current = (repo.read(name) or "").splitlines()
    out = []
    for s, n in both.most_common():
        if n / len(pairs) >= min_share:
            rx = re.compile(s)
            out.append(Suggestion(s, n / len(pairs), sum(1 for ln in current if rx.search(ln))))
    return out
