from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cfgtrack.cli import main
from cfgtrack.noise import shape, suggest_ignores
from cfgtrack.store import Repo

T0 = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)


def config(day: int, desc: str = "uplink") -> str:
    return (
        f"hostname fw1\nuptime 4{day:03} seconds\nlast-boot: 2026-11-{day + 1:02} 10:{day:02}:00\n"
        f"interface eth0\n description {desc}\nntp server 10.0.0.1\n"
    )


def fill(repo: Repo, days: int) -> None:
    for d in range(days):
        repo.commit("fw1", config(d), f"day {d}", T0 + timedelta(days=d))


def test_shape_matches_the_same_line_with_other_numbers() -> None:
    rx = re.compile(shape("uptime 4001 seconds"))
    assert rx.search("uptime 77 seconds") and rx.search(" uptime 123456 seconds ")
    assert not rx.search("uptime soon seconds") and not rx.search("uptime 4001 minutes")
    assert re.compile(shape("last-boot: 2026-11-02 10:01:00")).search("last-boot: 2027-01-30 23:59:59")


def test_the_noise_is_found_and_the_real_lines_are_not(repo: Repo) -> None:
    fill(repo, 12)
    found = suggest_ignores(repo, "fw1", "indent")
    assert len(found) == 2
    assert all(s.share == 1.0 and s.matches_now == 1 for s in found)
    rxs = [re.compile(s.pattern) for s in found]
    text = config(12)
    hidden = [ln for ln in text.splitlines() if any(r.search(ln) for r in rxs)]
    assert hidden == ["uptime 4012 seconds", "last-boot: 2026-11-13 10:12:00"]


def test_a_real_change_is_not_suggested_as_noise(repo: Repo) -> None:
    fill(repo, 10)
    repo.commit("fw1", config(10, "uplink-core"), "real change", T0 + timedelta(days=10))
    found = suggest_ignores(repo, "fw1", "indent")
    assert all("description" not in s.pattern for s in found)


def test_not_enough_history_gives_nothing(repo: Repo) -> None:
    fill(repo, 3)
    assert suggest_ignores(repo, "fw1", "indent") == []
    assert suggest_ignores(repo, "nobody", "indent") == []


def test_a_quiet_device_gives_nothing(repo: Repo) -> None:
    for d in range(8):
        repo.commit("fw1", config(0) if d % 2 == 0 else config(0, "x"), f"d{d}", T0 + timedelta(days=d))
    found = suggest_ignores(repo, "fw1", "indent")
    # the description flips back and forth, which is a change, but uptime and last-boot do not appear
    assert all("uptime" not in s.pattern for s in found)


def test_the_cli_prints_a_pasteable_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cfgtrack.toml").write_text('[[device]]\nname = "fw1"\nfetch = "file"\npath = "x"\n')
    repo = Repo(tmp_path / "backups")
    repo.init()
    fill(repo, 8)
    assert main(["noise", "fw1"]) == 0
    out = capsys.readouterr().out
    assert out.count("changed in 100% of backups") == 2 and "ignore = [" in out
    import tomllib

    snippet = "\n".join(line for line in out.splitlines() if not line.startswith("# "))
    assert len(tomllib.loads(snippet)["ignore"]) == 2  # the output is valid TOML
    (tmp_path / "backups" / "devices" / "fw1.cfg").unlink()
    repo2 = Repo(tmp_path / "other")
    repo2.init()
    assert main(["-c", "cfgtrack.toml", "noise", "nobody"]) == 0
    assert "nothing to suggest" in capsys.readouterr().out
