"""One backup pass: fetch every device in parallel, then decide device by device what to store."""

from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cfgtrack.config import Config, Device
from cfgtrack.diffing import Change, diff_text, format_changes, summarize
from cfgtrack.fetchers import FetchError, fetch
from cfgtrack.normalize import Normalized, implausible, normalize
from cfgtrack.store import Repo

Fetcher = Callable[[Device, Mapping[str, str]], str]
GUARD_MIN_LINES = 10  # a tiny configuration may legitimately lose half of its lines


def load_key(path: Path) -> bytes:
    """The secret for the tags that replace passwords. Created on first use, readable by the owner only."""
    if path.exists():
        try:
            key = bytes.fromhex(path.read_text(encoding="ascii").strip())
        except ValueError:
            raise ValueError(f"{path} is not a cfgtrack key file (expected 64 hex characters)") from None
        if len(key) < 16:
            raise ValueError(f"{path}: the key is too short")
        return key
    key = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(key.hex() + "\n")
    return key


@dataclass
class Result:
    name: str
    status: str  # new | changed | unchanged | error | suspect
    message: str = ""
    added: int = 0
    removed: int = 0
    lines: int = 0
    commit: str | None = None
    seconds: float = 0.0
    changes: list[Change] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status in ("error", "suspect")


class State:
    """Per device: when it last worked, when it last changed, how many times in a row it failed. Not in Git."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.devices: dict[str, dict[str, object]] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("devices"), dict):
                    self.devices = data["devices"]
            except (OSError, json.JSONDecodeError):
                self.devices = {}  # a damaged state file only loses the history of failures

    def get(self, name: str) -> dict[str, object]:
        return self.devices.setdefault(name, {"last_ok": "", "last_change": "", "last_error": "", "failures": 0})

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"devices": self.devices}, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


def _collect(dev: Device, key: bytes, fetcher: Fetcher, env: Mapping[str, str]) -> tuple[Normalized | None, str, float]:
    t0 = time.perf_counter()
    try:
        raw = fetcher(dev, env)
    except FetchError as exc:
        return None, str(exc), time.perf_counter() - t0
    norm = normalize(raw, dev.profile, key)
    return norm, implausible(norm, dev.profile) or "", time.perf_counter() - t0


def run_backup(
    cfg: Config,
    repo: Repo,
    state: State,
    key: bytes,
    names: Iterable[str] | None = None,
    accept_shrink: Iterable[str] = (),
    now: datetime | None = None,
    fetcher: Fetcher = fetch,
    env: Mapping[str, str] | None = None,
) -> list[Result]:
    env = os.environ if env is None else env
    now = now or datetime.now(UTC)
    wanted = set(names) if names is not None else None
    accept = set(accept_shrink)
    devices = [d for d in cfg.devices if wanted is None or d.name in wanted]
    repo.init()
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        fetched = list(pool.map(lambda d: _collect(d, key, fetcher, env), devices))

    results = []
    for dev, (norm, problem, secs) in zip(devices, fetched, strict=True):
        res = _decide(dev, norm, problem, repo, cfg.shrink_guard, dev.name in accept, now)
        res.seconds = secs
        st = state.get(dev.name)
        if res.failed:
            st["failures"] = int(str(st["failures"])) + 1
            st["last_error"] = res.message
        else:
            st["failures"] = 0
            st["last_error"] = ""
            st["last_ok"] = now.isoformat()
            if res.status in ("new", "changed"):
                st["last_change"] = now.isoformat()
        results.append(res)
    state.save()
    return results


def _decide(
    dev: Device, norm: Normalized | None, problem: str, repo: Repo, guard: float, accept: bool, now: datetime
) -> Result:
    if norm is None or problem:
        return Result(dev.name, "error", problem)
    old = repo.read(dev.name)
    if old is None:
        commit = repo.commit(dev.name, norm.text, f"{dev.name}: initial backup ({norm.lines} lines)", now)
        return Result(dev.name, "new", f"{norm.lines} lines", lines=norm.lines, commit=commit)
    if old == norm.text:
        return Result(dev.name, "unchanged", lines=norm.lines)
    old_lines = old.count("\n")
    if not accept and old_lines >= GUARD_MIN_LINES and norm.lines < old_lines * guard:
        return Result(
            dev.name,
            "suspect",
            f"the configuration shrank from {old_lines} to {norm.lines} lines; not stored "
            f"(if this is right, run: cfgtrack backup --accept-shrink {dev.name})",
            lines=norm.lines,
        )
    changes = diff_text(old, norm.text, dev.profile.style)
    s = summarize(changes)
    subject = f"{dev.name}: +{s.added} -{s.removed}"
    body = format_changes(changes, limit=80) if changes else "formatting only (indentation changed)"
    commit = repo.commit(dev.name, norm.text, f"{subject}\n\n{body}", now)
    return Result(dev.name, "changed", subject, s.added, s.removed, norm.lines, commit, changes=changes)
