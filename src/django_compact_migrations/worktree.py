"""
Where the project is, and what Git knows about the files to be written.

The project root is the top of the Git work tree holding the current
directory, or the current directory outside Git. Only migrations under it
are edited unless their app is named as a target.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, check=False, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None


def git_toplevel(path: Path) -> Path | None:
    """The top of the Git work tree holding ``path``, if any."""
    done = _git(path, "rev-parse", "--show-toplevel")
    if done is None or done.returncode != 0:
        return None
    return Path(os.fsdecode(done.stdout.strip()))


def project_root() -> Path:
    cwd = Path.cwd().resolve()
    top = git_toplevel(cwd)
    return top.resolve() if top is not None else cwd


INSTALLED = ("site-packages", "dist-packages")


def read_only_reason(path: Path, root: Path) -> str | None:
    """Why the migration file at ``path`` isn't part of the project, if so."""
    path = path.resolve()
    if any(
        part in INSTALLED or part.endswith((".dist-info", ".egg"))
        for part in path.parts
    ):
        return "installed package"
    if not path.is_relative_to(root):
        return "outside the project"
    parts = path.relative_to(root).parts
    for depth in range(1, len(parts)):
        if root.joinpath(*parts[:depth], "pyvenv.cfg").exists():
            return "in a virtual environment"
    return None


def uncommitted(paths: Iterable[Path]) -> list[Path] | None:
    """
    Which of ``paths`` have changes Git hasn't committed (or aren't tracked),
    or None when they aren't in a Git work tree.
    """
    paths = [p.resolve() for p in paths]
    if not paths:
        return []
    top = git_toplevel(paths[0].parent)
    if top is None:
        return None
    done = _git(
        top,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
        "--",
        *(os.fspath(p) for p in paths),
    )
    if done is None or done.returncode != 0:
        return None
    changed: set[Path] = set()
    # NUL-terminated "XY path" entries; a rename or copy adds its source.
    entries = done.stdout.split(b"\0")[:-1]
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        status = entry[:2]
        changed.add((top / os.fsdecode(entry[3:])).resolve())
        if b"R" in status or b"C" in status:
            index += 1  # the source path of a rename or copy
    return [p for p in paths if p in changed]
