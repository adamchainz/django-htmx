from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from django_compact_migrations import worktree
from tests.project import ITEM, Base, Project, ProjectTestCase

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


class WorktreeTests(ProjectTestCase):
    def test_untracked_and_renamed(self):
        dead_field(self.project)
        committed(self.project)
        path = self.project.path("shop", "0003_remove")
        git(self.project.root, "mv", str(path), str(path.with_name("0003_gone.py")))
        git(self.project.root, "mv", str(path.with_name("0003_gone.py")), str(path))
        self.project.migration("shop", "0004_new", 'migrations.RunSQL("SELECT 1"),')
        paths = [
            self.project.path("shop", n)
            for n in ("0002_add", "0003_remove", "0004_new")
        ]
        assert worktree.uncommitted(paths) == [paths[2]]
        git(
            self.project.root, "mv", str(paths[1]), str(paths[1].with_name("0003_x.py"))
        )
        renamed = paths[1].with_name("0003_x.py")
        # Both sides of the rename asked about: one "R" entry naming both.
        assert worktree.uncommitted([paths[0], paths[1], renamed]) == [renamed]

    def test_git_failing(self):
        dead_field(self.project)
        committed(self.project)
        real = subprocess.run

        def failing_status(args: list[str], **kwargs: Any) -> Any:
            if "status" in args:
                return subprocess.CompletedProcess(args, 1, b"", b"")
            return real(args, **kwargs)

        self.monkeypatch.setattr(subprocess, "run", failing_status)
        assert worktree.uncommitted([self.project.path("shop", "0002_add")]) is None


class ProjectRootTests(Base):
    def test_read_only_reason(self):
        root = self.tmp_path
        assert worktree.read_only_reason(root / "app/migrations/0001.py", root) is None
        for installed in ("site-packages", "dist-packages", "x.dist-info", "x.egg"):
            path = root / "venv" / installed / "app/migrations/0001.py"
            assert worktree.read_only_reason(path, root) == "installed package"
        assert (
            worktree.read_only_reason(self.tmp_path.parent / "m.py", root)
            == "outside the project"
        )

    def test_project_root_without_git(self):
        def missing(*args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError("git")

        self.monkeypatch.chdir(self.tmp_path)
        self.monkeypatch.setattr(subprocess, "run", missing)
        assert worktree.project_root() == self.tmp_path.resolve()
        assert worktree.uncommitted([self.tmp_path / "m.py"]) is None

    def test_project_root_in_git(self):
        git(self.tmp_path, "init", "-q")
        (self.tmp_path / "sub").mkdir()
        self.monkeypatch.chdir(self.tmp_path / "sub")
        assert worktree.project_root() == self.tmp_path.resolve()
        assert worktree.uncommitted([]) == []
