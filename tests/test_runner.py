from __future__ import annotations

import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cfgtrack.config import Config, Device
from cfgtrack.fetchers import FetchError
from cfgtrack.profiles import IOS
from cfgtrack.runner import State, load_key, run_backup
from cfgtrack.store import Repo

T0 = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)


class Net:
    """A fake network: the text each device would print right now. Used in place of the real fetchers."""

    def __init__(self, **texts: str | Exception) -> None:
        self.texts = texts
        self.calls: list[str] = []

    def __call__(self, dev: Device, env: object) -> str:
        self.calls.append(dev.name)
        t = self.texts[dev.name]
        if isinstance(t, Exception):
            raise t
        return t


def make_cfg(tmp_path: Path, *names: str, guard: float = 0.5) -> Config:
    devs = tuple(Device(name=n, profile=IOS, fetch="file", path="unused") for n in names)
    return Config(store=tmp_path / "store", key_file=tmp_path / "k.key", workers=4, shrink_guard=guard, devices=devs)


@pytest.fixture
def world(tmp_path: Path, key: bytes) -> tuple[Config, Repo, State]:
    cfg = make_cfg(tmp_path, "sw1", "sw2")
    return cfg, Repo(cfg.store), State(cfg.store / "state.json")


def test_first_backup_then_unchanged_then_changed(world: tuple[Config, Repo, State], ios: str, key: bytes) -> None:
    cfg, repo, state = world
    net = Net(sw1=ios, sw2=ios.replace("sw1", "sw2"))
    r = run_backup(cfg, repo, state, key, now=T0, fetcher=net)
    assert [(x.name, x.status) for x in r] == [("sw1", "new"), ("sw2", "new")]
    r = run_backup(cfg, repo, state, key, now=T0 + timedelta(days=1), fetcher=net)
    assert {x.status for x in r} == {"unchanged"}
    changed = ios.replace("description uplink", "description uplink-core")
    net.texts["sw1"] = changed
    r = run_backup(cfg, repo, state, key, now=T0 + timedelta(days=2), fetcher=net)
    by = {x.name: x for x in r}
    assert by["sw1"].status == "changed" and (by["sw1"].added, by["sw1"].removed) == (1, 1)
    assert by["sw2"].status == "unchanged"
    assert "description uplink-core" in repo.read("sw1") and len(repo.log("sw1")) == 2 and len(repo.log("sw2")) == 1


def test_noise_alone_never_makes_a_commit(world: tuple[Config, Repo, State], ios: str, key: bytes) -> None:
    cfg, repo, state = world
    run_backup(cfg, repo, state, key, names=["sw1"], now=T0, fetcher=Net(sw1=ios))
    noisy = ios.replace("10:11:12", "23:01:59").replace("1432", "1500").replace("17179869", "17180000")
    r = run_backup(cfg, repo, state, key, names=["sw1"], now=T0 + timedelta(days=1), fetcher=Net(sw1=noisy))
    assert r[0].status == "unchanged" and len(repo.log("sw1")) == 1


def test_secrets_never_reach_the_repository(world: tuple[Config, Repo, State], ios: str, key: bytes) -> None:
    cfg, repo, state = world
    run_backup(cfg, repo, state, key, names=["sw1"], now=T0, fetcher=Net(sw1=ios))
    rotated = ios.replace("public123", "brandnewcommunity")
    r = run_backup(cfg, repo, state, key, names=["sw1"], now=T0 + timedelta(days=1), fetcher=Net(sw1=rotated))
    assert r[0].status == "changed"  # a rotation is a change worth knowing about...
    history = repo.all_text()
    for s in ("public123", "brandnewcommunity", "$9$abcdefghijklmn", "$9$zzzzzzzzzz", "0822455D0A16"):
        assert s not in history  # ...but the secret itself is stored nowhere, not even in an old commit
    assert "<secret:" in history


def test_failures_are_recorded_and_do_not_touch_the_repository(
    world: tuple[Config, Repo, State], ios: str, key: bytes
) -> None:
    cfg, repo, state = world
    run_backup(cfg, repo, state, key, now=T0, fetcher=Net(sw1=ios, sw2=ios))
    cut = "\n".join(ios.splitlines()[:12]) + "\n"
    net = Net(sw1=FetchError("timeout after 30 s"), sw2=cut)
    r = run_backup(cfg, repo, state, key, now=T0 + timedelta(days=1), fetcher=net)
    assert [(x.status, x.failed) for x in r] == [("error", True), ("error", True)]
    assert "timeout" in r[0].message and "complete configuration" in r[1].message
    assert len(repo.log("sw1")) == 1 and len(repo.log("sw2")) == 1
    st = state.get("sw1")
    assert st["failures"] == 1 and st["last_ok"] == T0.isoformat()
    run_backup(cfg, repo, state, key, now=T0 + timedelta(days=2), fetcher=net)
    assert state.get("sw1")["failures"] == 2
    run_backup(cfg, repo, state, key, now=T0 + timedelta(days=3), fetcher=Net(sw1=ios, sw2=ios))
    assert state.get("sw1")["failures"] == 0 and state.get("sw1")["last_error"] == ""


def test_a_configuration_that_shrinks_a_lot_is_held_back(
    world: tuple[Config, Repo, State], ios: str, key: bytes
) -> None:
    cfg, repo, state = world
    routes = "".join(f"ip route 10.{i}.0.0 255.255.0.0 10.0.0.1\n" for i in range(60))
    big = ios.removesuffix("end\n") + routes + "end\n"
    run_backup(cfg, repo, state, key, names=["sw1"], now=T0, fetcher=Net(sw1=big))
    r = run_backup(
        cfg, repo, state, key, names=["sw1"], now=T0 + timedelta(days=1), fetcher=Net(sw1="hostname sw1\nend\n")
    )
    assert r[0].status == "suspect" and "--accept-shrink sw1" in r[0].message
    assert len(repo.log("sw1")) == 1
    r = run_backup(
        cfg,
        repo,
        state,
        key,
        names=["sw1"],
        accept_shrink=["sw1"],
        now=T0 + timedelta(days=2),
        fetcher=Net(sw1="hostname sw1\nend\n"),
    )
    assert r[0].status == "changed" and len(repo.log("sw1")) == 2


def test_a_small_configuration_may_shrink(world: tuple[Config, Repo, State], key: bytes) -> None:
    cfg, repo, state = world
    run_backup(cfg, repo, state, key, names=["sw1"], now=T0, fetcher=Net(sw1="a\nb\nc\nd\nend\n"))
    r = run_backup(cfg, repo, state, key, names=["sw1"], now=T0 + timedelta(days=1), fetcher=Net(sw1="a\nend\n"))
    assert r[0].status == "changed"


def test_only_the_named_devices_are_fetched(world: tuple[Config, Repo, State], ios: str, key: bytes) -> None:
    cfg, repo, state = world
    net = Net(sw1=ios, sw2=ios)
    run_backup(cfg, repo, state, key, names=["sw2"], now=T0, fetcher=net)
    assert net.calls == ["sw2"] and repo.devices() == ["sw2"]


def test_state_is_saved_and_survives_damage(world: tuple[Config, Repo, State], ios: str, key: bytes) -> None:
    cfg, repo, state = world
    run_backup(cfg, repo, state, key, now=T0, fetcher=Net(sw1=ios, sw2=ios))
    data = json.loads((cfg.store / "state.json").read_text())
    assert data["devices"]["sw1"]["last_change"] == T0.isoformat()
    assert State(cfg.store / "state.json").get("sw1")["last_ok"] == T0.isoformat()
    (cfg.store / "state.json").write_text("{not json")
    assert State(cfg.store / "state.json").get("sw1")["failures"] == 0


def test_state_and_key_are_not_committed(world: tuple[Config, Repo, State], ios: str, key: bytes) -> None:
    cfg, repo, state = world
    run_backup(cfg, repo, state, key, now=T0, fetcher=Net(sw1=ios, sw2=ios))
    import subprocess

    tracked = subprocess.run(["git", "-C", str(repo.path), "ls-files"], capture_output=True, text=True).stdout.split()
    assert "state.json" not in tracked and not any(t.endswith(".key") for t in tracked)


def test_key_file_is_created_private_and_reused(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "k.key"
    k1 = load_key(p)
    assert len(k1) == 32 and load_key(p) == k1
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    p.write_text("zz")
    with pytest.raises(ValueError, match="not a cfgtrack key file"):
        load_key(p)
    p.write_text("ab" * 4)
    with pytest.raises(ValueError, match="too short"):
        load_key(p)
