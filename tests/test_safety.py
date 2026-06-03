import subprocess

import pytest

from librarian import safety


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def git_vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    _git(v, "init")
    _git(v, "config", "user.email", "t@t.com")
    _git(v, "config", "user.name", "t")
    (v / "seed.md").write_text("seed")
    _git(v, "add", "-A")
    _git(v, "commit", "-m", "init")
    return v


def test_is_git_repo(tmp_path, git_vault):
    assert safety.is_git_repo(git_vault) is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert safety.is_git_repo(plain) is False


def test_snapshot_returns_none_for_non_git(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert safety.snapshot(plain, "x") is None


def test_snapshot_commits_pending_changes(git_vault):
    (git_vault / "new.md").write_text("new note")
    assert safety.has_uncommitted_changes(git_vault) is True
    sha = safety.snapshot(git_vault, "librarian: pre-run snapshot")
    assert sha and len(sha) == 40
    assert safety.has_uncommitted_changes(git_vault) is False  # all committed


def test_snapshot_noop_when_clean_returns_head(git_vault):
    sha = safety.snapshot(git_vault, "noop")
    head = subprocess.run(
        ["git", "-C", str(git_vault), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert sha == head
