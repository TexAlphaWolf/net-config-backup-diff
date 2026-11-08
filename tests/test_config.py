from __future__ import annotations

from pathlib import Path

import pytest

from cfgtrack.config import DEFAULT_TOML, ConfigError, load, parse, parse_duration

SSH = {
    "name": "r1", "profile": "cisco_ios", "fetch": "ssh", "host": "192.0.2.1", "username": "backup",
    "password_env": "PW", "host_key": "SHA256:" + "A" * 43,
}  # fmt: skip


def cfg(**dev: object) -> dict[str, object]:
    return {"device": [{**SSH, **dev}]}


def test_the_example_configuration_loads(tmp_path: Path) -> None:
    p = tmp_path / "cfgtrack.toml"
    p.write_text(DEFAULT_TOML)
    c = load(p)
    assert [d.name for d in c.devices] == ["core-sw1", "edge-fw", "lab-ap"]
    assert c.store == tmp_path / "backups" and c.stale_after == 2 * 86400
    assert c.devices[0].command == "show running-config" and c.devices[0].profile.name == "cisco_ios"


def test_defaults_per_profile() -> None:
    c = parse(cfg(profile="junos"), Path("."))
    assert c.devices[0].command == "show configuration"
    c = parse(cfg(profile="routeros"), Path("."))
    assert c.devices[0].command == "/export"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"password": "hunter2"}, "must not be written in the file"),
        ({"token": "abc"}, "must not be written in the file"),
        ({"bogus": 1}, "unknown key 'bogus'"),
        ({"fetch": "telnet"}, "'fetch' must be one of"),
        ({"profile": "nope"}, "unknown profile"),
        ({"name": "../x"}, "bad device name"),
        ({"host_key": ""}, "host keys are never accepted blindly"),
        ({"host_key": "SHA256:short"}, "must look like SHA256"),
        ({"password_env": ""}, "needs 'password_env' or 'key_file'"),
        ({"host": ""}, "needs 'host' and 'username'"),
        ({"profile": "generic"}, "needs a 'command'"),
        ({"mode": "telnet"}, "'mode' must be exec or shell"),
        ({"prompt": "("}, "bad prompt pattern"),
        ({"timeout": 0}, "'timeout' must be a number"),
        ({"timeout": True}, "'timeout' must be a number"),
        ({"port": 70000}, "'port' must be a number"),
        ({"ignore": "x"}, "'ignore' must be a list of strings"),
        ({"redact": ["no-group"]}, "named group"),
        ({"host": 5}, "'host' must be a string"),
    ],
)
def test_ssh_validation(override: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(cfg(**override), Path("."))


@pytest.mark.parametrize(
    ("dev", "message"),
    [
        ({"name": "a", "fetch": "command"}, "needs 'argv'"),
        ({"name": "a", "fetch": "file"}, "needs 'path'"),
        ({"name": "a", "fetch": "http", "url": "ftp://x/y"}, "must be http"),
        ({"name": "a", "fetch": "http", "url": "https://user:pw@x/y"}, "not in the URL"),
    ],
)
def test_other_kinds(dev: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse({"device": [dev]}, Path("."))


def test_structure_errors() -> None:
    with pytest.raises(ConfigError, match="unknown section"):
        parse({"stor": {}}, Path("."))
    with pytest.raises(ConfigError, match="unknown key 'x'"):
        parse({"store": {"x": 1}}, Path("."))
    with pytest.raises(ConfigError, match="same name"):
        parse({"device": [SSH, SSH]}, Path("."))
    with pytest.raises(ConfigError, match="array of tables"):
        parse({"device": [1]}, Path("."))
    with pytest.raises(ConfigError, match="must be a table"):
        parse({"store": 1}, Path("."))
    with pytest.raises(ConfigError, match="shrink_guard"):
        parse({"store": {"shrink_guard": 2}}, Path("."))


def test_load_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "nope.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[store\n")
    with pytest.raises(ConfigError, match=r"bad\.toml"):
        load(bad)


def test_durations() -> None:
    assert parse_duration("90s") == 90 and parse_duration("15m") == 900 and parse_duration("2d") == 172800
    assert parse_duration(60) == 60 and parse_duration("1.5h") == 5400
    for bad in ("soon", "2 days", "", True):
        with pytest.raises(ConfigError):
            parse_duration(bad)  # type: ignore[arg-type]


def test_known_hosts_is_an_alternative_to_a_pinned_key() -> None:
    d = {k: v for k, v in SSH.items() if k != "host_key"}
    c = parse({"device": [{**d, "known_hosts": "~/.ssh/known_hosts"}]}, Path("."))
    assert c.devices[0].known_hosts
