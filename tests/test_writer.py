"""What gets edited, and how files are written: the project root, Git, file
modes, and restoring files when the result doesn't verify."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError
from unittest_parametrize import parametrize

from django_compact_migrations import core, worktree, writer
from django_compact_migrations.core import CompactError
from django_compact_migrations.verify import VerificationError
from django_compact_migrations.writer import WriteError
from tests.project import ITEM, Project, ProjectTestCase

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


# Git


# Files that can't be written as they are


# Modes and restoring


# Interrupted writes


class WriterTests(ProjectTestCase):
    def test_outside_the_project_not_edited(self):
        dead_field(self.project)
        inner = (self.project.root / "inner").resolve()
        inner.mkdir()
        self.monkeypatch.setattr(worktree, "project_root", lambda: inner)
        before = self.project.files()
        out, err = self.project.command("apply", "--static")
        assert out == "No operations to remove.\n"
        assert "Migrations of shop are outside the project" in err
        assert self.project.files() == before
        # Named as a target, the app is edited all the same.
        out, _ = self.project.command("apply", "--static", "shop")
        assert "Editing migration files of: shop." in out
        assert self.project.files() != before

    def test_virtual_environment_not_edited(self):
        dead_field(self.project)
        (self.project.root / self.project.package / "pyvenv.cfg").write_text("")
        result = self.project.compact()
        assert result.files == []
        assert "Migrations of shop are outside the project" in result.warnings[0]

    def test_uncommitted_files_refused(self):
        dead_field(self.project)
        committed(self.project)
        path = self.project.path("shop", "0002_add")
        path.write_text(path.read_text() + "# mine\n")
        before = self.project.files()
        with pytest.raises(CommandError, match="aren't committed.*0002_add.py"):
            self.project.command("apply", "--static")
        assert self.project.files() == before
        out, _ = self.project.command("apply", "--static", "--allow-dirty")
        assert "Removed 2 operations" in out
        assert "# mine" in self.project.read("shop", "0002_add")

    @parametrize("problem", ["read-only", "symbolic link", "directory"])
    def test_unwritable_refused_before_writing(self, problem):
        dead_field(self.project)
        path = self.project.path("shop", "0003_remove")
        if problem == "read-only":
            path.chmod(0o444)
        elif problem == "symbolic link":
            target = self.project.root / "elsewhere.py"
            target.write_text(path.read_text())
            path.unlink()
            path.symlink_to(target)
        result = self.project.compact()
        if problem == "directory":
            path.unlink()
            path.mkdir()
            expected = "not a regular file"
        else:
            expected = problem
        with pytest.raises(CompactError, match=f"can't be written.*{expected}"):
            result.check_worktree()
        # Refused before anything is written.
        assert LEGACY in self.project.read("shop", "0002_add")

    def test_check_writable_missing_and_directory(self):
        with pytest.raises(WriteError, match="gone.py"):
            writer.check_writable([self.tmp_path / "gone.py"])
        (self.tmp_path / "m.py").write_text("")
        real = os.access
        # As root, a read-only directory is still writable: pretend.
        self.monkeypatch.setattr(
            os,
            "access",
            lambda path, mode: Path(path) != self.tmp_path and real(path, mode),
        )
        with pytest.raises(WriteError, match="directory is read-only"):
            writer.check_writable([self.tmp_path / "m.py"])

    def test_restored_files_keep_their_mode(self):
        dead_field(self.project)
        result = self.project.compact(prune=True)
        deleted = [c.path for c in result.files if c.deleted]
        assert deleted
        for path in deleted:
            path.chmod(0o640)

        def fail(*args: Any) -> None:
            raise VerificationError("boom")

        self.monkeypatch.setattr(core, "verify_on_disk", fail)
        with pytest.raises(CompactError, match="restored: boom"):
            result.write()
        for path in deleted:
            assert stat.S_IMODE(path.stat().st_mode) == 0o640

    def test_written_files_keep_their_mode(self):
        dead_field(self.project)
        path = self.project.path("shop", "0002_add")
        path.chmod(0o640)
        self.project.compact().write()
        assert stat.S_IMODE(path.stat().st_mode) == 0o640

    def test_restore_failure_reported(self):
        dead_field(self.project)
        result = self.project.compact()

        def fail(path: Path, data: bytes, mode: int) -> None:
            raise OSError("disk full")

        self.monkeypatch.setattr(writer, "_replace", fail)
        with pytest.raises(CompactError, match="Restore them with Git"):
            result.write()
