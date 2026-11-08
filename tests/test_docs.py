"""The numbers quoted in the docs must be the ones in docs/report.json, and the examples must work."""

from __future__ import annotations

import json
import re
from pathlib import Path

from cfgtrack.cli import _parser
from cfgtrack.config import DEFAULT_TOML, load

ROOT = Path(__file__).resolve().parent.parent
REPORT = json.loads((ROOT / "docs" / "report.json").read_text(encoding="utf-8"))
README = (ROOT / "README.md").read_text(encoding="utf-8")
EVAL = (ROOT / "docs" / "evaluation.md").read_text(encoding="utf-8")
P = REPORT["pipeline"]


def test_report_is_a_full_run() -> None:
    assert REPORT["environment"]["quick"] is False
    assert (P["devices"], P["days"]) == (40, 60)


def test_the_claims_are_true_in_the_report() -> None:
    assert P["detected"] == P["change_events"] and P["missed"] == 0
    assert P["false_commits"] == 0
    assert P["exact_reports"] == P["exact_reports_possible"]
    assert P["reorders_reported"] == P["reorders"]
    assert P["faults_blocked"] == P["faults"]
    assert P["secrets"]["leaked_cfgtrack"] == 0 and P["secrets"]["leaked_naive"] == P["secrets"]["distinct"]
    assert P["problems"] == []
    assert P["stored_files_that_would_be_changed_by_the_rules"] == 0
    for kind in REPORT["fetch"]["kinds"].values():
        assert kind["identical"] == kind["fetches"]
    n = REPORT["noise"]
    assert n["commits_with_the_rule"] == 0 and n["real_change_still_found"] and not n["suggestion_hides_a_real_line"]
    assert n["patterns_suggested"] == 2 and n["commits_without_the_rule"] == n["days_without_the_rule"]


def test_numbers_in_the_readme() -> None:
    assert f"**{P['detected']} of {P['change_events']}**" in README
    assert f"{P['quiet_rounds']} days where only the noise changed" in README
    assert (
        f"{P['exact_reports']} of {P['exact_reports_possible']}" in README
        and f"all {P['reorders']} reorderings" in README
    )
    assert f"**{P['faults_blocked']} of {P['faults']}**" in README
    assert (
        f"**0 of {P['secrets']['distinct']}**" in README
        and f"{P['secrets']['leaked_naive']} of {P['secrets']['distinct']}" in README
    )
    ratio = P["repo_bytes"]["naive"] / P["repo_bytes"]["cfgtrack"]
    assert f"{ratio:.1f} times smaller" in README
    assert f"{P['repo_bytes']['cfgtrack'] / 1024:.0f} KiB against {P['repo_bytes']['naive'] / 1024:.0f} KiB" in README
    bp = REPORT["cost"]["backup_pass"]
    assert f"{bp['devices']} devices" in README and f"{bp['quiet_pass_s']:g} s" in README
    n = REPORT["noise"]
    assert f"{n['commits_without_the_rule']} commits in {n['days_without_the_rule']} days" in README
    assert f"{n['quiet_days_with_the_rule']} quiet days" in README


def test_numbers_in_the_evaluation() -> None:
    for name, k in REPORT["fetch"]["kinds"].items():
        assert f"| {name} | {k['identical']} of {k['fetches']} | {k['median_ms']:g} ms | {k['p95_ms']:g} ms |" in EVAL
    assert f"| real changes found | {P['detected']} of {P['change_events']} |" in EVAL
    assert f"| {P['quiet_rounds']} |" in EVAL or f"{P['quiet_rounds']} days with no change" in EVAL
    for kind, v in P["by_kind"].items():
        assert f"| {kind} | {v['events']} | {v['detected']} | {v['exact']} |" in EVAL
    for kind, v in P["faults_by_kind"].items():
        assert f"| {kind} | {v['injected']} | {v['blocked']} |" in EVAL
    for k, v in REPORT["cost"].items():
        if k != "backup_pass":
            assert f"| {v['lines']:,} | {v['normalize_ms']:g} ms | {v['diff_20_changes_ms']:g} ms |" in EVAL


def test_every_command_in_the_readme_exists_and_the_other_way_round() -> None:
    commands = set(_parser()._subparsers._group_actions[0].choices)  # type: ignore[union-attr,attr-defined]
    documented = set(re.findall(r"^\| `([a-z-]+)[ `]", README, re.MULTILINE))
    assert documented == commands - {"init-config"}


def test_the_example_configurations_load(tmp_path: Path) -> None:
    c = load(ROOT / "examples" / "cfgtrack.toml")
    assert {d.fetch for d in c.devices} == {"ssh", "command", "http", "file"}
    assert any(d.mode == "shell" for d in c.devices)
    (tmp_path / "c.toml").write_text(DEFAULT_TOML)
    assert len(load(tmp_path / "c.toml").devices) == 3


def test_the_readme_snippet_is_a_valid_configuration(tmp_path: Path) -> None:
    block = re.search(r"```toml\n(.*?)```", README, re.DOTALL)
    assert block
    text = block.group(1).replace("SHA256:...", "SHA256:" + "A" * 43)
    (tmp_path / "c.toml").write_text(text)
    c = load(tmp_path / "c.toml")
    assert c.devices[0].profile.name == "cisco_ios" and len(c.devices[0].profile.redact) > 13
