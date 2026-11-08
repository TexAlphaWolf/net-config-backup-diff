from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from cfgtrack.config import Config, Device
from cfgtrack.profiles import GENERIC
from cfgtrack.render import build_report, html_page, markdown, status_rows, status_text, to_json
from cfgtrack.runner import State
from cfgtrack.store import Repo

T0 = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)


def history(repo: Repo) -> None:
    repo.commit("sw1", "hostname sw1\ninterface A\n description x\n", "sw1: initial backup (3 lines)", T0)
    repo.commit(
        "sw1",
        "hostname sw1\ninterface A\n description <script>alert(1)</script>\n",
        "sw1: +1 -1",
        T0 + timedelta(days=2),
    )
    repo.commit("sw2", "hostname sw2\n", "sw2: initial backup (1 lines)", T0 + timedelta(days=1))


def test_report_lists_commits_with_their_diffs(repo: Repo) -> None:
    history(repo)
    entries = build_report(repo, {"sw1": "indent"}, None)
    assert [(e.device, e.added, e.removed) for e in entries] == [("sw1", 0, 0), ("sw2", 0, 0), ("sw1", 1, 1)]
    assert len(build_report(repo, {}, T0 + timedelta(days=1, hours=12))) == 1  # only the last commit is that recent


def test_markdown(repo: Repo) -> None:
    history(repo)
    md = markdown(build_report(repo, {}, None), None)
    assert "1 change(s) on 1 device(s); 2 initial backup(s). Lines: +1 -1." in md
    assert "## sw1 - 2026-11-03 08:00" in md and "```diff" in md
    assert markdown([], T0).endswith("No changes.\n") and "since 2026-11-01 08:00" in markdown([], T0)


def test_json(repo: Repo) -> None:
    history(repo)
    import json

    data = json.loads(to_json(build_report(repo, {}, None)))
    assert data[2]["device"] == "sw1" and data[2]["changes"][0]["path"] == ["interface A"]


def test_html_is_one_page_without_scripts_and_escapes_everything(repo: Repo) -> None:
    history(repo)
    page = html_page(build_report(repo, {}, None), T0)
    assert page.startswith("<!doctype html>") and "<script" not in page and "http" not in page.replace("viewport", "")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "No changes" in html_page([], None)


def cfg_for(tmp_path: Path) -> Config:
    devs = tuple(Device(name=n, profile=GENERIC, fetch="file") for n in ("a", "b", "c"))
    return Config(store=tmp_path, key_file=tmp_path / "k", devices=devs, stale_after=86400)


def test_status(tmp_path: Path) -> None:
    state = State(tmp_path / "s.json")
    now = T0 + timedelta(hours=30)
    state.get("a").update(last_ok=(now - timedelta(hours=1)).isoformat(), last_change=T0.isoformat())
    state.get("b").update(last_ok=(now - timedelta(hours=26)).isoformat(), failures=3, last_error="timeout after 30 s")
    rows = status_rows(cfg_for(tmp_path), state, now)
    assert [(r["device"], r["stale"], r["failures"]) for r in rows] == [("a", False, 0), ("b", True, 3), ("c", True, 0)]
    text = status_text(rows)
    assert "timeout after 30 s" in text and text.count("STALE") == 2 and "never" in text
    assert status_text([]) == "no devices configured"
