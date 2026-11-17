"""Planning and writing: failures and the less common paths."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from django.db.migrations.operations import AlterUniqueTogether

from django_compact_migrations import core, planfile, writer
from django_compact_migrations.core import CompactError, Location
from django_compact_migrations.history import load_history
from django_compact_migrations.prune import PruneError
from django_compact_migrations.rewrite import RewriteError
from django_compact_migrations.timeline import Element, Timeline
from tests.project import (
    ITEM,
    Project,
    ProjectTestCase,
    blocked,
    kinds,
    only,
)

ADD = 'migrations.AddField("item", "legacy", models.IntegerField(default=0)),'
REMOVE = 'migrations.RemoveField("item", "legacy"),'


def dead_field(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_add", ADD)
    project.migration("shop", "0003_remove", REMOVE)


class WriteRestoresTests(ProjectTestCase):
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

    def test_failure_mid_write(self):
        self.dead_field(self.project)
        before = self.project.files()
        result = self.project.compact()
        real = writer._replace
        calls = []

        def flaky(path, data, mode):
            calls.append(path)
            if len(calls) == 2:
                path.write_text("garbage")  # a torn write
                raise OSError("disk full")
            real(path, data, mode)

        self.monkeypatch.setattr(writer, "_replace", flaky)
        with pytest.raises(CompactError, match="restored: disk full"):
            result.write()
        assert self.project.files() == before

    def test_keyboard_interrupt(self):
        self.dead_field(self.project)
        before = self.project.files()
        result = self.project.compact()

        def interrupt(*args: object) -> None:
            raise KeyboardInterrupt

        self.monkeypatch.setattr(core, "verify_on_disk", interrupt)
        with pytest.raises(KeyboardInterrupt):
            result.write()
        assert self.project.files() == before

    def test_no_temp_files_left(self):
        self.dead_field(self.project)
        self.project.compact().write()
        assert sorted(p.name for p in self.project.path("shop").iterdir()) == [
            "0001_initial.py",
            "0002_add.py",
            "0003_remove.py",
            "__init__.py",
        ]


class UnknownTargetsTests(ProjectTestCase):
    def test_model(self):
        self.project.migration("shop", "0001_initial", ITEM)
        with pytest.raises(CompactError, match="No model matching 'shop.Nope'"):
            self.project.compact("shop.Nope")

    def test_field(self):
        self.project.migration("shop", "0001_initial", ITEM)
        with pytest.raises(CompactError, match="No field matching 'shop.Item.nope'"):
            self.project.compact("shop.Item.nope")

    def test_exclude(self):
        self.project.migration("shop", "0001_initial", ITEM)
        with pytest.raises(CompactError, match="No model matching 'shop.Nope'"):
            self.project.compact(exclude=["shop.Nope"])

    def test_old_names_ok(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_drop", 'migrations.DeleteModel("Item"),')
        assert self.project.compact("shop.Item.name").removals == []


class CoreTests(ProjectTestCase):
    def test_location_without_path(self):
        location = Location(("shop", "0001_initial"), None, None, "CreateModel")
        assert location.display_path() == "shop.0001_initial"

    def test_static_conflicts(self):
        self.project.migration("shop", "0001_initial", ITEM)
        with pytest.raises(CompactError, match="can't be combined with --static"):
            self.project.compact(noop_data=True, static=True)

    def test_broken_migration_module(self):
        self.project.migration(
            "shop", "0001_initial", ITEM + "\n", extra='raise ValueError("boom")'
        )
        with pytest.raises(CompactError, match="Could not load migrations: boom"):
            self.project.compact()

    def test_changed_before_writing(self):
        dead_field(self.project)
        result = self.project.compact()
        path = self.project.path("shop", "0002_add")
        path.write_text(path.read_text() + "\n")
        self.project.path("shop", "0003_remove").unlink()
        with pytest.raises(
            CompactError, match="changed since the plan was made"
        ) as info:
            result.write()
        assert f"{path}," in str(info.value)
        assert "0003_remove.py (missing)" in str(info.value)

    def test_restore_failure(self):
        dead_field(self.project)
        result = self.project.compact()

        def fail(path: Path, data: bytes, mode: int) -> None:
            raise OSError("disk full")

        self.monkeypatch.setattr(writer, "_replace", fail)
        with pytest.raises(CompactError, match="could not be restored") as info:
            result.write()
        assert "disk full" in str(info.value)

    def test_write_text_cleans_up(self):
        path = self.tmp_path / "m.py"
        path.write_text("old")

        def fail(*args: Any) -> None:
            raise OSError("no")

        self.monkeypatch.setattr(os, "replace", fail)
        self.monkeypatch.setattr(os, "unlink", fail)
        with pytest.raises(OSError, match="no"):
            writer._replace(path, b"new", 0o644)
        assert path.read_text() == "old"

    def test_loaded_plan_with_broken_module(self):
        dead_field(self.project)
        plan_path = self.tmp_path / "plan.toml"
        self.project.command("plan", "--static", "-o", str(plan_path))
        self.project.migration("shop", "0004_broken", extra='raise ValueError("boom")')
        self.project.purge()
        with (
            self.project.db_blocker.unblock(),
            pytest.raises(CompactError, match="Could not load migrations: boom"),
        ):
            planfile.load_plan(plan_path)

    def test_loaded_plan_then_broken_module(self):
        dead_field(self.project)
        plan_path = self.tmp_path / "plan.toml"
        self.project.command("plan", "--static", "-o", str(plan_path))
        with self.project.db_blocker.unblock():
            result = planfile.load_plan(plan_path)
            self.project.migration(
                "shop", "0004_broken", extra='raise ValueError("boom")'
            )
            self.project.purge()
            with pytest.raises(CompactError, match="Could not load migrations: boom"):
                result.write()

    def test_pruned_with_broken_module(self):
        dead_field(self.project)
        result = self.project.compact(prune=True)
        self.project.migration("shop", "0004_broken", extra='raise ValueError("boom")')
        self.project.purge()
        with (
            self.project.db_blocker.unblock(),
            pytest.raises(CompactError, match="Could not load migrations: boom"),
        ):
            result.write()

    def test_internal_errors(self):
        dead_field(self.project)

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise RewriteError("bad edit")

        with self.monkeypatch.context() as patch:
            patch.setattr(core, "rewrite_source", fail)
            with pytest.raises(
                CompactError, match=r"rewriting shop.0002_add failed \(bad"
            ):
                self.project.compact()

        def prune_fail(*args: Any, **kwargs: Any) -> Any:
            raise PruneError("bad prune")

        with self.monkeypatch.context() as patch:
            patch.setattr(core, "plan_prune", prune_fail)
            with pytest.raises(CompactError, match="Internal error: bad prune"):
                self.project.compact(prune=True)

        self.project.migration(
            "shop",
            "0004_after",
            'migrations.AlterField("item", "name", models.TextField()),',
        )
        with self.monkeypatch.context() as patch:
            patch.setattr(core, "rewrite_dependencies", prune_fail)
            with pytest.raises(CompactError, match=r"rewiring shop.0004_after failed"):
                self.project.compact(prune=True)

    def test_folded_history_not_replaying(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_add", ADD)
        real = Timeline.replay
        calls: list[int] = []

        def replay(self: Timeline) -> None:
            calls.append(1)
            if len(calls) > 1:
                raise ValueError("odd")
            real(self)

        self.monkeypatch.setattr(Timeline, "replay", replay)
        with pytest.raises(
            CompactError, match=r"the folded history doesn't replay \(odd\)"
        ):
            self.project.compact()

    def test_field_of_removed_model_reported_once(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
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
        assert kinds(self.project.compact()) == [("model", "shop.Coupon")]

    def test_blocked_requirement_reported_with_root(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            ADD
            + '\nmigrations.AddIndex("item", models.Index(fields=["legacy"], name="l_idx")),',
        )
        self.project.migration(
            "shop",
            "0003_remove",
            """
            migrations.RunSQL("SELECT 'l_idx'"),
            migrations.RemoveIndex("item", "l_idx"),
            migrations.RemoveField("item", "legacy"),
            """,
        )
        # Only fields are asked for: the index is no root of its own.
        assert list(blocked(self.project.compact(**only("field")))) == [
            "shop.Item.legacy"
        ]

    def test_fold_into_unlocatable_create_model(self):
        self.project.migration(
            "shop",
            "0001_initial",
            'migrations.CreateModel(name="Item", fields=FIELDS),',
            extra='FIELDS = [("id", models.AutoField(primary_key=True)),'
            ' ("name", models.CharField(max_length=10))]',
        )
        self.project.migration(
            "shop",
            "0002_alter",
            'migrations.AlterField("item", "name", models.CharField(max_length=20)),',
        )
        result = self.project.compact()
        (removal,) = result.removals
        (target,) = removal.targets
        assert target.line == 12  # the whole operation's line
        assert target.detail == "regenerated"

    def test_flat_together_located(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM.replace(
                "        ],\n",
                '        ],\n    options={"unique_together": ("id", "name")},\n',
            ),
        )
        self.project.migration(
            "shop", "0002_drop", 'migrations.AlterUniqueTogether("item", set()),'
        )
        removal = self.project.compact().removals[0]
        (element,) = removal.elements
        assert (element.operation, element.line) == ("CreateModel", 16)

    def test_fold_into_create_model_edited_earlier(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM.replace(
                "        ],\n",
                '        ],\n    options={"unique_together": {("id", "name")}},\n',
            ),
        )
        self.project.migration(
            "shop", "0002_drop", 'migrations.AlterUniqueTogether("item", set()),'
        )
        self.project.migration(
            "shop",
            "0003_alter",
            'migrations.AlterField("item", "name", models.CharField(max_length=20)),',
        )
        result = self.project.check_equivalence()
        (fold,) = [r for r in result.removals if r.kind == "fold"]
        (target,) = fold.targets
        assert (target.operation, target.line) == ("CreateModel", 10)  # the call

    def test_bom_kept(self):
        self.project.migration("shop", "0001_initial", ITEM)
        path = self.project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),\n'
            'migrations.AddField("item", "qty", models.IntegerField(default=0)),',
        )
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = self.project.check_equivalence()
        assert ("field", "shop.Item.legacy") in kinds(result)
        data = path.read_bytes()
        assert data.startswith(b"\xef\xbb\xbf")
        assert b"legacy" not in data

    def test_project_helper(self):
        Project(self.tmp_path, self.db_blocker)
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.activate()
        # A new app deactivates the project, to be activated with it.
        self.project.migration("other", "0001_initial", "")
        assert self.project._stack is None
        self.project.close()
        self.project.close()

    def test_sub_line_of_edited_together_operation(self):

        self.project.migration(
            "shop",
            "0001_initial",
            ITEM + 'migrations.AlterUniqueTogether("item", {("id", "name")}),',
        )
        self.project.activate()
        with self.project.db_blocker.unblock():
            rec = load_history().ops[-1]
        rec.op = AlterUniqueTogether("item", set())  # as an earlier round left it
        assert core._sub_line(Element(rec, ("entry", ("id", "name")), None)) is None
