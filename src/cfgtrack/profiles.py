"""What each kind of device prints that is noise, what is a secret, and how its configuration is structured.

A profile is deliberately small: three lists of regular expressions and a structure style. The rules are best effort
for the commands they were written for (see tests/test_profiles.py); every device section of the configuration file
can add its own `ignore` and `redact` patterns, and `cfgtrack verify` checks the stored files for leftovers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# `style` says how a configuration is nested:
#   indent   - children are indented under their parent (Cisco IOS, Arista EOS, Linux files, most things)
#   braces   - `parent {` ... `}` blocks (Junos)
#   sections - `/path` header lines followed by flat commands (MikroTik RouterOS export)
STYLES = ("indent", "braces", "sections")


def _r(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p) for p in patterns)


@dataclass(frozen=True)
class Profile:
    name: str
    style: str
    ignore: tuple[re.Pattern[str], ...]  # whole lines that change without the configuration changing
    redact: tuple[re.Pattern[str], ...]  # each has a named group `secret`: the part that is replaced by a tag
    complete: re.Pattern[str] | None = None  # the last line of a complete configuration
    errors: tuple[re.Pattern[str], ...] = ()  # what the device prints instead of a configuration


IOS = Profile(
    name="cisco_ios",
    style="indent",
    ignore=_r(
        r"^\s*!",  # comments and separators; IOS prints its own timestamps as comments
        r"^Building configuration\.\.\.",
        r"^Current configuration\s*:\s*\d+ bytes",
        r"^Using \d+ out of \d+ bytes",
        r"^\s*ntp clock-period\s+\d+",
        r"^\s*Last configuration change",
    ),
    redact=_r(
        r"^\s*enable\s+(?:secret|password)(?:\s+\d+)?\s+(?P<secret>\S+)",
        r"^\s*username\s+\S+\s.*?\b(?:secret|password)(?:\s+\d+)?\s+(?P<secret>\S+)",
        r"^\s*password(?:\s+\d+)?\s+(?P<secret>\S+)\s*$",
        r"^\s*snmp-server\s+community\s+(?P<secret>\S+)",
        r"^\s*(?:tacacs-server|radius-server)\s+key(?:\s+\d+)?\s+(?P<secret>\S+)",
        r"^\s+key\s+\d+\s+(?P<secret>\S+)\s*$",
        r"^\s*(?:pre-shared-key|key-string)(?:\s+\d+)?\s+(?P<secret>\S+)",
        r"^\s*ntp\s+authentication-key\s+\d+\s+md5\s+(?P<secret>\S+)",
        r"^\s*crypto\s+isakmp\s+key\s+(?:\d+\s+)?(?P<secret>\S+)",
        r"^\s*ip\s+ospf\s+message-digest-key\s+\d+\s+md5\s+(?:\d+\s+)?(?P<secret>\S+)",
        r"^\s*ip\s+ospf\s+authentication-key\s+(?:\d+\s+)?(?P<secret>\S+)",
        r"^\s*standby\s+\d+\s+authentication(?:\s+md5\s+key-string)?(?:\s+\d+)?\s+(?P<secret>\S+)",
        r"^\s*wpa-psk\s+ascii(?:\s+\d+)?\s+(?P<secret>\S+)",
    ),
    complete=re.compile(r"^end\s*$"),
    errors=_r(r"^%\s*(?:Invalid|Incomplete|Ambiguous|Unrecognized|Authorization failed)", r"(?i)permission denied"),
)

JUNOS = Profile(
    name="junos",
    style="braces",
    ignore=_r(r"^\s*##\s*Last (?:commit|changed)", r"^\s*/\*.*\*/\s*$", r"^\s*#"),
    redact=_r(
        r'(?:encrypted-password|authentication-key|secret|pre-shared-key\s+(?:ascii-text|hexadecimal))\s+"(?P<secret>[^"]+)"',
        r"\bcommunity\s+(?P<secret>[^\s;{]+)",
    ),
    errors=_r(r"^error:", r"(?i)syntax error", r"(?i)unknown command", r"(?i)permission denied"),
)

ROUTEROS = Profile(
    name="routeros",
    style="sections",
    ignore=_r(r"^#"),  # `# 2026-11-08 20:00:00 by RouterOS 7.16`, `# software id = ...`
    redact=_r(
        r'\b(?:[\w-]*(?:password|secret|passphrase|pre-shared-key|private-key)|key|community)=(?P<secret>"[^"]*"|\S+)'
    ),
    errors=_r(r"^bad command name", r"^expected end of command", r"^failure:", r"(?i)permission denied"),
)

GENERIC = Profile(
    name="generic",
    style="indent",
    ignore=(),
    redact=_r(
        r'(?i)\b(?:password|passwd|secret|token|api[_-]?key|community|psk)\b\s*[=:\s]\s*(?P<secret>"[^"]+"|\S+)',
    ),
    errors=_r(r"(?i)permission denied", r"(?i)command not found", r"^Traceback \(most recent call last\)"),
)

PROFILES: dict[str, Profile] = {p.name: p for p in (IOS, JUNOS, ROUTEROS, GENERIC)}


def get_profile(name: str, extra_ignore: tuple[str, ...] = (), extra_redact: tuple[str, ...] = ()) -> Profile:
    """The named profile plus the device's own patterns. Raises ValueError for an unknown name or a bad pattern."""
    try:
        base = PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown profile {name!r} (known: {', '.join(sorted(PROFILES))})") from None
    ignore = base.ignore + _compile(extra_ignore, "ignore")
    redact = base.redact + _compile(extra_redact, "redact")
    for pat in redact[len(base.redact) :]:
        if "secret" not in pat.groupindex:
            raise ValueError(f"redact pattern {pat.pattern!r} needs a named group (?P<secret>...)")
    return Profile(base.name, base.style, ignore, redact, base.complete, base.errors)


def _compile(patterns: tuple[str, ...], what: str) -> tuple[re.Pattern[str], ...]:
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p))
        except re.error as exc:
            raise ValueError(f"bad {what} pattern {p!r}: {exc}") from None
    return tuple(out)
