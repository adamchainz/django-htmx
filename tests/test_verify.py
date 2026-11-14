from __future__ import annotations

import dataclasses
from typing import Any

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


# Direct checks


def analysed(project: Project) -> Any:
    project.activate()
    with project.db_blocker.unblock():
        return core.analyse()


def test_in_memory_errors(project: Project) -> None:
    from django.db.migrations import operations as m
    from django.db.models import IntegerField

    from django_clean_migrations.verify import VerificationError, verify_in_memory

    dead_field(project)
    analysis = analysed(project)
    history, timeline = analysis.history, analysis.timeline
    first = history.migrations[0]
    second = history.migrations[1]
    create = first.migration.operations[0]

    def check(edited: dict[int, list[Any]], message: str) -> None:
        with pytest.raises(VerificationError, match=message):
            verify_in_memory(history, edited, timeline.dangling, timeline.state)

    check({second.index: [m.RemoveField("item", "nope")]}, "fails in shop.0002_add")
    # The same fields, created in another order.
    fields = list(create.fields)
    swapped = m.CreateModel("Item", [fields[1], fields[0]])
    check({first.index: [swapped]}, "fields of shop.item in another order")
    # The same models, created in another order.
    other = m.CreateModel("Other", [("id", IntegerField(primary_key=True))])
    late = list(second.migration.operations) + [other]
    early = [m.CreateModel("Other", [("id", IntegerField(primary_key=True))]), create]
    edited = {first.index: early, second.index: list(second.migration.operations)}
    original = {first.index: [create], second.index: late}
    verify_in_memory(
        history,
        original,
        timeline.dangling,
        _replayed(history, original),
    )
    with pytest.raises(VerificationError, match="models in another order"):
        verify_in_memory(
            history, edited, timeline.dangling, _replayed(history, original)
        )


def _replayed(history: Any, edited: dict[int, list[Any]]) -> Any:
    from django.db.migrations.state import ProjectState

    state = ProjectState()
    for info in history.migrations:
        for op in edited.get(info.index, info.migration.operations):
            op.state_forwards(info.app_label, state)
    return state


def test_first_difference() -> None:
    from django.db.migrations.state import ModelState

    from django_clean_migrations.verify import first_difference

    a = ModelState("shop", "A", [])
    assert first_difference({("shop", "a"): a}, {}) == "model shop.a is missing"
    assert first_difference({}, {("shop", "a"): a}) == "model shop.a is unexpected"


def test_same_value_odd_objects() -> None:
    from django_clean_migrations.verify import same_value

    class NoEq:
        def __eq__(self, other: object) -> bool:
            raise TypeError("no")

    assert same_value(NoEq(), NoEq()) is False


def test_relation_targets_unresolvable() -> None:
    from django.db.migrations.state import ModelState
    from django.db.models import ForeignKey

    from django_clean_migrations.verify import _relation_targets

    field: Any = ForeignKey("shop.Other", on_delete=models_cascade())
    field.remote_field.model = 42  # neither a model nor a reference
    state = ModelState("shop", "A", [("f", field)])
    assert _relation_targets(state) == set()


def models_cascade() -> Any:
    from django.db.models import CASCADE

    return CASCADE


def test_dangling_tracker_opaque_state_operation() -> None:
    from django.db.migrations import operations as m
    from django.db.migrations.operations.base import Operation
    from django.db.migrations.state import ProjectState

    from django_clean_migrations.verify import DanglingTracker

    class Custom(Operation):
        pass

    op = m.SeparateDatabaseAndState(state_operations=[Custom()])
    assert DanglingTracker().before(op, "shop", ProjectState()) is None


def test_on_disk_checks(project: Project) -> None:
    from django_clean_migrations.verify import (
        VerificationError,
        state_from_disk,
        verify_on_disk,
    )

    dead_field(project)
    project.migration("shop", "0004_more", 'migrations.RunSQL("SELECT 1"),')
    project.activate()
    with project.db_blocker.unblock():
        loader, plan, state = state_from_disk()
        with pytest.raises(VerificationError, match="graph changed"):
            verify_on_disk(plan[:-1], state, {})
        gone = {("shop", "0003_remove")}
        with pytest.raises(VerificationError, match="exactly the migrations"):
            verify_on_disk(plan, state, {}, gone)
        # A migration said deleted that never was on disk, to reach the
        # later checks with the same set of migrations.
        fake = {("shop", "0000_fake")}
        with pytest.raises(VerificationError, match="another order"):
            verify_on_disk([*fake, *reversed(plan)], state, {}, fake)
        ancestors: dict[Any, frozenset[Any]] = {k: frozenset() for k in plan}
        with pytest.raises(VerificationError, match="doesn't run after the same"):
            verify_on_disk([*fake, *plan], state, {}, fake, ancestors)
        ends: dict[str, Any] = {"shop": (frozenset(), frozenset())}
        with pytest.raises(VerificationError, match="latest migrations of shop"):
            verify_on_disk([*fake, *plan], state, {}, fake, None, ends)
        verify_on_disk([*fake, *plan], state, {}, fake, None, None)


def test_on_disk_conflicts(project: Project) -> None:
    from django_clean_migrations.verify import (
        VerificationError,
        state_from_disk,
        verify_on_disk,
    )

    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_a", 'migrations.RunSQL("SELECT 1"),')
    project.migration(
        "shop",
        "0002_b",
        'migrations.RunSQL("SELECT 2"),',
        deps=[("shop", "0001_initial")],
    )
    project.activate()
    with project.db_blocker.unblock():
        loader, plan, state = state_from_disk()
        fake = {("shop", "0000_fake")}
        one_leaf = {
            "shop": (
                frozenset({("shop", "0001_initial")}),
                frozenset({("shop", "0002_a")}),
            )
        }
        with pytest.raises(VerificationError, match="conflicting migrations"):
            verify_on_disk([*fake, *plan], state, {}, fake, None, one_leaf)
