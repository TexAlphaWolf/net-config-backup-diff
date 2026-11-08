"""Command line. Exit codes: 0 fine, 1 a problem worth an alert (a device failed, or a change with --fail-on-change),
2 a mistake in how it was called or configured."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cfgtrack import __version__
from cfgtrack.config import DEFAULT_TOML, Config, ConfigError, load, parse_duration
from cfgtrack.diffing import diff_text, format_changes
from cfgtrack.fetchers import FetchError, host_key_of
from cfgtrack.normalize import normalize
from cfgtrack.render import build_report, html_page, markdown, status_rows, status_text, to_json
from cfgtrack.runner import State, load_key, run_backup
from cfgtrack.store import GitError, Repo, check_name, check_rev

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")


def parse_since(value: str, now: datetime | None = None) -> datetime:
    """'7d', '12h' (that long ago) or a date such as 2026-11-01 or '2026-11-01 08:00' (UTC)."""
    now = now or datetime.now(UTC)
    v = value.strip()
    if _DATE.fullmatch(v):
        return datetime.fromisoformat(v.replace(" ", "T")).replace(tzinfo=UTC)
    try:
        return now - timedelta(seconds=parse_duration(v))
    except ConfigError:
        raise ValueError(f"bad time {value!r} (use 7d, 12h, 2026-11-01 or '2026-11-01 08:00')") from None


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cfgtrack", description=__doc__.split("\n")[0] if __doc__ else None)
    p.add_argument("--version", action="version", version=f"cfgtrack {__version__}")
    p.add_argument("-c", "--config", type=Path, default=Path("cfgtrack.toml"), help="configuration file")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init-config", help="write an example configuration file")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("hostkey", help="print the fingerprint of an SSH server, to pin it in the configuration")
    s.add_argument("host")
    s.add_argument("--port", type=int, default=22)

    s = sub.add_parser("backup", help="fetch every device and store what changed")
    s.add_argument("devices", nargs="*", help="only these devices")
    s.add_argument("--fail-on-change", action="store_true", help="exit 1 when any configuration changed")
    s.add_argument("--accept-shrink", action="append", default=[], metavar="DEVICE", help="store a much smaller config")
    s.add_argument("--diff", action="store_true", help="print the changes")

    s = sub.add_parser("diff", help="show what changed in one device between two revisions")
    s.add_argument("device")
    s.add_argument("--from", dest="rev_from", default="HEAD~1")
    s.add_argument("--to", dest="rev_to", default="HEAD")
    s.add_argument("--format", choices=["text", "json"], default="text")

    s = sub.add_parser("log", help="the history of one device")
    s.add_argument("device")
    s.add_argument("-n", type=int, default=20)

    s = sub.add_parser("show", help="print a stored configuration")
    s.add_argument("device")
    s.add_argument("--rev", default="HEAD")

    s = sub.add_parser("report", help="what changed, on which device, when")
    s.add_argument("--since", default="7d")
    s.add_argument("--format", choices=["md", "json", "html"], default="md")
    s.add_argument("--output", type=Path)

    s = sub.add_parser("noise", help="suggest 'ignore' patterns for lines that change on every backup")
    s.add_argument("device")
    s.add_argument("-n", type=int, default=20, help="how many recent commits to look at")

    sub.add_parser("status", help="last backup of every device; exit 1 if one is stale or failing")
    sub.add_parser("verify", help="check the stored files for passwords the rules do not catch")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (GitError, FetchError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace) -> int:
    if args.cmd == "init-config":
        if args.config.exists() and not args.force:
            print(f"error: {args.config} exists (use --force to overwrite)", file=sys.stderr)
            return 2
        args.config.write_text(DEFAULT_TOML, encoding="utf-8")
        print(f"wrote {args.config}")
        return 0
    if args.cmd == "hostkey":
        kind, fp = host_key_of(args.host, args.port)
        print(f'host_key = "{fp}"   # {kind}')
        return 0

    cfg = load(args.config)
    repo = Repo(cfg.store)
    state = State(cfg.store / "state.json")
    styles = {d.name: d.profile.style for d in cfg.devices}

    if args.cmd == "backup":
        return _backup(args, cfg, repo, state)
    if args.cmd == "diff":
        check_rev(args.rev_from), check_rev(args.rev_to)
        old = repo.read(args.device, args.rev_from)
        new = repo.read(args.device, args.rev_to)
        if new is None:
            raise ValueError(f"no configuration stored for {args.device!r} at {args.rev_to}")
        changes = diff_text(old or "", new, styles.get(args.device, "indent"))
        if args.format == "json":
            import json

            print(json.dumps([c.as_dict() for c in changes], indent=2))
        else:
            print(format_changes(changes) or "no changes")
        return 0
    if args.cmd == "log":
        commits = repo.log(args.device, limit=args.n)
        for c in commits:
            print(f"{c.id}  {c.when:%Y-%m-%d %H:%M}  {c.subject}")
        if not commits:
            print(f"no history for {check_name(args.device)!r}")
        return 0
    if args.cmd == "show":
        text = repo.read(args.device, args.rev)
        if text is None:
            raise ValueError(f"no configuration stored for {args.device!r} at {args.rev}")
        sys.stdout.write(text)
        return 0
    if args.cmd == "report":
        since = parse_since(args.since)
        entries = build_report(repo, styles, since)
        out = {"md": markdown, "html": html_page}.get(args.format)
        text = out(entries, since) if out else to_json(entries)
        if args.output:
            args.output.write_text(text, encoding="utf-8")
            print(f"wrote {args.output}")
        else:
            print(text)
        return 0
    if args.cmd == "noise":
        return _noise(args, repo, styles)
    if args.cmd == "status":
        rows = status_rows(cfg, state)
        print(status_text(rows))
        return 1 if any(r["stale"] or r["failures"] for r in rows) else 0
    if args.cmd == "verify":
        return _verify(cfg, repo)
    raise AssertionError(args.cmd)  # pragma: no cover


def _backup(args: argparse.Namespace, cfg: Config, repo: Repo, state: State) -> int:
    known = {d.name for d in cfg.devices}
    for n in [*args.devices, *args.accept_shrink]:
        if n not in known:
            raise ValueError(f"no device named {n!r} in the configuration")
    key = load_key(cfg.key_file)
    results = run_backup(cfg, repo, state, key, args.devices or None, args.accept_shrink)
    w = max((len(r.name) for r in results), default=6)
    for r in results:
        print(f"{r.name.ljust(w)}  {r.status:<9}  {r.message}".rstrip())
        if args.diff and r.changes:
            print(format_changes(r.changes))
    changed = sum(1 for r in results if r.status == "changed")
    failed = sum(1 for r in results if r.failed)
    print(f"{len(results)} device(s): {changed} changed, {failed} failed")
    if failed:
        return 1
    return 1 if args.fail_on_change and changed else 0


def _noise(args: argparse.Namespace, repo: Repo, styles: dict[str, str]) -> int:
    from cfgtrack.noise import suggest_ignores

    check_name(args.device)
    found = suggest_ignores(repo, args.device, styles.get(args.device, "indent"), args.n)
    if not found:
        print("no line changes on (almost) every backup: nothing to suggest")
        return 0
    print("# lines of this shape changed in most backups; add what is really noise to the device section:")
    print("ignore = [")
    for sg in found:
        warn = f"   # matches {sg.matches_now} lines now: check" if sg.matches_now != 1 else ""
        print(f"  {_toml(sg.pattern)},  # changed in {sg.share:.0%} of backups{warn}")
    print("]")
    return 0


def _toml(pattern: str) -> str:
    return "'" + pattern + "'" if "'" not in pattern else '"' + pattern.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _verify(cfg: Config, repo: Repo) -> int:
    from cfgtrack.normalize import redact_line

    key = load_key(cfg.key_file)
    problems = 0
    by_name = {d.name: d for d in cfg.devices}
    for name in repo.devices():
        dev = by_name.get(name)
        if dev is None:
            continue
        text = repo.read(name) or ""
        for n, line in enumerate(text.splitlines(), 1):
            _, hits = redact_line(line, dev.profile, key)
            if hits:
                problems += 1
                print(f"{name}:{n}: a secret is stored in clear text")
        again = normalize(text, dev.profile, key).text
        if again != text:
            problems += 1
            print(f"{name}: stored text is not what the current rules would store (rules changed?)")
    print(f"{problems} problem(s)")
    return 1 if problems else 0
