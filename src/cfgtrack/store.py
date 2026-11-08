"""The Git repository where configurations live: one file per device, one commit per real change."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# A revision is a commit id or HEAD~n. Anything else (a branch, a range, an option) is refused, so a value taken from
# the command line can never turn into a git option.
REV_RE = re.compile(r"(?:[0-9a-fA-F]{4,64}|HEAD)(?:~[0-9]{1,6})?")
_SEP = "\x1f"


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Commit:
    id: str
    when: datetime
    subject: str


def check_name(name: str) -> str:
    if not NAME_RE.fullmatch(name) or ".." in name:
        raise ValueError(f"bad device name {name!r} (letters, digits, '.', '_', '-'; up to 64)")
    return name


def check_rev(rev: str) -> str:
    if not REV_RE.fullmatch(rev):
        raise ValueError(f"bad revision {rev!r} (use a commit id, HEAD or either with ~n)")
    return rev


class Repo:
    def __init__(self, path: Path, git: str = "git") -> None:
        self.path = path
        self.git = git

    # ---- plumbing ----

    def _run(
        self, *args: str, env: dict[str, str] | None = None, ok: tuple[int, ...] = (0,), data: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        if args[0] != "init" and not (self.path / ".git").exists():
            raise GitError(f"{self.path} is not a backup repository yet (run: cfgtrack backup)")
        base = {
            k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in ("GIT_EXEC_PATH", "GIT_SSL_CAINFO")
        }
        base.update(
            {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        )
        base.update(env or {})
        cmd = [
            self.git,
            "-c", "core.autocrlf=false",
            "-c", "core.safecrlf=false",
            "-c", "commit.gpgsign=false",
            "-c", f"core.hooksPath={os.devnull}",
            "-c", f"safe.directory={self.path.resolve().as_posix()}",
            "-c", "user.name=cfgtrack",
            "-c", "user.email=cfgtrack@localhost",
            *args,
        ]  # fmt: skip
        try:
            r = subprocess.run(cmd, cwd=self.path, env=base, input=data, capture_output=True, timeout=120, check=False)
        except FileNotFoundError:
            raise GitError(f"the git program was not found ({self.git!r}); install git") from None
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} timed out") from None
        if r.returncode not in ok:
            msg = r.stderr.decode("utf-8", "replace").strip().splitlines()
            raise GitError(f"git {args[0]} failed: {msg[-1] if msg else r.returncode}")
        return r

    # ---- repository ----

    def init(self) -> None:
        (self.path / "devices").mkdir(parents=True, exist_ok=True)
        if (self.path / ".git").exists():
            return
        self._run("init", "-q", "-b", "main")
        # the state file and any key must never be committed
        (self.path / ".gitignore").write_bytes(b"state.json\nstate.json.tmp\n*.key\n")
        self._run("add", "--", ".gitignore")
        self._run("commit", "-q", "-m", "cfgtrack: initialise the repository")

    def device_file(self, name: str) -> Path:
        return self.path / "devices" / f"{check_name(name)}.cfg"

    def _rel(self, name: str) -> str:
        return f"devices/{check_name(name)}.cfg"

    def devices(self) -> list[str]:
        d = self.path / "devices"
        return sorted(p.stem for p in d.glob("*.cfg")) if d.is_dir() else []

    # ---- writing ----

    def commit(self, name: str, text: str, message: str, when: datetime | None = None) -> str | None:
        """Store `text` as the device's file. Returns the new commit id, or None if nothing changed."""
        rel = self._rel(name)
        target = self.device_file(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        self._run("add", "--", rel)
        if self._run("diff", "--cached", "--quiet", "--", rel, ok=(0, 1)).returncode == 0:
            return None
        env = {}
        if when is not None:
            env = {"GIT_AUTHOR_DATE": when.isoformat(), "GIT_COMMITTER_DATE": when.isoformat()}
        self._run("commit", "-q", "-m", message, env=env)
        return self._run("rev-parse", "--short=10", "HEAD").stdout.decode().strip()

    # ---- reading ----

    def read(self, name: str, rev: str = "HEAD") -> str | None:
        """The file as it was at `rev`, or None if the device did not exist then (or there is no commit yet)."""
        r = self._run("show", f"{check_rev(rev)}:{self._rel(name)}", ok=(0, 128))
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None

    def log(self, name: str, limit: int | None = None, since: datetime | None = None) -> list[Commit]:
        args = ["log", f"--format=%h{_SEP}%aI{_SEP}%s"]
        if limit is not None:
            args.append(f"-n{int(limit)}")
        r = self._run(*args, "--", self._rel(name), ok=(0, 128))  # 128: no commit yet
        out = []
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            h, when, subject = line.split(_SEP, 2)
            out.append(Commit(h, datetime.fromisoformat(when), subject))
        # filtered here, not with `git log --since`, which stops at the first old commit and so misses commits
        # when the clock of the machine was ever set back
        return [c for c in out if since is None or c.when >= since]

    def commit_count(self) -> int:
        r = self._run("rev-list", "--count", "HEAD", ok=(0, 128))
        return int(r.stdout.decode().strip() or 0) if r.returncode == 0 else 0

    def size_bytes(self) -> int:
        """Size of the object database after packing (what a clone would have to download)."""
        self._run("gc", "-q", "--aggressive", "--prune=now")
        for line in self._run("count-objects", "-v").stdout.decode().splitlines():
            if line.startswith("size-pack:"):
                return int(line.split(":")[1]) * 1024
        return 0

    def all_text(self) -> str:
        """Every line ever committed, for the leak check (git log -p over all history)."""
        r = self._run("log", "-p", "--all", "--no-color", ok=(0, 128))
        return r.stdout.decode("utf-8", "replace")
