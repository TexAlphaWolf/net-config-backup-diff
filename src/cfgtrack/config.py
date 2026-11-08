"""The TOML configuration, validated strictly: unknown keys and plaintext secrets are errors, not warnings."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cfgtrack.profiles import Profile, get_profile
from cfgtrack.store import check_name

KINDS = ("ssh", "command", "file", "http")
DEFAULT_COMMANDS = {
    "cisco_ios": "show running-config",
    "junos": "show configuration",
    "routeros": "/export",
    "generic": "",
}
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*([smhdw])")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class ConfigError(ValueError):
    pass


def parse_duration(value: str | int | float) -> float:
    """'90s', '15m', '2d', '1w' or a number of seconds."""
    if isinstance(value, bool):
        raise ConfigError("a duration must be a number or a string like '2d'")
    if isinstance(value, int | float):
        return float(value)
    m = _DURATION.fullmatch(value.strip())
    if not m:
        raise ConfigError(f"bad duration {value!r} (use for example 90s, 15m, 2d, 1w)")
    return float(m.group(1)) * _UNIT[m.group(2)]


@dataclass(frozen=True)
class Device:
    name: str
    profile: Profile
    fetch: str
    timeout: float = 30.0
    # ssh
    host: str = ""
    port: int = 22
    username: str = ""
    password_env: str = ""
    key_file: str = ""
    passphrase_env: str = ""
    host_key: str = ""  # "SHA256:..." pinned fingerprint
    known_hosts: str = ""
    mode: str = "exec"  # exec | shell
    prompt: str = r"[\w.@:/()\-]+[>#]\s*$"
    before: tuple[str, ...] = ()  # commands sent first in shell mode, e.g. "terminal length 0"
    # ssh and command
    command: str = ""
    argv: tuple[str, ...] = ()
    # file
    path: str = ""
    # http
    url: str = ""
    token_env: str = ""
    max_bytes: int = 20_000_000


@dataclass(frozen=True)
class Config:
    store: Path
    key_file: Path
    workers: int = 8
    shrink_guard: float = 0.5
    stale_after: float = 2 * 86400.0
    devices: tuple[Device, ...] = field(default_factory=tuple)


DEFAULT_TOML = """\
# cfgtrack configuration. Secrets are never written here: passwords and tokens come from environment variables.

[store]
path = "backups"            # a Git repository, created on first use
key_file = "cfgtrack.key"   # secret for the tags that replace passwords; created on first use, keep it out of Git
workers = 8                 # devices fetched in parallel
shrink_guard = 0.5          # a new configuration with fewer than this share of the old lines is not committed
stale_after = "2d"          # `cfgtrack status` flags a device not backed up successfully for this long

[[device]]
name = "core-sw1"
profile = "cisco_ios"       # cisco_ios | junos | routeros | generic
fetch = "ssh"
host = "192.0.2.10"
username = "backup"
password_env = "CORE_SW1_PASSWORD"
host_key = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"   # placeholder: run `cfgtrack hostkey 192.0.2.10`

[[device]]
name = "edge-fw"
profile = "generic"
fetch = "command"           # any program that prints the configuration, without a shell
argv = ["ssh", "-o", "BatchMode=yes", "backup@192.0.2.20", "show", "config"]

[[device]]
name = "lab-ap"
profile = "generic"
fetch = "http"
url = "https://192.0.2.30/api/config"
token_env = "LAB_AP_TOKEN"
"""

_STORE_KEYS = {"path", "key_file", "workers", "shrink_guard", "stale_after"}
_DEVICE_KEYS = {
    "name", "profile", "fetch", "timeout", "host", "port", "username", "password_env", "key_file", "passphrase_env",
    "host_key", "known_hosts", "mode", "prompt", "before", "command", "argv", "path", "url", "token_env", "max_bytes",
    "ignore", "redact",
}  # fmt: skip
_FORBIDDEN = ("password", "passphrase", "token", "secret")


def _check_keys(table: dict[str, Any], allowed: set[str], where: str) -> None:
    for k in table:
        if k in _FORBIDDEN:
            raise ConfigError(
                f"{where}: '{k}' must not be written in the file; use {k}_env to name an environment variable"
            )
        if k not in allowed:
            raise ConfigError(f"{where}: unknown key '{k}'")


def _str(t: dict[str, Any], k: str, default: str = "") -> str:
    v = t.get(k, default)
    if not isinstance(v, str):
        raise ConfigError(f"'{k}' must be a string")
    return v


def _strs(t: dict[str, Any], k: str) -> tuple[str, ...]:
    v = t.get(k, [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ConfigError(f"'{k}' must be a list of strings")
    return tuple(v)


def _num(t: dict[str, Any], k: str, default: float, lo: float, hi: float) -> float:
    v = t.get(k, default)
    if isinstance(v, bool) or not isinstance(v, int | float) or not lo <= v <= hi:
        raise ConfigError(f"'{k}' must be a number from {lo:g} to {hi:g}")
    return float(v)


def _device(t: dict[str, Any], n: int) -> Device:
    where = f"device #{n}"
    _check_keys(t, _DEVICE_KEYS, where)
    try:
        name = check_name(_str(t, "name"))
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from None
    where = f"device {name!r}"
    pname = _str(t, "profile", "generic")
    try:
        profile = get_profile(pname, _strs(t, "ignore"), _strs(t, "redact"))
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from None
    fetch = _str(t, "fetch")
    if fetch not in KINDS:
        raise ConfigError(f"{where}: 'fetch' must be one of {', '.join(KINDS)}")
    mode = _str(t, "mode", "exec")
    if mode not in ("exec", "shell"):
        raise ConfigError(f"{where}: 'mode' must be exec or shell")
    prompt = _str(t, "prompt", Device.prompt)
    try:
        re.compile(prompt)
    except re.error as exc:
        raise ConfigError(f"{where}: bad prompt pattern: {exc}") from None
    dev = Device(
        name=name,
        profile=profile,
        fetch=fetch,
        timeout=_num(t, "timeout", 30, 1, 600),
        host=_str(t, "host"),
        port=int(_num(t, "port", 22, 1, 65535)),
        username=_str(t, "username"),
        password_env=_str(t, "password_env"),
        key_file=_str(t, "key_file"),
        passphrase_env=_str(t, "passphrase_env"),
        host_key=_str(t, "host_key"),
        known_hosts=_str(t, "known_hosts"),
        mode=mode,
        prompt=prompt,
        before=_strs(t, "before"),
        command=_str(t, "command", DEFAULT_COMMANDS.get(pname, "")),
        argv=_strs(t, "argv"),
        path=_str(t, "path"),
        url=_str(t, "url"),
        token_env=_str(t, "token_env"),
        max_bytes=int(_num(t, "max_bytes", 20_000_000, 1000, 500_000_000)),
    )
    _validate(dev, where)
    return dev


def _validate(d: Device, where: str) -> None:
    if d.fetch == "ssh":
        if not d.host or not d.username:
            raise ConfigError(f"{where}: ssh needs 'host' and 'username'")
        if not d.command:
            raise ConfigError(f"{where}: ssh needs a 'command' (the profile has no default)")
        if not (d.password_env or d.key_file):
            raise ConfigError(f"{where}: ssh needs 'password_env' or 'key_file'")
        if not (d.host_key or d.known_hosts):
            raise ConfigError(
                f"{where}: ssh needs 'host_key' (a pinned fingerprint) or 'known_hosts'; host keys are never accepted blindly"
            )
        if d.host_key and not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", d.host_key):
            raise ConfigError(
                f"{where}: 'host_key' must look like SHA256:<43 characters>; get it with: cfgtrack hostkey {d.host}"
            )
    elif d.fetch == "command":
        if not d.argv:
            raise ConfigError(f"{where}: command needs 'argv', a list (no shell is involved)")
    elif d.fetch == "file":
        if not d.path:
            raise ConfigError(f"{where}: file needs 'path'")
    elif d.fetch == "http":
        u = urlsplit(d.url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ConfigError(f"{where}: 'url' must be http:// or https://")
        if u.username or u.password:
            raise ConfigError(f"{where}: put credentials in token_env, not in the URL")


def load(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"configuration file {path} not found (create one with: cfgtrack init-config)") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    return parse(raw, path.parent)


def parse(raw: dict[str, Any], base: Path) -> Config:
    for k in raw:
        if k not in ("store", "device"):
            raise ConfigError(f"unknown section '{k}'")
    store = raw.get("store", {})
    if not isinstance(store, dict):
        raise ConfigError("[store] must be a table")
    _check_keys(store, _STORE_KEYS, "[store]")
    guard = _num(store, "shrink_guard", 0.5, 0, 1)
    devices_raw = raw.get("device", [])
    if not isinstance(devices_raw, list) or not all(isinstance(x, dict) for x in devices_raw):
        raise ConfigError("[[device]] must be an array of tables")
    devices = tuple(_device(t, i + 1) for i, t in enumerate(devices_raw))
    names = [d.name for d in devices]
    if len(set(names)) != len(names):
        raise ConfigError("two devices have the same name")
    return Config(
        store=base / _str(store, "path", "backups"),
        key_file=base / _str(store, "key_file", "cfgtrack.key"),
        workers=int(_num(store, "workers", 8, 1, 64)),
        shrink_guard=guard,
        stale_after=parse_duration(store.get("stale_after", "2d")),
        devices=devices,
    )
