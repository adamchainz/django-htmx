"""Planning and writing: failures and the less common paths."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from django_compact_migrations import core, planfile, writer
from django_compact_migrations.core import CompactError, Location
from django_compact_migrations.prune import PruneError
from django_compact_migrations.rewrite import RewriteError
from django_compact_migrations.timeline import Timeline
from tests.project import ITEM, Project, blocked, kinds, only

ADD = 'migrations.AddField("item", "legacy", models.IntegerField(default=0)),'
REMOVE = 'migrations.RemoveField("item", "legacy"),'


def dead_field(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_add", ADD)
    project.migration("shop", "0003_remove", REMOVE)


def test_location_without_path():
    location = Location(("shop", "0001_initial"), None, None, "CreateModel")
    assert location.display_path() == "shop.0001_initial"


def test_static_conflicts(project):
    project.migration("shop", "0001_initial", ITEM)
    with pytest.raises(CompactError, match="can't be combined with --static"):
        project.compact(noop_data=True, static=True)


def test_broken_migration_module(project):
    project.migration(
        "shop", "0001_initial", ITEM + "\n", extra='raise ValueError("boom")'
    )
    with pytest.raises(CompactError, match="Could not load migrations: boom"):
        project.compact()


def test_changed_before_writing(project):
    dead_field(project)
    result = project.compact()
    path = project.path("shop", "0002_add")
    path.write_text(path.read_text() + "\n")
    project.path("shop", "0003_remove").unlink()
    with pytest.raises(CompactError, match="changed since the plan was made") as info:
        result.write()
    assert f"{path}," in str(info.value)
    assert "0003_remove.py (missing)" in str(info.value)


def test_restore_failure(project, monkeypatch):
    dead_field(project)
    result = project.compact()

    def fail(path: Path, data: bytes, mode: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(writer, "_replace", fail)
    with pytest.raises(CompactError, match="could not be restored") as info:
        result.write()
    assert "disk full" in str(info.value)


def test_write_text_cleans_up(tmp_path, monkeypatch):
    path = tmp_path / "m.py"
    path.write_text("old")

    def fail(*args: Any) -> None:
        raise OSError("no")

    monkeypatch.setattr(os, "replace", fail)
    monkeypatch.setattr(os, "unlink", fail)
    with pytest.raises(OSError, match="no"):
        writer._replace(path, b"new", 0o644)
    assert path.read_text() == "old"


def test_loaded_plan_with_broken_module(project, tmp_path):
    dead_field(project)
    plan_path = tmp_path / "plan.toml"
    project.command("plan", "--static", "-o", str(plan_path))
    project.migration("shop", "0004_broken", extra='raise ValueError("boom")')
    project.purge()
    with (
        project.db_blocker.unblock(),
        pytest.raises(CompactError, match="Could not load migrations: boom"),
    ):
        planfile.load_plan(plan_path)


def test_loaded_plan_then_broken_module(project, tmp_path):
    dead_field(project)
    plan_path = tmp_path / "plan.toml"
    project.command("plan", "--static", "-o", str(plan_path))
    with project.db_blocker.unblock():
        result = planfile.load_plan(plan_path)
        project.migration("shop", "0004_broken", extra='raise ValueError("boom")')
        project.purge()
        with pytest.raises(CompactError, match="Could not load migrations: boom"):
            result.write()


def test_pruned_with_broken_module(project):
    dead_field(project)
    result = project.compact(prune=True)
    project.migration("shop", "0004_broken", extra='raise ValueError("boom")')
    project.purge()
    with (
        project.db_blocker.unblock(),
        pytest.raises(CompactError, match="Could not load migrations: boom"),
    ):
        result.write()


def test_internal_errors(project, monkeypatch):
    dead_field(project)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RewriteError("bad edit")

    with monkeypatch.context() as patch:
        patch.setattr(core, "rewrite_source", fail)
        with pytest.raises(CompactError, match=r"rewriting shop.0002_add failed \(bad"):
            project.compact()

    def prune_fail(*args: Any, **kwargs: Any) -> Any:
        raise PruneError("bad prune")

    with monkeypatch.context() as patch:
        patch.setattr(core, "plan_prune", prune_fail)
        with pytest.raises(CompactError, match="Internal error: bad prune"):
            project.compact(prune=True)

    project.migration(
        "shop",
        "0004_after",
        'migrations.AlterField("item", "name", models.TextField()),',
    )
    with monkeypatch.context() as patch:
        patch.setattr(core, "rewrite_dependencies", prune_fail)
        with pytest.raises(CompactError, match=r"rewiring shop.0004_after failed"):
            project.compact(prune=True)


def test_folded_history_not_replaying(project, monkeypatch):
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_add", ADD)
    real = Timeline.replay
    calls: list[int] = []

    def replay(self: Timeline) -> None:
        calls.append(1)
        if len(calls) > 1:
            raise ValueError("odd")
        real(self)

    monkeypatch.setattr(Timeline, "replay", replay)
    with pytest.raises(
        CompactError, match=r"the folded history doesn't replay \(odd\)"
    ):
        project.compact()


def test_field_of_removed_model_reported_once(project):
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_coupon",
        """
        migrations.CreateModel(
            name="Coupon", fields=[("id", models.AutoField(primary_key=True))]
        ),
        migrations.AddField("coupon", "legacy", models.IntegerField(default=0)),
        migrations.RemoveField("coupon", "legacy"),
        migrations.DeleteModel("Coupon"),
        """,
    )
    assert kinds(project.compact()) == [("model", "shop.Coupon")]


def test_blocked_requirement_reported_with_root(project):
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        ADD
        + '\nmigrations.AddIndex("item", models.Index(fields=["legacy"], name="l_idx")),',
    )
    project.migration(
        "shop",
        "0003_remove",
        """
        migrations.RunSQL("SELECT 'l_idx'"),
        migrations.RemoveIndex("item", "l_idx"),
        migrations.RemoveField("item", "legacy"),
        """,
    )
    # Only fields are asked for: the index is no root of its own.
    assert list(blocked(project.compact(**only("field")))) == ["shop.Item.legacy"]


def test_fold_into_unlocatable_create_model(project):
    project.migration(
        "shop",
        "0001_initial",
        'migrations.CreateModel(name="Item", fields=FIELDS),',
        extra='FIELDS = [("id", models.AutoField(primary_key=True)),'
        ' ("name", models.CharField(max_length=10))]',
    )
    project.migration(
        "shop",
        "0002_alter",
        'migrations.AlterField("item", "name", models.CharField(max_length=20)),',
    )
    result = project.compact()
    (removal,) = result.removals
    (target,) = removal.targets
    assert target.line == 12  # the whole operation's line
    assert target.detail == "regenerated"


def test_flat_together_located(project):
    project.migration(
        "shop",
        "0001_initial",
        ITEM.replace(
            "        ],\n",
            '        ],\n    options={"unique_together": ("id", "name")},\n',
        ),
    )
    project.migration(
        "shop", "0002_drop", 'migrations.AlterUniqueTogether("item", set()),'
    )
    removal = project.compact().removals[0]
    (element,) = removal.elements
    assert (element.operation, element.line) == ("CreateModel", 16)


def test_fold_into_create_model_edited_earlier(project):
    # The first round removes the dead unique_together entry from the
    # CreateModel; the AlterField folds into it in the next round, when the
    # edited operation no longer matches its source.
    project.migration(
        "shop",
        "0001_initial",
        ITEM.replace(
            "        ],\n",
            '        ],\n    options={"unique_together": {("id", "name")}},\n',
        ),
    )
    project.migration(
        "shop", "0002_drop", 'migrations.AlterUniqueTogether("item", set()),'
    )
    project.migration(
        "shop",
        "0003_alter",
        'migrations.AlterField("item", "name", models.CharField(max_length=20)),',
    )
    result = project.check_equivalence()
    (fold,) = [r for r in result.removals if r.kind == "fold"]
    (target,) = fold.targets
    assert (target.operation, target.line) == ("CreateModel", 10)  # the call


def test_sub_line_of_edited_together_operation(project):
    from django.db.migrations.operations import AlterUniqueTogether

    from django_compact_migrations.history import load_history
    from django_compact_migrations.timeline import Element

    project.migration(
        "shop",
        "0001_initial",
        ITEM + 'migrations.AlterUniqueTogether("item", {("id", "name")}),',
    )
    project.activate()
    with project.db_blocker.unblock():
        rec = load_history().ops[-1]
    rec.op = AlterUniqueTogether("item", set())  # as an earlier round left it
    assert core._sub_line(Element(rec, ("entry", ("id", "name")), None)) is None


class TestWriteRestores:
    def dead_field(self, project: Project) -> None:
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )

    def test_failure_mid_write(self, project, monkeypatch):
        self.dead_field(project)
        before = project.files()
        result = project.compact()
        real = writer._replace
        calls = []

        def flaky(path, data, mode):
            calls.append(path)
            if len(calls) == 2:
                path.write_text("garbage")  # a torn write
                raise OSError("disk full")
            real(path, data, mode)

        monkeypatch.setattr(writer, "_replace", flaky)
        with pytest.raises(CompactError, match="restored: disk full"):
            result.write()
        assert project.files() == before

    def test_keyboard_interrupt(self, project, monkeypatch):
        self.dead_field(project)
        before = project.files()
        result = project.compact()

        def interrupt(*args: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(core, "verify_on_disk", interrupt)
        with pytest.raises(KeyboardInterrupt):
            result.write()
        assert project.files() == before

    def test_no_temp_files_left(self, project):
        self.dead_field(project)
        project.compact().write()
        assert sorted(p.name for p in project.path("shop").iterdir()) == [
            "0001_initial.py",
            "0002_add.py",
            "0003_remove.py",
            "__init__.py",
        ]


class TestUnknownTargets:
    def test_model(self, project):
        project.migration("shop", "0001_initial", ITEM)
        with pytest.raises(CompactError, match="No model matching 'shop.Nope'"):
            project.compact("shop.Nope")

    def test_field(self, project):
        project.migration("shop", "0001_initial", ITEM)
        with pytest.raises(CompactError, match="No field matching 'shop.Item.nope'"):
            project.compact("shop.Item.nope")

    def test_exclude(self, project):
        project.migration("shop", "0001_initial", ITEM)
        with pytest.raises(CompactError, match="No model matching 'shop.Nope'"):
            project.compact(exclude=["shop.Nope"])

    def test_old_names_ok(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration("shop", "0002_drop", 'migrations.DeleteModel("Item"),')
        assert project.compact("shop.Item.name").removals == []


def test_bom_kept(project):
    # Python reads a file with a byte order mark as UTF-8; it is written
    # back with it.
    project.migration("shop", "0001_initial", ITEM)
    path = project.migration(
        "shop",
        "0002_add",
        'migrations.AddField("item", "legacy", models.IntegerField(default=0)),\n'
        'migrations.AddField("item", "qty", models.IntegerField(default=0)),',
    )
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    project.migration(
        "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
    )
    result = project.check_equivalence()
    assert ("field", "shop.Item.legacy") in kinds(result)
    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    assert b"legacy" not in data


def test_project_helper(tmp_path, django_db_blocker):
    project = Project(tmp_path, django_db_blocker)
    project.migration("shop", "0001_initial", ITEM)
    project.activate()
    # A new app deactivates the project, to be activated with it.
    project.migration("other", "0001_initial", "")
    assert project._stack is None
    project.close()
    project.close()
