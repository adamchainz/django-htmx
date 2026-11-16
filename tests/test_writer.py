"""What gets edited, and how files are written: the project root, Git, file
modes, and recovering from an interrupted write."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError

from django_compact_migrations import core, worktree, writer
from django_compact_migrations.core import CompactError
from django_compact_migrations.verify import VerificationError
from django_compact_migrations.writer import Journal, Planned, WriteError
from tests.project import ITEM, Project

LEGACY = 'migrations.AddField("item", "legacy", models.IntegerField(default=0)),'
REMOVE = 'migrations.RemoveField("item", "legacy"),'


def dead_field(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_add", LEGACY)
    project.migration("shop", "0003_remove", REMOVE)


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


def committed(project: Project) -> None:
    git(project.root, "init", "-q")
    git(project.root, "add", "-A")
    git(project.root, "commit", "-qm", "init")


# The project root


def test_outside_the_project_not_edited(project, monkeypatch):
    dead_field(project)
    inner = (project.root / "inner").resolve()
    inner.mkdir()
    monkeypatch.setattr(worktree, "project_root", lambda: inner)
    before = project.files()
    out, err = project.command("apply")
    assert out == "No operations to remove.\n"
    assert "Migrations of shop are outside the project" in err
    assert project.files() == before
    # Named as a target, the app is edited all the same.
    out, _ = project.command("apply", "shop")
    assert "Editing migration files of: shop." in out
    assert project.files() != before


def test_virtual_environment_not_edited(project):
    dead_field(project)
    (project.root / project.package / "pyvenv.cfg").write_text("")
    result = project.compact()
    assert result.files == []
    assert "Migrations of shop are outside the project" in result.warnings[0]


def test_read_only_reason(tmp_path):
    root = tmp_path
    assert worktree.read_only_reason(root / "app/migrations/0001.py", root) is None
    for installed in ("site-packages", "dist-packages", "x.dist-info", "x.egg"):
        path = root / "venv" / installed / "app/migrations/0001.py"
        assert worktree.read_only_reason(path, root) == "installed package"
    assert (
        worktree.read_only_reason(tmp_path.parent / "m.py", root)
        == "outside the project"
    )


def test_project_root_without_git(tmp_path, monkeypatch):
    def missing(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, "run", missing)
    assert worktree.project_root() == tmp_path.resolve()
    assert worktree.uncommitted([tmp_path / "m.py"]) is None


def test_project_root_in_git(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q")
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "sub")
    assert worktree.project_root() == tmp_path.resolve()
    assert worktree.uncommitted([]) == []


# Git


def test_uncommitted_files_refused(project):
    dead_field(project)
    committed(project)
    path = project.path("shop", "0002_add")
    path.write_text(path.read_text() + "# mine\n")
    before = project.files()
    with pytest.raises(CommandError, match="aren't committed.*0002_add.py"):
        project.command("apply")
    assert project.files() == before
    out, _ = project.command("apply", "--allow-dirty")
    assert "Removed 2 operations" in out
    assert "# mine" in project.read("shop", "0002_add")


def test_untracked_and_renamed(project):
    dead_field(project)
    committed(project)
    path = project.path("shop", "0003_remove")
    git(project.root, "mv", str(path), str(path.with_name("0003_gone.py")))
    git(project.root, "mv", str(path.with_name("0003_gone.py")), str(path))
    project.migration("shop", "0004_new", 'migrations.RunSQL("SELECT 1"),')
    paths = [project.path("shop", n) for n in ("0002_add", "0003_remove", "0004_new")]
    assert worktree.uncommitted(paths) == [paths[2]]
    git(project.root, "mv", str(paths[1]), str(paths[1].with_name("0003_x.py")))
    renamed = paths[1].with_name("0003_x.py")
    # Both sides of the rename asked about: one "R" entry naming both.
    assert worktree.uncommitted([paths[0], paths[1], renamed]) == [renamed]


def test_git_failing(project, monkeypatch):
    dead_field(project)
    committed(project)
    real = subprocess.run

    def failing_status(args: list[str], **kwargs: Any) -> Any:
        if "status" in args:
            return subprocess.CompletedProcess(args, 1, b"", b"")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", failing_status)
    assert worktree.uncommitted([project.path("shop", "0002_add")]) is None


# Files that can't be written as they are


@pytest.mark.parametrize("problem", ["read-only", "symbolic link", "directory"])
def test_unwritable_refused_before_writing(project, problem):
    dead_field(project)
    path = project.path("shop", "0003_remove")
    if problem == "read-only":
        path.chmod(0o444)
    elif problem == "symbolic link":
        target = project.root / "elsewhere.py"
        target.write_text(path.read_text())
        path.unlink()
        path.symlink_to(target)
    result = project.compact()
    if problem == "directory":
        path.unlink()
        path.mkdir()
        expected = "not a regular file"
    else:
        expected = problem
    with pytest.raises(CompactError, match=f"can't be written.*{expected}"):
        result.check_worktree()
    # Refused before anything is written.
    assert LEGACY in project.read("shop", "0002_add")


def test_check_writable_missing_and_directory(tmp_path, monkeypatch):
    with pytest.raises(WriteError, match="gone.py"):
        writer.check_writable([tmp_path / "gone.py"])
    (tmp_path / "m.py").write_text("")
    real = os.access
    # As root, a read-only directory is still writable: pretend.
    monkeypatch.setattr(
        os, "access", lambda path, mode: Path(path) != tmp_path and real(path, mode)
    )
    with pytest.raises(WriteError, match="directory is read-only"):
        writer.check_writable([tmp_path / "m.py"])


# Modes and restoring


def test_restored_files_keep_their_mode(project, monkeypatch):
    dead_field(project)
    result = project.compact(prune=True)
    deleted = [c.path for c in result.files if c.deleted]
    assert deleted
    for path in deleted:
        path.chmod(0o640)

    def fail(*args: Any) -> None:
        raise VerificationError("boom")

    monkeypatch.setattr(core, "verify_on_disk", fail)
    with pytest.raises(CompactError, match="restored: boom"):
        result.write()
    for path in deleted:
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert not writer.journal_dir().exists()


def test_written_files_keep_their_mode(project):
    dead_field(project)
    path = project.path("shop", "0002_add")
    path.chmod(0o640)
    project.compact().write()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


# Interrupted writes


def interrupted(project: Project, after: int) -> list[Planned]:
    """What a write killed after ``after`` files leaves behind."""
    result = project.compact(prune=True)
    planned = [
        Planned(c.path, None if c.deleted else c.new.encode()) for c in result.files
    ]
    Journal.start(planned)
    for p in planned[:after]:
        if p.new is None:
            p.path.unlink()
        else:
            p.path.write_bytes(p.new)
    return planned


def test_interrupted_write_recovered(project):
    dead_field(project)
    before = project.files()
    interrupted(project, after=2)
    assert project.files() != before
    out, err = project.command("plan")
    assert "Restored" in err
    assert project.files() == before
    assert "Removed" not in out and "Would remove" in out
    assert not writer.journal_dir().exists()


def test_interrupted_write_changed_since(project):
    dead_field(project)
    planned = interrupted(project, after=1)
    planned[0].path.write_text("# edited by hand\n")
    messages = writer.recover()
    assert "changed since the interrupted apply" in messages[0]
    assert "Remove" in messages[-1]
    assert writer.journal_dir().exists()
    with pytest.raises(WriteError, match="an earlier apply was interrupted"):
        Journal.start(planned)


def test_unreadable_journal(project):
    dead_field(project)
    directory = writer.journal_dir()
    directory.mkdir()
    (directory / "journal.json").write_text("{not json")
    with pytest.raises(CommandError, match="can't be read"):
        project.command("plan")


def test_restore_failure_keeps_journal(project, monkeypatch):
    dead_field(project)
    result = project.compact()

    def fail(path: Path, data: bytes, mode: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(writer, "_replace", fail)
    with pytest.raises(CompactError, match="originals are in"):
        result.write()
    assert writer.journal_dir().exists()
