from __future__ import annotations

import json
from pathlib import Path

import pytest

from cfgtrack.cli import main, parse_since
from conftest import IOS


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A configuration with two devices that are plain files, so that the whole flow runs without a network."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sw1.txt").write_text(IOS)
    (tmp_path / "sw2.txt").write_text(IOS.replace("sw1", "sw2"))
    (tmp_path / "cfgtrack.toml").write_text(
        """\
[store]
path = "backups"
stale_after = "2d"

[[device]]
name = "sw1"
profile = "cisco_ios"
fetch = "file"
path = "sw1.txt"

[[device]]
name = "sw2"
profile = "cisco_ios"
fetch = "file"
path = "sw2.txt"
"""
    )
    return tmp_path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_init_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    assert run(capsys, "init-config")[0] == 0
    assert (tmp_path / "cfgtrack.toml").exists()
    code, _, err = run(capsys, "init-config")
    assert code == 2 and "exists" in err
    assert run(capsys, "init-config", "--force")[0] == 0


def test_the_whole_flow(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "backup")
    assert code == 0 and "sw1" in out and "new" in out and "0 failed" in out
    assert (project / "cfgtrack.key").exists()
    code, out, _ = run(capsys, "backup")
    assert code == 0 and "2 device(s): 0 changed" in out

    (project / "sw1.txt").write_text(IOS.replace("description uplink", "description uplink-core"))
    code, out, _ = run(capsys, "backup", "--diff", "--fail-on-change")
    assert code == 1 and "changed" in out and "+   description uplink-core" in out
    code, out, _ = run(capsys, "backup", "--fail-on-change")
    assert code == 0  # nothing new this time

    code, out, _ = run(capsys, "log", "sw1")
    assert code == 0 and "sw1: +1 -1" in out and "initial backup" in out
    code, out, _ = run(capsys, "diff", "sw1")
    assert "-   description uplink" in out and "+   description uplink-core" in out
    code, out, _ = run(capsys, "diff", "sw1", "--format", "json")
    assert {c["kind"] for c in json.loads(out)} == {"added", "removed"}
    code, out, _ = run(capsys, "show", "sw1")
    assert "description uplink-core" in out and "public123" not in out and "<secret:" in out
    code, out, _ = run(capsys, "show", "sw1", "--rev", "HEAD~1")
    assert "description uplink\n" in out

    code, out, _ = run(capsys, "report", "--since", "1d")
    assert code == 0 and "sw1" in out and "```diff" in out
    code, out, _ = run(capsys, "report", "--since", "1d", "--format", "json")
    assert any(e["device"] == "sw1" and e["added"] == 1 for e in json.loads(out))
    html = project / "r.html"
    code, out, _ = run(capsys, "report", "--since", "1d", "--format", "html", "--output", str(html))
    assert code == 0 and "<script" not in html.read_text() and "uplink-core" in html.read_text()

    code, out, _ = run(capsys, "status")
    assert code == 0 and "STALE" not in out
    code, out, _ = run(capsys, "verify")
    assert code == 0 and "0 problem(s)" in out


def test_failures_give_exit_code_1_and_status_flags_them(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "sw2.txt").unlink()
    code, out, _ = run(capsys, "backup")
    assert code == 1 and "error" in out and "1 failed" in out
    code, out, _ = run(capsys, "status")
    assert code == 1 and "STALE" in out and "cannot read file" in out


def test_status_before_any_backup_is_stale(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "status")
    assert code == 1 and out.count("STALE") == 2 and "never" in out


def test_verify_finds_a_secret_the_rules_miss(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "backup")
    from cfgtrack.store import Repo

    repo = Repo(project / "backups")
    repo.commit("sw1", (repo.read("sw1") or "") + "snmp-server community leaked-one RO\n", "manual edit")
    code, out, _ = run(capsys, "verify")
    assert code == 1 and "sw1:" in out and "clear text" in out


def test_usage_errors_give_exit_code_2(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (
        ["backup", "nobody"],
        ["backup", "--accept-shrink", "nobody"],
        ["show", "nobody"],
        ["diff", "nobody"],
        ["show", "sw1", "--rev=--output=x"],
        ["diff", "sw1", "--from", "main"],
        ["report", "--since", "yesterday-ish"],
        ["log", "../x"],
    ):
        run(capsys, "backup")
        code, _, err = run(capsys, *argv)
        assert code == 2 and err.startswith("error:"), argv
    code, _, err = run(capsys, "-c", "missing.toml", "status")
    assert code == 2 and "not found" in err


def test_reading_before_any_backup_and_an_unknown_device(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "log", "sw1")
    assert code == 2 and "not a backup repository" in err
    run(capsys, "backup")
    code, out, _ = run(capsys, "log", "nobody")
    assert code == 0 and "no history" in out


def test_since_parsing() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    assert parse_since("1d", now) == datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    assert parse_since("2026-11-01", now) == datetime(2026, 11, 1, tzinfo=UTC)
    assert parse_since("2026-11-01 08:30", now) == datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
    with pytest.raises(ValueError, match="bad time"):
        parse_since("whenever", now)


def test_hostkey_command(capsys: pytest.CaptureFixture[str]) -> None:
    from fakedevice import FakeSshDevice

    with FakeSshDevice("x\n") as fake:
        code, out, _ = run(capsys, "hostkey", "127.0.0.1", "--port", str(fake.port))
        assert code == 0 and f'host_key = "{fake.fingerprint}"' in out
    code, _, err = run(capsys, "hostkey", "127.0.0.1", "--port", "1")
    assert code == 2 and "cannot connect" in err
