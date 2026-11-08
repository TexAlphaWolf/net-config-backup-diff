from __future__ import annotations

from cfgtrack.normalize import implausible, normalize
from cfgtrack.profiles import GENERIC, IOS, JUNOS, ROUTEROS


def test_noise_and_secrets_are_removed(ios: str, key: bytes) -> None:
    n = normalize(ios, IOS, key)
    assert n.dropped >= 6 and n.redacted == 4
    for gone in (
        "Building configuration",
        "Last configuration change",
        "clock-period",
        "$9$abcdefghijklmn",
        "public123",
        "0822455D0A16",
    ):
        assert gone not in n.text
    assert "hostname sw1" in n.text and n.text.endswith("end\n")
    assert n.lines == len(n.text.splitlines())


def test_two_runs_that_differ_only_in_noise_normalize_identically(ios: str, key: bytes) -> None:
    other = ios.replace("10:11:12", "23:59:01").replace("1432 bytes", "1440 bytes").replace("17179869", "17179902")
    assert other != ios
    assert normalize(other, IOS, key).text == normalize(ios, IOS, key).text


def test_a_real_change_survives(ios: str, key: bytes) -> None:
    changed = ios.replace("description uplink", "description uplink-core")
    assert normalize(changed, IOS, key).text != normalize(ios, IOS, key).text


def test_a_rotated_secret_is_still_a_change_but_never_shows_the_secret(ios: str, key: bytes) -> None:
    rotated = ios.replace("public123", "newcommunity9")
    a, b = normalize(ios, IOS, key).text, normalize(rotated, IOS, key).text
    assert a != b and "newcommunity9" not in b


def test_line_endings_whitespace_and_control_characters(key: bytes) -> None:
    raw = "hostname a  \r\n\r\n\x1b[0mvlan 1\x00\r\n name x\t\r\n"
    assert normalize(raw, GENERIC, key).text == "hostname a\nvlan 1\n name x\n"


def test_empty_input(key: bytes) -> None:
    n = normalize("\n \n", GENERIC, key)
    assert (n.text, n.lines) == ("", 0)
    assert implausible(n, GENERIC) == "empty output"


def test_implausible_outputs(ios: str, key: bytes) -> None:
    assert implausible(normalize(ios, IOS, key), IOS) is None
    cut = "\n".join(ios.splitlines()[:20]) + "\n"
    assert "does not end" in (implausible(normalize(cut, IOS, key), IOS) or "")
    err = normalize("% Invalid input detected at '^' marker.\n", IOS, key)
    assert "device error" in (implausible(err, IOS) or "")
    denied = normalize("Permission denied, please try again.\n", GENERIC, key)
    assert "device error" in (implausible(denied, GENERIC) or "")
    assert "device error" in (implausible(normalize("error: syntax error\n", JUNOS, key), JUNOS) or "")
    assert "device error" in (implausible(normalize("bad command name export\n", ROUTEROS, key), ROUTEROS) or "")


def test_an_error_word_deep_in_a_real_configuration_is_not_an_error(key: bytes) -> None:
    lines = [f"line {i}" for i in range(30)] + ["description Permission denied area", "end"]
    assert implausible(normalize("\n".join(lines), IOS, key), IOS) is None


def test_junos_noise(key: bytes) -> None:
    raw = "## Last commit: 2026-11-03 10:11:12 UTC by admin\nsystem {\n    host-name r1;\n}\n"
    assert normalize(raw, JUNOS, key).text == "system {\n    host-name r1;\n}\n"
