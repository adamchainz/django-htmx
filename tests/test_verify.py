from __future__ import annotations

import dataclasses

import pytest

from django_clean_migrations import core
from django_clean_migrations.core import CleanError
from django_clean_migrations.rewrite import OpEdit
from django_clean_migrations.solver import Solver
from tests.project import Project
from tests.test_clean import ITEM


def dead_field(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        """
        migrations.AddField("item", "legacy", models.IntegerField(default=0)),
        migrations.AddField("item", "other", models.IntegerField(default=0)),
        """,
    )
    project.migration(
        "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
    )


def test_on_disk_rollback(project: Project) -> None:
    dead_field(project)
    before = project.files()
    result = project.clean()
    # Simulate a rewriting bug: the written file also loses another operation.
    change = result.files[0]
    assert change.migration == ("shop", "0002_add")
    bad = change.new.replace(
        'migrations.AddField("item", "other", models.IntegerField(default=0)),', ""
    )
    result.files[0] = dataclasses.replace(change, new=bad)
    with pytest.raises(CleanError, match="were restored.*has operations"):
        result.write()
    assert project.files() == before
    # The restored files load again.
    assert len(project.clean().removals) == 1


def test_on_disk_state_mismatch(project: Project) -> None:
    dead_field(project)
    before = project.files()
    result = project.clean()
    change = result.files[0]
    bad = change.new.replace("default=0", "default=1")
    result.files[0] = dataclasses.replace(change, new=bad)
    with pytest.raises(
        CleanError, match="produce a different state: model shop.item differs"
    ):
        result.write()
    assert project.files() == before


def test_on_disk_import_error(project: Project) -> None:
    dead_field(project)
    before = project.files()
    result = project.clean()
    result.files[-1] = dataclasses.replace(
        result.files[-1], new="raise ValueError('x')\n"
    )
    with pytest.raises(CleanError, match="restored: x"):
        result.write()
    assert project.files() == before


def test_in_memory_failure(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    project.migration(
        "shop",
        "0001_initial",
        """
        migrations.CreateModel(
            name="Author",
            fields=[("id", models.AutoField(primary_key=True))],
        ),
        migrations.CreateModel(
            name="Book",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("author", models.ForeignKey("shop.author", models.CASCADE)),
            ],
        ),
        """,
    )
    project.migration("shop", "0002_drop", 'migrations.DeleteModel("Author"),')
    # Pretend nothing blocks: removing Author would leave Book's FK dangling
    # from the start.
    monkeypatch.setattr(Solver, "blocker", lambda self, life: None)
    with pytest.raises(
        CleanError, match="Internal error: .*missing model.*shop.author"
    ):
        project.clean()


def test_in_memory_state_difference(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead_field(project)
    original = core._collect_edits

    def collect(solver: Solver) -> dict[int, dict[int, OpEdit]]:
        edits = original(solver)
        for index, op_edits in edits.items():
            for rec in solver.history.ops:
                if rec.migration.index == index:
                    op_edits.setdefault(rec.index, OpEdit(rec)).remove = True
        return edits

    monkeypatch.setattr(core, "_collect_edits", collect)
    with pytest.raises(
        CleanError, match="ends in a different state: model shop.item differs"
    ):
        project.clean()
