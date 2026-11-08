from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from cfgtrack.config import Device
from cfgtrack.fetchers import FetchError, fetch, fingerprint, host_key_of
from cfgtrack.profiles import GENERIC, IOS
from fakedevice import FakeSshDevice

CONFIG = "hostname r1\ninterface Gi0/1\n description a\nend\n"


def dev(**kw: object) -> Device:
    base = {"name": "d", "profile": GENERIC, "fetch": "file", "timeout": 5.0}
    return Device(**{**base, **kw})  # type: ignore[arg-type]


def ssh_dev(fake: FakeSshDevice, **kw: object) -> Device:
    base = {
        "name": "r1", "profile": IOS, "fetch": "ssh", "host": "127.0.0.1", "port": fake.port, "username": "backup",
        "password_env": "PW", "host_key": fake.fingerprint, "command": "show running-config", "timeout": 8.0,
    }  # fmt: skip
    return dev(**{**base, **kw})


ENV = {"PW": "s3cret-pw"}


# ---- file and command ----


def test_file(tmp_path: Path) -> None:
    p = tmp_path / "c.cfg"
    p.write_bytes(CONFIG.encode())
    assert fetch(dev(path=str(p))) == CONFIG
    with pytest.raises(FetchError, match="cannot read file"):
        fetch(dev(path=str(tmp_path / "missing")))
    with pytest.raises(FetchError, match="larger than max_bytes"):
        fetch(dev(path=str(p), max_bytes=10))


def test_command() -> None:
    d = dev(fetch="command", argv=(sys.executable, "-c", "print('hostname x')"))
    assert fetch(d).strip() == "hostname x"
    with pytest.raises(FetchError, match="exited with status 3"):
        fetch(dev(fetch="command", argv=(sys.executable, "-c", "import sys; sys.exit(3)")))
    with pytest.raises(FetchError, match="program not found"):
        fetch(dev(fetch="command", argv=("no-such-program-xyz",)))
    with pytest.raises(FetchError, match="timeout"):
        fetch(dev(fetch="command", argv=(sys.executable, "-c", "import time; time.sleep(30)"), timeout=1.0))
    with pytest.raises(FetchError, match="larger than max_bytes"):
        fetch(dev(fetch="command", argv=(sys.executable, "-c", "print('x'*5000)"), max_bytes=1000))


def test_command_is_not_run_through_a_shell(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    d = dev(fetch="command", argv=(sys.executable, "-c", "import sys; print(sys.argv[1])", f"; touch {marker}"))
    assert "touch" in fetch(d)
    assert not marker.exists()


# ---- http ----


def test_http(http_config: tuple[str, dict[str, str]]) -> None:
    base, seen = http_config
    assert fetch(dev(fetch="http", url=f"{base}/config")) == "hostname web1\nend\n"
    assert seen["auth"] == ""
    assert fetch(dev(fetch="http", url=f"{base}/config", token_env="T"), {"T": "tok"}) == "hostname web1\nend\n"
    assert seen["auth"] == "Bearer tok"
    with pytest.raises(FetchError, match="http 404"):
        fetch(dev(fetch="http", url=f"{base}/missing"))
    with pytest.raises(FetchError, match="T is not set"):
        fetch(dev(fetch="http", url=f"{base}/config", token_env="T"), {})
    with pytest.raises(FetchError, match="larger than max_bytes"):
        fetch(dev(fetch="http", url=f"{base}/big", max_bytes=1000))


def test_http_unreachable() -> None:
    with pytest.raises(FetchError, match="request failed"):
        fetch(dev(fetch="http", url="http://127.0.0.1:1/", timeout=2.0))


# ---- ssh ----


def test_ssh_exec_returns_the_configuration() -> None:
    with FakeSshDevice(CONFIG) as fake:
        assert fetch(ssh_dev(fake), ENV) == CONFIG
        assert fake.commands_seen == ["show running-config"]


def test_ssh_shell_mode_strips_echo_and_prompt() -> None:
    with FakeSshDevice(CONFIG, allow_exec=False) as fake:
        out = fetch(ssh_dev(fake, mode="shell", before=("terminal length 0",)), ENV)
        assert out.replace("\r", "").strip() == CONFIG.strip()
        assert fake.commands_seen == ["terminal length 0", "show running-config"]


def test_ssh_shell_does_not_mistake_a_line_ending_in_hash_for_the_prompt() -> None:
    cfg = "hostname r1\n" + "banner motd ^C welcome to r1#\n" * 40 + "end\n"
    with FakeSshDevice(cfg, allow_exec=False, chunk_delay=0.05) as fake:
        out = fetch(ssh_dev(fake, mode="shell", timeout=20.0), ENV)
        assert out.strip().endswith("end") and out.count("welcome to r1#") == 40


def test_ssh_exec_refused_by_the_device_gives_a_clear_error() -> None:
    with FakeSshDevice(CONFIG, allow_exec=False) as fake, pytest.raises(FetchError, match="ssh error"):
        fetch(ssh_dev(fake), ENV)


def test_ssh_nonzero_exit_status() -> None:
    with FakeSshDevice(CONFIG) as fake:
        fake.exit_status = 2
        with pytest.raises(FetchError, match="exited with status 2"):
            fetch(ssh_dev(fake), ENV)


def test_ssh_wrong_password_and_missing_env() -> None:
    with FakeSshDevice(CONFIG) as fake:
        with pytest.raises(FetchError, match="authentication failed"):
            fetch(ssh_dev(fake), {"PW": "wrong"})
        with pytest.raises(FetchError, match="PW is not set"):
            fetch(ssh_dev(fake), {})


def test_ssh_refuses_a_server_whose_key_is_not_the_pinned_one() -> None:
    with FakeSshDevice(CONFIG) as fake:
        other = "SHA256:" + "B" * 43
        with pytest.raises(FetchError, match="host key not trusted"):
            fetch(ssh_dev(fake, host_key=other), ENV)
        assert fake.commands_seen == []
        assert fake.auth_attempts == 0  # the password was never offered to a server with the wrong key


def test_ssh_known_hosts(tmp_path: Path) -> None:
    with FakeSshDevice(CONFIG) as fake:
        kh = tmp_path / "known_hosts"
        key = fake.key
        kh.write_text(f"[127.0.0.1]:{fake.port} {key.get_name()} {key.get_base64()}\n")
        assert fetch(ssh_dev(fake, host_key="", known_hosts=str(kh)), ENV) == CONFIG
        empty = tmp_path / "empty"
        empty.write_text("")
        with pytest.raises(FetchError, match="host key not trusted"):
            fetch(ssh_dev(fake, host_key="", known_hosts=str(empty)), ENV)
        with pytest.raises(FetchError, match="cannot read known_hosts"):
            fetch(ssh_dev(fake, host_key="", known_hosts=str(tmp_path / "nope")), ENV)


def test_ssh_timeout_and_size_limit() -> None:
    with FakeSshDevice("x" * 20000) as fake, pytest.raises(FetchError, match="larger than max_bytes"):
        fetch(ssh_dev(fake, max_bytes=1000), ENV)
    with FakeSshDevice(CONFIG * 400, chunk_delay=0.3) as fake:
        t0 = time.monotonic()
        with pytest.raises(FetchError, match="timeout"):
            fetch(ssh_dev(fake, timeout=1.0), ENV)
        assert time.monotonic() - t0 < 6


def test_ssh_connection_refused() -> None:
    with FakeSshDevice(CONFIG) as fake:
        d = ssh_dev(fake, port=1, timeout=3.0)
    with pytest.raises(FetchError, match=r"cannot connect|timeout"):  # Windows may wait before it reports a refusal
        fetch(d, ENV)


def test_host_key_of_matches_the_servers_fingerprint() -> None:
    with FakeSshDevice(CONFIG) as fake:
        kind, fp = host_key_of("127.0.0.1", fake.port, timeout=5)
        assert fp == fake.fingerprint and kind.startswith("ecdsa")
    with pytest.raises(FetchError, match="cannot connect"):
        host_key_of("127.0.0.1", 1, timeout=2)


def test_fingerprint_format() -> None:
    fp = fingerprint(b"abc")
    assert fp.startswith("SHA256:") and len(fp) == 7 + 43 and "=" not in fp
    assert replace(dev(path="x"), name="n").name == "n"
