from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cfgtrack.store import GitError, Repo, check_name, check_rev

T1 = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
T2 = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)


def test_first_commit_then_unchanged_then_changed(repo: Repo) -> None:
    c1 = repo.commit("sw1", "a\nb\n", "sw1: initial", T1)
    assert c1
    assert repo.commit("sw1", "a\nb\n", "should not exist", T2) is None
    c2 = repo.commit("sw1", "a\nc\n", "sw1: +1 -1", T2)
    assert c2 and c2 != c1
    assert repo.read("sw1") == "a\nc\n"
    assert repo.read("sw1", "HEAD~1") == "a\nb\n"
    assert repo.read("sw1", c1) == "a\nb\n"
    log = repo.log("sw1")
    assert [c.subject for c in log] == ["sw1: +1 -1", "sw1: initial"]
    assert log[0].when == T2 and log[1].when == T1
    assert len(repo.log("sw1", limit=1)) == 1
    assert [c.subject for c in repo.log("sw1", since=datetime(2026, 11, 2, tzinfo=UTC))] == ["sw1: +1 -1"]


def test_since_still_works_when_the_clock_was_set_back(repo: Repo) -> None:
    repo.commit("sw1", "a\n", "late", T2)
    repo.commit("sw1", "b\n", "early", T1)  # committed afterwards, but dated earlier
    repo.commit("sw1", "c\n", "late again", T2 + (T2 - T1))
    assert {c.subject for c in repo.log("sw1", since=T2)} == {"late", "late again"}


def test_unknown_things_read_as_none(repo: Repo) -> None:
    assert repo.read("nobody") is None
    assert repo.log("nobody") == []
    repo.commit("sw1", "a\n", "x", T1)
    assert repo.read("sw1", "HEAD~1") is None  # before the first commit
    assert repo.read("nobody") is None


def test_one_file_per_device_and_listing(repo: Repo) -> None:
    repo.commit("b", "1\n", "m", T1)
    repo.commit("a", "2\n", "m", T1)
    assert repo.devices() == ["a", "b"]
    assert repo.log("a") and repo.log("b")


def test_content_is_stored_byte_for_byte_with_lf(repo: Repo) -> None:
    repo.commit("sw1", "x\ny\n", "m", T1)
    assert (repo.path / "devices" / "sw1.cfg").read_bytes() == b"x\ny\n"
    assert (
        subprocess.run(["git", "-C", str(repo.path), "status", "--porcelain"], capture_output=True, text=True).stdout
        == ""
    )


@pytest.mark.parametrize("name", ["../x", "a/b", "", "-rf", ".hidden", "a b", "x" * 65, "a..b", "a\n"])
def test_bad_device_names_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match="bad device name"):
        check_name(name)


@pytest.mark.parametrize("rev", ["--output=/tmp/x", "main", "HEAD^", "abc", "HEAD~1;rm", "-p", "HEAD..HEAD~1", "@{1}"])
def test_bad_revisions_are_refused(rev: str, repo: Repo) -> None:
    with pytest.raises(ValueError, match="bad revision"):
        check_rev(rev)
    with pytest.raises(ValueError, match="bad revision"):
        repo.read("sw1", rev)


def test_good_revisions() -> None:
    for rev in ("HEAD", "HEAD~3", "abcd", "0123456789abcdef", "abcd1234~2"):
        assert check_rev(rev) == rev


def test_a_device_name_cannot_escape_the_repository(repo: Repo, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="bad device name"):
        repo.commit("../escape", "x\n", "m", T1)
    assert not (tmp_path / "escape.cfg").exists()


def test_git_failures_are_reported_not_raised_raw(tmp_path: Path) -> None:
    broken = Repo(tmp_path / "nowhere")
    (tmp_path / "nowhere").mkdir()
    with pytest.raises(GitError, match="not a backup repository"):
        broken.commit("sw1", "x\n", "m")
    with pytest.raises(GitError, match="git add failed"):
        subprocess.run(["git", "-C", str(tmp_path / "nowhere"), "init", "-q"], check=True)
        (tmp_path / "nowhere" / ".git" / "index.lock").write_text("")  # a stuck lock makes git add fail
        broken.commit("sw1", "x\n", "m")
    missing = Repo(tmp_path, git="no-such-git-program")
    with pytest.raises(GitError, match="was not found"):
        missing.init()


def test_global_git_configuration_does_not_interfere(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[commit]\n\tgpgsign = true\n[core]\n\thooksPath = /nonexistent\n[user]\n\tname = Someone\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Someone Else")
    assert repo.commit("sw1", "a\n", "m", T1)
    out = subprocess.run(["git", "-C", str(repo.path), "log", "--format=%an"], capture_output=True, text=True).stdout
    assert set(out.split()) == {"cfgtrack"}  # every commit, whatever the global configuration says


def test_size_and_history(repo: Repo) -> None:
    for i in range(5):
        repo.commit("sw1", "\n".join(f"line {j}" for j in range(200 + i)) + "\n", f"m{i}", T1)
    assert repo.size_bytes() > 0
    assert "line 100" in repo.all_text()
