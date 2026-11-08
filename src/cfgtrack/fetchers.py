"""Getting the configuration text out of a device. Four ways; every one has a time limit and a size limit."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cfgtrack.config import Device


class FetchError(Exception):
    """A short, safe message: never the credentials, never raw output."""


def fetch(dev: Device, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    if dev.fetch == "file":
        return _file(dev)
    if dev.fetch == "command":
        return _command(dev)
    if dev.fetch == "http":
        return _http(dev, env)
    if dev.fetch == "ssh":
        return _ssh(dev, env)
    raise FetchError(f"unknown fetch kind {dev.fetch!r}")  # pragma: no cover (config validation prevents it)


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def _file(dev: Device) -> str:
    try:
        with open(dev.path, "rb") as fh:
            data = fh.read(dev.max_bytes + 1)
    except OSError as exc:
        raise FetchError(f"cannot read file: {exc.strerror or 'error'}") from None
    if len(data) > dev.max_bytes:
        raise FetchError(f"file is larger than max_bytes ({dev.max_bytes})")
    return _decode(data)


def _command(dev: Device) -> str:
    try:
        r = subprocess.run(list(dev.argv), capture_output=True, timeout=dev.timeout, check=False)
    except FileNotFoundError:
        raise FetchError(f"program not found: {dev.argv[0]}") from None
    except subprocess.TimeoutExpired:
        raise FetchError(f"timeout after {dev.timeout:g} s") from None
    except OSError as exc:
        raise FetchError(f"cannot run the command: {exc.strerror or 'error'}") from None
    if r.returncode != 0:
        raise FetchError(f"command exited with status {r.returncode}")
    if len(r.stdout) > dev.max_bytes:
        raise FetchError(f"output is larger than max_bytes ({dev.max_bytes})")
    return _decode(r.stdout)


def _http(dev: Device, env: Mapping[str, str]) -> str:
    req = urllib.request.Request(dev.url, headers={"User-Agent": "cfgtrack", "Accept": "text/plain, */*"})
    if dev.token_env:
        token = env.get(dev.token_env)
        if not token:
            raise FetchError(f"environment variable {dev.token_env} is not set")
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=dev.timeout) as resp:
            data = resp.read(dev.max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"http {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise FetchError(f"request failed: {type(reason).__name__}") from None
    if len(data) > dev.max_bytes:
        raise FetchError(f"response is larger than max_bytes ({dev.max_bytes})")
    return _decode(data)


# ---- SSH ----


QUIET = 0.15  # seconds of silence after something that looks like a prompt before it is believed


def fingerprint(key_blob: bytes) -> str:
    """The OpenSSH way of writing a host key fingerprint: SHA256:<base64 without padding>."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(key_blob).digest()).decode().rstrip("=")


def _paramiko() -> Any:
    try:
        import paramiko
    except ImportError:
        raise FetchError("SSH needs the paramiko package: pip install 'cfgtrack[ssh]'") from None
    return paramiko


def host_key_of(host: str, port: int = 22, timeout: float = 10.0) -> tuple[str, str]:
    """Connect without logging in and return (key type, fingerprint): the value to pin as `host_key`."""
    paramiko = _paramiko()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise FetchError(f"cannot connect: {exc.strerror or 'error'}") from None
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        return key.get_name(), fingerprint(key.asbytes())
    except paramiko.SSHException as exc:
        raise FetchError(f"ssh negotiation failed: {type(exc).__name__}") from None
    finally:
        transport.close()


def _ssh(dev: Device, env: Mapping[str, str]) -> str:
    paramiko = _paramiko()
    password = None
    if dev.password_env:
        password = env.get(dev.password_env)
        if not password:
            raise FetchError(f"environment variable {dev.password_env} is not set")
    passphrase = env.get(dev.passphrase_env) if dev.passphrase_env else None

    client = paramiko.SSHClient()
    if dev.host_key:
        client.set_missing_host_key_policy(_pinned(paramiko, dev.host_key))
    else:
        try:
            client.load_host_keys(str(Path(dev.known_hosts).expanduser()))
        except OSError as exc:
            raise FetchError(f"cannot read known_hosts: {exc.strerror or 'error'}") from None
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    deadline = time.monotonic() + dev.timeout
    try:
        client.connect(
            dev.host,
            port=dev.port,
            username=dev.username,
            password=password,
            key_filename=str(Path(dev.key_file).expanduser()) if dev.key_file else None,
            passphrase=passphrase,
            timeout=dev.timeout,
            banner_timeout=dev.timeout,
            auth_timeout=dev.timeout,
            allow_agent=False,
            look_for_keys=False,
        )
        if dev.mode == "shell":
            return _shell(client, dev, deadline)
        return _exec(client, dev, deadline)
    except FetchError:
        raise
    except paramiko.AuthenticationException:
        raise FetchError("authentication failed") from None
    except paramiko.BadHostKeyException:
        raise FetchError("host key does not match the known one") from None
    except paramiko.SSHException as exc:
        raise FetchError(f"ssh error: {_ssh_reason(exc)}") from None
    except TimeoutError:
        raise FetchError(f"timeout after {dev.timeout:g} s") from None
    except OSError as exc:
        raise FetchError(f"cannot connect: {exc.strerror or type(exc).__name__}") from None
    finally:
        client.close()


def _ssh_reason(exc: Exception) -> str:
    text = str(exc)
    if "not found in known_hosts" in text or "pinned" in text or "does not match" in text:
        return "host key not trusted"
    return type(exc).__name__


def _pinned(paramiko: Any, expected: str) -> Any:
    class Pinned(paramiko.MissingHostKeyPolicy):  # type: ignore[misc]
        def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
            if not hmac_equal(fingerprint(key.asbytes()), expected):
                raise paramiko.SSHException("host key does not match the pinned fingerprint")

    return Pinned()


def hmac_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _exec(client: Any, dev: Device, deadline: float) -> str:
    _stdin, stdout, _stderr = client.exec_command(dev.command, timeout=dev.timeout)
    chan = stdout.channel
    chan.settimeout(1.0)
    data = bytearray()
    while True:
        if time.monotonic() > deadline:
            raise FetchError(f"timeout after {dev.timeout:g} s")
        try:
            chunk = chan.recv(65536)
        except TimeoutError:
            continue
        if not chunk:
            break
        data += chunk
        if len(data) > dev.max_bytes:
            raise FetchError(f"output is larger than max_bytes ({dev.max_bytes})")
    status = chan.recv_exit_status()
    if status not in (0, -1):
        raise FetchError(f"command exited with status {status}")
    return _decode(bytes(data))


def _shell(client: Any, dev: Device, deadline: float) -> str:
    """An interactive session, for devices that refuse `exec`: send the commands, wait for the prompt after each."""
    prompt = re.compile(dev.prompt)
    chan = client.invoke_shell(width=511, height=2000)
    chan.settimeout(0.1)

    def read_until_prompt() -> str:
        buf = bytearray()
        quiet_since: float | None = None
        while True:
            if time.monotonic() > deadline:
                raise FetchError(f"timeout after {dev.timeout:g} s")
            try:
                chunk = chan.recv(65536)
                if not chunk:
                    raise FetchError("the device closed the session")
                buf += chunk
                quiet_since = None
                if len(buf) > dev.max_bytes:
                    raise FetchError(f"output is larger than max_bytes ({dev.max_bytes})")
            except TimeoutError:
                # a prompt only counts when the device has also gone quiet: a line that happens to end in '#'
                # in the middle of a long output is not a prompt
                last = _decode(bytes(buf)).replace("\r", "").rsplit("\n", 1)[-1]
                if prompt.search(last):
                    now = time.monotonic()
                    quiet_since = quiet_since or now
                    if now - quiet_since >= QUIET:
                        return _decode(bytes(buf))

    read_until_prompt()  # banner and first prompt
    for cmd in (*dev.before, dev.command):
        chan.send(cmd + "\n")
        out = read_until_prompt()
        text = out.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and cmd in lines[0]:  # the echo of the command
        lines = lines[1:]
    if lines and prompt.search(lines[-1]):  # the prompt that came back
        lines = lines[:-1]
    return "\n".join(lines)
