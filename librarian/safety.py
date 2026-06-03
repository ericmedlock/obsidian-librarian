"""
Pre-mutation safety: snapshot the vault with git before the librarian changes
anything, so every autonomous run is recoverable with `git revert`/`reset`.

Policy (enforced by the CLI): an autonomous run refuses to proceed on a vault
that isn't under git, unless the user passes --force.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


def breaking_inbound_links(all_links, vault: Path, src: Path) -> list[tuple[str, str]]:
    """
    Return inbound wikilinks that a move of `src` would break.

    Obsidian resolves `[[Note]]` by basename regardless of folder, so name-based
    links survive a move. Only PATH-based links (`[[folder/Note]]`) that point at
    the note's current location break when it moves. `all_links` is an iterable
    of (source_path, target_name) edges (e.g. VaultGraph.all_links()).
    """
    vault = Path(vault)
    try:
        rel_no_ext = str(Path(src).resolve().relative_to(vault.resolve()).with_suffix(""))
    except ValueError:
        return []
    breakers = []
    for source_path, target in all_links:
        if "/" not in target:
            continue  # name-based link — survives the move
        target_no_ext = target[:-3] if target.endswith(".md") else target
        if target_no_ext == rel_no_ext:
            breakers.append((source_path, target))
    return breakers


def _git(vault: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        capture_output=True,
        text=True,
    )


def is_git_repo(vault: Path) -> bool:
    """True if `vault` is inside a git working tree."""
    r = _git(vault, "rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == "true"


def has_uncommitted_changes(vault: Path) -> bool:
    r = _git(vault, "status", "--porcelain")
    return bool(r.stdout.strip())


def snapshot(vault: Path, message: str) -> Optional[str]:
    """
    Commit the current vault state and return the resulting commit SHA.

    Returns None if `vault` isn't a git repo. If there's nothing to commit,
    returns the current HEAD SHA (a no-op snapshot is still a valid restore
    point). Raises RuntimeError if a git command fails unexpectedly.
    """
    vault = Path(vault)
    if not is_git_repo(vault):
        return None

    if has_uncommitted_changes(vault):
        add = _git(vault, "add", "-A")
        if add.returncode != 0:
            raise RuntimeError(f"git add failed: {add.stderr.strip()}")
        commit = _git(vault, "commit", "-m", message)
        if commit.returncode != 0:
            raise RuntimeError(f"git commit failed: {commit.stderr.strip()}")

    head = _git(vault, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise RuntimeError(f"git rev-parse failed: {head.stderr.strip()}")
    return head.stdout.strip()
