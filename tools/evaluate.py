"""Measure cfgtrack against changes that are known because they were injected.

    python tools/evaluate.py [--quick] [--only fetch,pipeline,noise,cost] [--out docs/report.json]

fetch     the same device fetched over SSH (exec and interactive shell), HTTP, a command and a file: is the text
          identical, and how long does it take
pipeline  a simulated network (devices, days) in which configurations really change, run through the whole tool next
          to a naive "commit whatever the device prints" baseline; every change is known in advance
noise     a device with noise the profile does not know: the commits it causes, the suggestion, the commits after
cost      time and space
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import re
import statistics
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

import netgen  # noqa: E402
from cfgtrack.config import Config, Device  # noqa: E402
from cfgtrack.diffing import diff_text  # noqa: E402
from cfgtrack.fetchers import fetch  # noqa: E402
from cfgtrack.noise import suggest_ignores  # noqa: E402
from cfgtrack.normalize import TAG, normalize  # noqa: E402
from cfgtrack.profiles import IOS, get_profile  # noqa: E402
from cfgtrack.runner import State, run_backup  # noqa: E402
from cfgtrack.store import Repo  # noqa: E402
from fakedevice import FakeSshDevice  # noqa: E402

KEY = bytes(range(32))
T0 = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)
P_CHANGE = 0.12  # chance that a device is really reconfigured on a given day
P_FAULT = 0.03  # chance that a fetch hands back garbage instead of a configuration


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, round(p / 100 * (len(s) - 1)))]


def ms(values: list[float]) -> dict[str, float]:
    return {"median_ms": round(statistics.median(values) * 1000, 2), "p95_ms": round(pct(values, 95) * 1000, 2)}


# ---------------------------------------------------------------------------------------------------------------- fetch


def section_fetch(quick: bool) -> dict[str, object]:
    import http.server
    import threading

    n = 10 if quick else 40
    rng = random.Random(1)
    model = netgen.make_model(rng, "lab-r1", ports=24)
    text = netgen.render(model, rng)
    want = normalize(text, IOS, KEY).text
    out: dict[str, dict[str, object]] = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: object) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as tmp, FakeSshDevice(text, allow_shell=True) as fake:
        f = Path(tmp) / "c.cfg"
        f.write_text(text)
        base = {"name": "r", "profile": IOS, "timeout": 20.0}
        ssh = {
            **base, "fetch": "ssh", "host": "127.0.0.1", "port": fake.port, "username": "backup",
            "password_env": "PW", "host_key": fake.fingerprint, "command": "show running-config",
        }  # fmt: skip
        kinds = {
            "ssh exec": Device(**{**ssh, "mode": "exec"}),  # type: ignore[arg-type]
            "ssh shell": Device(**{**ssh, "mode": "shell", "before": ("terminal length 0",)}),  # type: ignore[arg-type]
            "http": Device(**{**base, "fetch": "http", "url": f"http://127.0.0.1:{srv.server_address[1]}/c"}),  # type: ignore[arg-type]
            "command": Device(
                **{
                    **base,
                    "fetch": "command",
                    "argv": (sys.executable, "-c", f"print(open({str(f)!r}).read(), end='')"),
                }
            ),  # type: ignore[arg-type]
            "file": Device(**{**base, "fetch": "file", "path": str(f)}),  # type: ignore[arg-type]
        }
        for label, dev in kinds.items():
            times, same = [], 0
            for _ in range(n):
                t0 = time.perf_counter()
                got = fetch(dev, {"PW": "s3cret-pw"})
                times.append(time.perf_counter() - t0)
                same += normalize(got, IOS, KEY).text == want
            out[label] = {"fetches": n, "identical": same, **ms(times)}
    srv.shutdown()
    return {"config_lines": text.count("\n"), "kinds": out}


# ------------------------------------------------------------------------------------------------------------ pipeline


def section_pipeline(quick: bool) -> dict[str, object]:
    n_dev, days = (8, 12) if quick else (40, 60)
    rng = random.Random(2026)
    names = [f"sw{i:02}" for i in range(n_dev)]
    models = {n: netgen.make_model(rng, n, ports=rng.choice([24, 48])) for n in names}
    secrets_seen: set[str] = set()

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        cfg = Config(
            store=base / "ours", key_file=base / "k", workers=8,
            devices=tuple(Device(name=n, profile=IOS, fetch="file") for n in names),
        )  # fmt: skip
        ours, naive = Repo(cfg.store), Repo(base / "naive")
        naive.init()
        state = State(base / "state.json")
        prev_raw: dict[str, str] = {}

        ev = Counter()
        by_kind: dict[str, Counter[str]] = {k: Counter() for k in netgen.MUTATIONS}
        fault_by_kind: dict[str, Counter[str]] = {k: Counter() for k in netgen.FAULTS}
        noise_lines: list[int] = []
        problems: list[str] = []
        run_secs: list[float] = []

        for day in range(days):
            now = T0 + timedelta(days=day)
            raw: dict[str, str] = {}
            truth: dict[str, tuple[list[str], list[netgen.ChangeKey], str | None]] = {}
            for n in names:
                m = models[n]
                kinds: list[str] = []
                expected: list[netgen.ChangeKey] = []
                flt = None
                if day > 0 and rng.random() < P_FAULT:
                    flt = rng.choice(netgen.FAULTS)
                elif day > 0 and rng.random() < P_CHANGE:
                    kinds, expected = netgen.mutate(m, rng, rng.choice([1, 1, 2, 3]))
                text = netgen.render(m, rng)
                if flt:
                    text = netgen.fault(text, rng, flt)
                secrets_seen |= m.all_secrets()
                raw[n], truth[n] = text, (kinds, expected, flt)
                if n in prev_raw and not kinds and not flt:
                    a, b = Counter(prev_raw[n].splitlines()), Counter(text.splitlines())
                    noise_lines.append(sum(((a - b) + (b - a)).values()))
                if not flt:
                    prev_raw[n] = text
                naive.commit(n, text, f"{n} day {day}", now)  # the baseline stores whatever it was handed

            t0 = time.perf_counter()
            results = run_backup(cfg, ours, state, KEY, now=now, fetcher=lambda d, e, raw=raw: raw[d.name])
            run_secs.append(time.perf_counter() - t0)

            for r in results:
                kinds, expected, flt = truth[r.name]
                ev["rounds"] += 1
                if day == 0:
                    ev["initial"] += 1
                    continue
                if flt:
                    ev["faults"] += 1
                    fault_by_kind[flt]["injected"] += 1
                    if r.failed:
                        ev["faults_blocked"] += 1
                        fault_by_kind[flt]["blocked"] += 1
                    else:
                        problems.append(f"day {day} {r.name}: stored a {flt} fetch")
                    continue
                if not kinds:
                    ev["quiet_rounds"] += 1
                    if r.status == "changed":
                        ev["false_commits"] += 1
                        problems.append(f"day {day} {r.name}: committed on a day without a change")
                    continue
                ev["change_events"] += 1
                for k in kinds:
                    by_kind[k]["events"] += 1
                if r.status != "changed":
                    ev["missed"] += 1
                    problems.append(f"day {day} {r.name}: missed {kinds} (status {r.status})")
                    continue
                ev["detected"] += 1
                for k in kinds:
                    by_kind[k]["detected"] += 1
                if "acl_swap" in kinds:
                    ev["reorders"] += 1
                    acl_path = {e[1] for e in expected if e[0] == "reorder"}
                    in_acl = [c for c in r.changes if c.path in acl_path]
                    added = Counter(c.line for c in in_acl if c.kind == "added")
                    removed = Counter(c.line for c in in_acl if c.kind == "removed")
                    rest_want = {e for e in expected if e[0] != "reorder"}
                    rest_got = {
                        (c.kind, c.path, TAG.sub("<secret:*>", c.line)) for c in r.changes if c.path not in acl_path
                    }
                    # a pure reorder: the same entries removed and added again, nothing else touched in that list
                    if in_acl and added == removed and rest_got == rest_want:
                        ev["reorders_reported"] += 1
                        for k in kinds:
                            by_kind[k]["exact"] += 1
                    else:
                        problems.append(f"day {day} {r.name}: a reorder was not reported as one")
                    continue
                got = {(c.kind, c.path, TAG.sub("<secret:*>", c.line)) for c in r.changes}
                if got == set(expected) and len(got) == len(r.changes):
                    ev["exact"] += 1
                    for k in kinds:
                        by_kind[k]["exact"] += 1
                else:
                    problems.append(f"day {day} {r.name}: reported {sorted(got)} expected {sorted(set(expected))}")

        ours_text = ours.all_text()
        naive_text = naive.all_text()
        leaked_ours = sum(1 for s in secrets_seen if s in ours_text)
        leaked_naive = sum(1 for s in secrets_seen if s in naive_text)
        ours_size, naive_size = ours.size_bytes(), naive.size_bytes()
        ours_commits, naive_commits = ours.commit_count(), naive.commit_count()
        noise_mean = statistics.mean(noise_lines) if noise_lines else 0.0
        verify_bad = sum(1 for n in names if normalize(ours.read(n) or "", IOS, KEY).text != (ours.read(n) or ""))

    changes_nonswap = ev["change_events"] - ev["reorders"]
    return {
        "devices": n_dev,
        "days": days,
        "rounds": ev["rounds"],
        "p_change": P_CHANGE,
        "p_fault": P_FAULT,
        "change_events": ev["change_events"],
        "detected": ev["detected"],
        "missed": ev["missed"],
        "quiet_rounds": ev["quiet_rounds"],
        "false_commits": ev["false_commits"],
        "exact_reports": ev["exact"],
        "exact_reports_possible": changes_nonswap,
        "reorders": ev["reorders"],
        "reorders_reported": ev["reorders_reported"],
        "by_kind": {k: dict(v) for k, v in by_kind.items()},
        "faults": ev["faults"],
        "faults_blocked": ev["faults_blocked"],
        "faults_by_kind": {k: dict(v) for k, v in fault_by_kind.items()},
        "naive_mean_changed_lines_on_a_quiet_day": round(noise_mean, 1),
        "commits": {"cfgtrack": ours_commits, "naive": naive_commits},
        "repo_bytes": {"cfgtrack": ours_size, "naive": naive_size},
        "secrets": {"distinct": len(secrets_seen), "leaked_cfgtrack": leaked_ours, "leaked_naive": leaked_naive},
        "stored_files_that_would_be_changed_by_the_rules": verify_bad,
        "problems": problems[:10],
        "run_seconds_median": round(statistics.median(run_secs), 3),
    }


# ----------------------------------------------------------------------------------------------------------------- noise


def section_noise(quick: bool) -> dict[str, object]:
    days = 10 if quick else 20
    rng = random.Random(5)
    model = netgen.make_model(rng, "fw1", ports=8)

    def text(day: int) -> str:
        # two kinds of noise the generic profile has never heard of
        extra = f"system-uptime {rng.randint(1000, 999999)} seconds\nlast-sync: 2026-11-{rng.randint(10, 28)} {rng.randint(10, 23)}:{rng.randint(10, 59)}:00\n"
        return netgen.render(model, rng).replace("end\n", "") + extra + "end\n"

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        dev = Device(name="fw1", profile=IOS, fetch="file")
        cfg = Config(store=base / "s", key_file=base / "k", devices=(dev,))
        repo, state = Repo(cfg.store), State(base / "st.json")
        texts = {d: text(d) for d in range(2 * days)}
        for d in range(days):
            run_backup(cfg, repo, state, KEY, now=T0 + timedelta(days=d), fetcher=lambda dv, e, d=d: texts[d])
        before = repo.commit_count() - 1  # the repository's own first commit
        found = suggest_ignores(repo, "fw1", "indent")
        cfg2 = Config(
            store=cfg.store, key_file=cfg.key_file,
            devices=(Device(name="fw1", profile=get_profile("cisco_ios", extra_ignore=tuple(s.pattern for s in found)), fetch="file"),),
        )  # fmt: skip
        # the rules change, so the stored file is rewritten once (that is one commit), then it must stay quiet
        run_backup(cfg2, repo, state, KEY, now=T0 + timedelta(days=days), fetcher=lambda dv, e: texts[days])
        base_commits = repo.commit_count()
        quiet_range = range(days + 1, 2 * days - 1)
        for d in quiet_range:
            run_backup(cfg2, repo, state, KEY, now=T0 + timedelta(days=d), fetcher=lambda dv, e, d=d: texts[d])
        quiet_commits = repo.commit_count() - base_commits
        model.ifaces[3].description = "changed-by-hand"
        real = (
            netgen.render(model, rng).replace("end\n", "")
            + "system-uptime 5 seconds\nlast-sync: 2026-11-30 10:10:00\nend\n"
        )
        r = run_backup(cfg2, repo, state, KEY, now=T0 + timedelta(days=2 * days), fetcher=lambda dv, e: real)[0]
        suggestion_hides_real_lines = any(
            re.search(s.pattern, ln)
            for s in found
            for ln in real.splitlines()
            if "uptime" not in ln and "last-sync" not in ln
        )
        return {
            "days_without_the_rule": days,
            "commits_without_the_rule": before,
            "patterns_suggested": len(found),
            "patterns": [s.pattern for s in found],
            "quiet_days_with_the_rule": len(quiet_range),
            "commits_with_the_rule": quiet_commits,
            "real_change_still_found": r.status == "changed" and (r.added, r.removed) == (1, 1),
            "suggestion_hides_a_real_line": suggestion_hides_real_lines,
        }


# ------------------------------------------------------------------------------------------------------------------ cost


def section_cost(quick: bool) -> dict[str, object]:
    out: dict[str, object] = {}
    rng = random.Random(9)
    for lines_target in (1_000, 10_000) if quick else (1_000, 10_000, 50_000):
        m = netgen.make_model(rng, "big", ports=24)
        m.routes = [
            f"ip route 10.{i // 250}.{i % 250}.0 255.255.255.0 10.0.0.{i % 200 + 2}" for i in range(lines_target)
        ]
        text = netgen.render(m, rng)
        norm = normalize(text, IOS, KEY)
        t0 = time.perf_counter()
        for _ in range(3):
            normalize(text, IOS, KEY)
        t_norm = (time.perf_counter() - t0) / 3
        for _ in range(20):
            m.routes[rng.randrange(len(m.routes))] = f"ip route 192.168.{rng.randint(0, 250)}.0 255.255.255.0 10.9.9.9"
        new = normalize(netgen.render(m, rng), IOS, KEY).text
        t0 = time.perf_counter()
        ch = diff_text(norm.text, new, "indent")
        t_diff = time.perf_counter() - t0
        out[str(lines_target)] = {
            "lines": norm.lines,
            "normalize_ms": round(t_norm * 1000, 1),
            "diff_20_changes_ms": round(t_diff * 1000, 1),
            "changes_reported": len(ch),
        }
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        n = 20 if quick else 100
        devs = tuple(Device(name=f"d{i:03}", profile=IOS, fetch="file") for i in range(n))
        cfg = Config(store=base / "s", key_file=base / "k", workers=8, devices=devs)
        repo, state = Repo(cfg.store), State(base / "st.json")
        texts = {d.name: netgen.render(netgen.make_model(rng, d.name, ports=24), rng) for d in devs}
        t0 = time.perf_counter()
        run_backup(cfg, repo, state, KEY, now=T0, fetcher=lambda d, e: texts[d.name])
        first = time.perf_counter() - t0
        t0 = time.perf_counter()
        run_backup(cfg, repo, state, KEY, now=T0 + timedelta(days=1), fetcher=lambda d, e: texts[d.name])
        again = time.perf_counter() - t0
        out["backup_pass"] = {
            "devices": n,
            "first_pass_s": round(first, 2),
            "quiet_pass_s": round(again, 2),
            "ms_per_device_quiet": round(again / n * 1000, 1),
        }
    return out


SECTIONS = {"fetch": section_fetch, "pipeline": section_pipeline, "noise": section_noise, "cost": section_cost}


def summary(rep: dict[str, object]) -> list[str]:
    lines = []
    if "fetch" in rep:
        k = rep["fetch"]["kinds"]  # type: ignore[index]
        lines.append(
            "fetch: " + ", ".join(f"{name} {v['identical']}/{v['fetches']} identical" for name, v in k.items())
        )
    if "pipeline" in rep:
        p = rep["pipeline"]
        lines.append(f"changes detected: {p['detected']} of {p['change_events']}")  # type: ignore[index]
        lines.append(f"false commits: {p['false_commits']} in {p['quiet_rounds']} quiet rounds")  # type: ignore[index]
        lines.append(f"exact reports: {p['exact_reports']} of {p['exact_reports_possible']}")  # type: ignore[index]
        lines.append(f"bad fetches stored: {p['faults'] - p['faults_blocked']} of {p['faults']}")  # type: ignore[index]
        s = p["secrets"]  # type: ignore[index]
        lines.append(
            f"secrets in history: cfgtrack {s['leaked_cfgtrack']} of {s['distinct']}, naive {s['leaked_naive']}"
        )
    if "noise" in rep:
        nz = rep["noise"]
        lines.append(
            f"noise: {nz['commits_without_the_rule']} commits without the rule, {nz['commits_with_the_rule']} with"
        )  # type: ignore[index]
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", default=",".join(SECTIONS))
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    rep: dict[str, object] = {
        "environment": {
            "python": platform.python_version(),
            "system": f"{platform.system()} {platform.machine()}",
            "quick": args.quick,
        },
    }
    for name in args.only.split(","):
        t0 = time.perf_counter()
        rep[name] = SECTIONS[name](args.quick)
        print(f"[{name}] {time.perf_counter() - t0:.1f} s", file=sys.stderr)
    for line in summary(rep):
        print(line)
    if args.out:
        args.out.write_text(json.dumps(rep, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
