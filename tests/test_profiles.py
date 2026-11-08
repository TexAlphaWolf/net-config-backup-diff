from __future__ import annotations

import pytest

from cfgtrack.normalize import TAG, redact_line, tag
from cfgtrack.profiles import GENERIC, IOS, JUNOS, PROFILES, ROUTEROS, get_profile

KEY = b"k" * 32


@pytest.mark.parametrize(
    ("line", "secret"),
    [
        ("enable secret 9 $9$abcdef", "$9$abcdef"),
        ("enable password cisco", "cisco"),
        ("username admin privilege 15 secret 9 $9$zzz", "$9$zzz"),
        ("username bob password 0 hunter2", "hunter2"),
        ("username bob secret hunter2 privilege 1", None),  # token after the secret is not the secret
        (" password 7 0822455D0A16", "0822455D0A16"),
        ("snmp-server community public123 RO", "public123"),
        ("tacacs-server key 7 1234ABCD", "1234ABCD"),
        ("radius-server key letmein", "letmein"),
        (" key 7 1234ABCD", "1234ABCD"),
        (" pre-shared-key 7 05080F1C2243", "05080F1C2243"),
        ("ntp authentication-key 1 md5 s3cr3t 7", "s3cr3t"),
        ("crypto isakmp key MyVpnKey address 1.2.3.4", "MyVpnKey"),
        (" ip ospf message-digest-key 1 md5 7 0822455D", "0822455D"),
        (" ip ospf authentication-key ospfpass", "ospfpass"),
        (" standby 1 authentication md5 key-string 7 ABCDEF", "ABCDEF"),
    ],
)
def test_ios_secrets(line: str, secret: str | None) -> None:
    out, n = redact_line(line, IOS, KEY)
    if secret is None:
        assert n >= 1 and "hunter2" not in out
        return
    assert n == 1
    assert secret not in out
    assert TAG.search(out)


@pytest.mark.parametrize(
    "line",
    [
        "key chain MYCHAIN",
        " key 1",  # a key id in a key chain, not a key
        "interface GigabitEthernet0/1",
        " description password reset kiosk",
        "ip route 10.0.0.0 255.0.0.0 10.1.1.1",
        "snmp-server location rack 4",
    ],
)
def test_ios_lines_that_are_not_secrets(line: str) -> None:
    assert redact_line(line, IOS, KEY) == (line, 0)


def test_junos_and_routeros_secrets() -> None:
    out, n = redact_line('            encrypted-password "$6$salt$hash"; ## SECRET-DATA', JUNOS, KEY)
    assert n == 1 and "$6$salt$hash" not in out
    out, n = redact_line("        community private-comm {", JUNOS, KEY)
    assert n == 1 and "private-comm" not in out
    out, n = redact_line('add name=u1 password="p w d" group=full', ROUTEROS, KEY)
    assert n == 1 and "p w d" not in out and "group=full" in out
    out, n = redact_line("set wpa2-pre-shared-key=abcd1234 mode=ap", ROUTEROS, KEY)
    assert n == 1 and "abcd1234" not in out


def test_generic_secrets() -> None:
    for line, secret in [
        ("password = hunter2", "hunter2"),
        ("API_KEY: abc123def", "abc123def"),
        ('token "quoted value"', '"quoted value"'),
        ("psk mypsk", "mypsk"),
    ]:
        out, n = redact_line(line, GENERIC, KEY)
        assert n == 1 and secret not in out, line


def test_redaction_is_idempotent_and_stable() -> None:
    line = "username bob privilege 15 secret 9 $9$abc"
    once, _ = redact_line(line, IOS, KEY)
    twice, n = redact_line(once, IOS, KEY)
    assert once == twice and n == 0
    assert redact_line(line, IOS, KEY)[0] == once  # same secret, same tag
    assert redact_line(line.replace("abc", "abd"), IOS, KEY)[0] != once  # other secret, other tag
    assert redact_line(line, IOS, b"z" * 32)[0] != once  # other key, other tag


def test_the_tag_does_not_contain_the_secret_and_is_keyed() -> None:
    t = tag(KEY, "hunter2")
    assert "hunter2" not in t and TAG.fullmatch(t)
    assert tag(KEY, "hunter2") == t and tag(b"x" * 32, "hunter2") != t


def test_ignore_rules() -> None:
    noisy = [
        "! Last configuration change at 10:11:12 UTC",
        "!",
        "Building configuration...",
        "Current configuration : 1234 bytes",
        "ntp clock-period 17179869",
    ]
    assert all(any(p.search(x) for p in IOS.ignore) for x in noisy)
    assert not any(p.search("hostname sw1") for p in IOS.ignore)
    assert any(p.search("## Last commit: 2026-11-03 10:11:12 UTC by admin") for p in JUNOS.ignore)
    assert any(p.search("# 2026-11-08 20:00:00 by RouterOS 7.16") for p in ROUTEROS.ignore)


def test_get_profile_with_extras_and_errors() -> None:
    p = get_profile("generic", extra_ignore=(r"^uptime",), extra_redact=(r"license=(?P<secret>\S+)",))
    assert len(p.ignore) == 1 and len(p.redact) == len(GENERIC.redact) + 1
    assert set(PROFILES) == {"cisco_ios", "junos", "routeros", "generic"}
    with pytest.raises(ValueError, match="unknown profile"):
        get_profile("nope")
    with pytest.raises(ValueError, match="bad ignore pattern"):
        get_profile("generic", extra_ignore=("(",))
    with pytest.raises(ValueError, match=r"\(\?P<secret>"):
        get_profile("generic", extra_redact=(r"license=\S+",))
