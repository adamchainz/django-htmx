from __future__ import annotations

import tomllib
from types import SimpleNamespace
from typing import Any

import pytest
from django.db import connections, migrations
from django.db.models.signals import post_migrate, pre_migrate
from django.test import override_settings
from unittest_parametrize import parametrize

from django_compact_migrations import backends
from django_compact_migrations import trial as trial_module
from django_compact_migrations.core import Analysis, CompactError, UnitInfo, analyse
from django_compact_migrations.scratch import (
    ScratchDatabaseError,
)
from django_compact_migrations.snapshot import (
    Difference,
)
from tests.project import (
    FAILURE,
    ITEM,
    MENTIONED,
    NO_FOLD,
    Project,
    ProjectTestCase,
    blocked,
    kinds,
    with_data_migration,
)

LEGACY = 'migrations.AddField("item", "legacy", models.IntegerField(default=0)),'


def break_index_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first candidate run loses 0004_index's AddIndex."""
    real = trial_module.Trial._run

    def run(self, label, operations, **kwargs):
        if label == "Trial run 1":
            from django.db.migrations.loader import MigrationLoader

            loader = MigrationLoader(None, ignore_no_migrations=True)
            key = next(k for k in loader.graph.nodes if k[1] == "0004_index")
            ops = operations.get(key, loader.graph.nodes[key].operations)
            operations[key] = [o for o in ops if type(o).__name__ != "AddIndex"]
        return real(self, label, operations, **kwargs)

    monkeypatch.setattr(trial_module.Trial, "_run", run)


def two_fields(project: Project, check: str) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        """
        migrations.AddField("item", "alpha", models.IntegerField(default=0)),
        migrations.AddField("item", "beta", models.IntegerField(default=0)),
        migrations.RunPython(check),
        """,
        extra=f"""
        def check(apps, schema_editor):
            names = {{f.name for f in apps.get_model("shop", "Item")._meta.fields}}
            mentioned = ("alpha", "beta")
            if {check!r} not in names and mentioned:
                raise RuntimeError("boom")
        """,
    )
    project.migration(
        "shop",
        "0003_remove",
        """
        migrations.RemoveField("item", "alpha"),
        migrations.RemoveField("item", "beta"),
        """,
    )


# The trial loop, driven by scripted outcomes


DEAD = 'migrations.AddField("item", "dead", models.IntegerField(default=0)),'


def unlockable(project: Project) -> None:
    """legacy: a removal only a trial unlocks; dead: one the static plan makes."""
    with_data_migration(project, MENTIONED)
    project.migration("shop", "0004_dead", DEAD)
    project.migration("shop", "0005_dead", 'migrations.RemoveField("item", "dead"),')


def script(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[Any], attribute: Any
) -> list[str]:
    """Replace the runs of candidates and the blame with scripted ones."""
    keys: list[str] = []

    def candidate(self: Any, solution: Any, built: Any, reference: Any) -> Any:
        self.passes += 1
        keys.append(",".join(sorted(solution.unit_keys())))
        return outcomes.pop(0)

    monkeypatch.setattr(trial_module.Trial, "_candidate", candidate)
    monkeypatch.setattr(trial_module.Trial, "_attribute", attribute)
    return keys


def static_key(solution: Any) -> str:
    return next(str(k) for k in solution.unit_keys() if ":dead" in k)


def legacy_key(solution: Any) -> str:
    return next(str(k) for k in solution.unit_keys() if ":legacy" in k)


def as_noop(trial: Any, key: str) -> None:
    """As if the removal ``key`` relied on a data migration that did nothing."""
    trial.noop_keys[key] = (0, 0)
    trial.noop[(0, 0)] = "nothing"


# Attribution, directly


def bare_trial(project: Project) -> trial_module.Trial:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_sql", 'migrations.RunSQL("SELECT 1"),')
    project.migration("shop", "0003_more", 'migrations.RunSQL("SELECT 2"),')
    project.activate()
    with project.db_blocker.unblock():
        return trial_module.Trial(
            analyse(),
            "default",
            None,
            noop_data=False,
        )


def unit(key: str, start: int = 0, end: int | None = None, **kwargs: Any) -> UnitInfo:
    values: dict[str, Any] = {
        "tables": frozenset(),
        "names": frozenset(),
        "at_risk": frozenset(),
    }
    values.update(kwargs)
    return UnitInfo(key, start, end, **values)


# Gaps the safety review found in what a trial compares


ROUTER = """
class Router:
    def allow_migrate(self, db, app_label, **hints):
        return hints.get("database", "default") == db
"""


LEAKY = """
COUNTS = {}


def forwards(apps, schema_editor):
    # A module-level cache, as projects do: wrong if it survives a run.
    COUNTS["runs"] = COUNTS.get("runs", 0) + 1
    if COUNTS["runs"] > 1:
        raise RuntimeError("state leaked from a previous run")
    alias = schema_editor.connection.alias
    apps.get_model("shop", "Item").objects.using(alias).filter(name="legacy").count()
"""


SERVERS_KIND = {"mysql": backends.MYSQL, "mariadb": backends.MARIADB}

SERVER_NAMES = {"mysql": "MySQL", "mariadb": "MariaDB"}


class TrialTests(ProjectTestCase):
    def test_mention_without_effect_is_unlocked(self):
        with_data_migration(
            self.project,
            """
            for item in Item.objects.using(alias).all():
                item.legacy = 1
                item.save()
            """,
        )
        assert self.project.compact(**NO_FOLD).removals == []
        result = self.project.check_equivalence(static=False, **NO_FOLD)
        assert kinds(result) == [("field", "shop.Item.legacy")]
        info = result.verification
        assert info["mode"] == "trial"
        assert info["vendor"] == "sqlite"
        assert info["unlocked"] == 1
        assert info["runs"] == 3
        assert info["failures"] == []

    def test_failure_harvested_and_blocked(self):
        with_data_migration(
            self.project,
            "Item.objects.using(alias).filter(legacy=1).count()",
            extra_ops="""
            migrations.CreateModel(
                name="Coupon",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            migrations.RunSQL("SELECT 'coupon'"),
            migrations.DeleteModel("Coupon"),
            """,
        )
        result = self.project.check_equivalence(static=False, **NO_FOLD)
        assert kinds(result) == [("model", "shop.Coupon")]
        (reason,) = blocked(result)["shop.Item.legacy"]
        assert reason.startswith(
            "trial migrate failed in shop.0002_add RunPython: FieldError: Cannot"
            " resolve keyword 'legacy'"
        )
        info = result.verification
        assert info["runs"] == 4
        assert info["trial_blocked"] == 1
        assert [f["harvested"] for f in info["failures"]] == [True]

    def test_seeded_rows(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_seed",
            """
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.AddField("item", "spare", models.IntegerField(default=0)),
            migrations.RunPython(seed),
            """,
            extra="""
            def seed(apps, schema_editor):
                Item = apps.get_model("shop", "Item")
                alias = schema_editor.connection.alias
                Item.objects.using(alias).create(name="a", legacy=5)
                Item.objects.using(alias).create(name="spare-free")
            """,
        )
        self.project.migration(
            "shop",
            "0003_remove",
            """
            migrations.RemoveField("item", "legacy"),
            migrations.RemoveField("item", "spare"),
            """,
        )
        result = self.project.check_equivalence(static=False, **NO_FOLD)
        # "spare" is only mentioned through its name in the seed function's
        # source; seeding works without it and leaves identical rows.
        assert kinds(result) == [("field", "shop.Item.spare")]
        assert blocked(result)["shop.Item.legacy"][0].startswith(
            "trial migrate failed in shop.0002_seed RunPython:"
        )

    def test_soft_reference_crash_blocked_by_trial(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Item",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("name", models.CharField(max_length=10)),
                ],
                options={"ordering": ("_name", "pk")},
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0002_name",
            """
            migrations.AddField("item", "_name", models.CharField(default="", max_length=10)),
            migrations.RunPython(forwards),
            """,
            extra="""
            def forwards(apps, schema_editor):
                alias = schema_editor.connection.alias
                list(apps.get_model("shop", "Item").objects.using(alias).all())
            """,
        )
        self.project.migration(
            "shop",
            "0003_natural",
            """
            migrations.AlterModelOptions("item", {"ordering": ("name", "pk")}),
            migrations.RemoveField("item", "_name"),
            """,
        )
        result = self.project.check_equivalence(static=False, **NO_FOLD)
        assert result.removals == []
        (reason,) = blocked(result)["shop.Item._name"]
        assert "FieldError: Cannot resolve keyword '_name'" in reason

    def test_schema_difference_detected(self):
        break_index_once(self.monkeypatch)
        with_data_migration(
            self.project, 'Item.objects.using(alias).count()\nunused = "legacy"'
        )
        self.project.migration(
            "shop",
            "0004_index",
            'migrations.AddIndex("item", models.Index(fields=["name"], name="name_idx")),',
        )
        result = self.project.check_equivalence(static=False, **NO_FOLD)
        assert result.removals == []
        (reason,) = blocked(result)["shop.Item.legacy"]
        assert reason == (
            "trial: schema of table shop_item differs (index sql name_idx missing)"
        )

    def test_baseline_failure(self):
        self.project.migration(
            "shop", "0001_initial", "migrations.RunSQL('SELECT * FROM nope'),"
        )
        with pytest.raises(
            CompactError, match="original migration history doesn't migrate"
        ):
            self.project.compact(static=False)

    def test_plan_file_round_trip(self):
        with_data_migration(
            self.project,
            """
            for item in Item.objects.using(alias).all():
                item.legacy = 1
            """,
        )
        plan_path = self.tmp_path / "plan.toml"
        out, _ = self.project.command("plan", "-o", str(plan_path))
        assert "Trial: 3 migrate runs on SQLite (database 'default') in " in out
        assert "unlocked 1 removal of 1 that the static scan blocks" in out
        data = tomllib.loads(plan_path.read_text())
        assert data["verification"]["mode"] == "trial"
        assert data["verification"]["unlocked"] == 1
        assert data["options"]["targets"] == []
        self.project.command("apply", "--plan", str(plan_path))
        assert 'AddField("item", "legacy"' not in self.project.read("shop", "0002_add")

    def test_non_atomic_failure_ends_pass(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            source="""
            from django.db import migrations, models


            def forwards(apps, schema_editor):
                alias = schema_editor.connection.alias
                apps.get_model("shop", "Item").objects.using(alias).filter(legacy=1).count()


            class Migration(migrations.Migration):
                atomic = False
                dependencies = [("shop", "0001_initial")]
                operations = [
                    migrations.AddField("item", "legacy", models.IntegerField(default=0)),
                    migrations.RunPython(forwards),
                ]
            """,
        )
        self.project.migration(
            "shop",
            "0003_remove",
            'migrations.RemoveField("item", "legacy"),',
            deps=[("shop", "0002_add")],
        )
        result = self.project.check_equivalence(static=False, **NO_FOLD)
        assert result.removals == []
        assert [f["harvested"] for f in result.verification["failures"]] == [False]
        assert blocked(result)["shop.Item.legacy"][0].startswith(
            "trial migrate failed in shop.0002_add RunPython: FieldError"
        )

    def test_vague_failure_bisected(self):
        two_fields(self.project, "alpha")
        result = self.project.check_equivalence(static=False, **NO_FOLD)
        # The first half of the suspects is blocked; the failure goes away, so
        # the other half stays removed.
        assert kinds(result) == [("field", "shop.Item.beta")]
        assert list(blocked(result)) == ["shop.Item.alpha"]
        assert result.verification["runs"] == 4

    def test_vague_failure_bisected_second_half(self):
        two_fields(self.project, "beta")
        result = self.project.check_equivalence(static=False, **NO_FOLD)
        # Blocking the first half didn't help: the failure came back, so the
        # rest is blocked (the innocent first half stays blocked: conservative).
        assert result.removals == []
        assert sorted(blocked(result)) == ["shop.Item.alpha", "shop.Item.beta"]
        assert result.verification["runs"] == 5

    def test_shorten_error(self):
        message = "Cannot resolve keyword 'x' into field. Choices are: " + "a, " * 200
        assert trial_module.shorten_error(message) == (
            "Cannot resolve keyword 'x' into field. (choices omitted)"
        )
        assert len(trial_module.shorten_error("y" * 500)) == 200

    def test_sqlite_column_order(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Item",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("name", models.CharField(max_length=10)),
                    ("qty", models.IntegerField()),
                ],
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0002_longer",
            'migrations.AlterField("item", "name", models.CharField(max_length=20)),',
        )
        static = self.project.compact()
        assert [r.kind for r in static.removals] == ["fold"]
        # Folding keeps the column where CreateModel had it, while the
        # AlterField rebuilt the SQLite table with its field last.
        result = self.project.compact(static=False)
        assert result.removals == []
        assert result.verification["trial_blocked"] == 1
        assert result.verification["unlocked"] <= result.verification["at_risk"]
        out, _ = self.project.command("plan", "--static")
        assert "physical order of columns" in out
        out, _ = self.project.command("plan")
        assert "physical order of columns" not in out

    def test_elidable_writer_kept(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_coupon",
            """
            migrations.CreateModel(
                name="Coupon",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            migrations.RunPython(seed, elidable=True),
            migrations.DeleteModel("Coupon"),
            """,
            extra="""
            def seed(apps, schema_editor):
                alias = schema_editor.connection.alias
                apps.get_model("shop", "Coupon").objects.using(alias).count()
                apps.get_model("shop", "Item").objects.using(alias).create(name="x")
            """,
        )
        static = self.project.compact()
        # The elidable operation goes with the model it mentions...
        assert [r.kind for r in static.removals] == ["elidable", "model"]
        # ...but it wrote a row on a fresh database: the trial keeps both.
        out, _ = self.project.command("plan", "--static")
        assert "Elidable operations are removed on trust" in out
        result = self.project.check_equivalence(static=False)
        assert result.removals == []
        assert result.verification["nondeterministic_tables"] == []

    def test_post_migrate_sent(self):

        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_coupon",
            """
            migrations.CreateModel(
                name="Coupon",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            migrations.RunPython(churn, elidable=True),
            migrations.DeleteModel("Coupon"),
            """,
            extra="""
            def churn(apps, schema_editor):
                alias = schema_editor.connection.alias
                apps.get_model("shop", "Coupon").objects.using(alias).count()
                Item = apps.get_model("shop", "Item")
                Item.objects.using(alias).create(name="tmp").delete()
            """,
        )

        sent = self.project.root / "sent"

        def create(using: str, apps: Any, **kwargs: Any) -> None:
            # Runs are forked: count them in a file.
            with sent.open("a") as f:
                f.write("x")
            apps.get_model("shop", "Item").objects.using(using).get_or_create(name="p")

        # post_migrate is only sent for apps with a models module.
        (self.project.path("shop").parent / "models.py").write_text("")
        post_migrate.connect(create, dispatch_uid="test_post_migrate_flag")
        try:
            # The history leaves no row behind, but the table's next id shows
            # the elidable operation's removal, and so does the id of the row
            # post_migrate creates, at the end of every run.
            assert self.project.compact(static=False).removals == []
            assert len(sent.read_text()) >= 3
        finally:
            post_migrate.disconnect(dispatch_uid="test_post_migrate_flag")

    def test_max_runs_reached(self):
        unlockable(self.project)
        script(
            self.monkeypatch,
            [([FAILURE], []), None],
            lambda self, solution, f, d, **kw: {legacy_key(solution): "x"},
        )
        self.monkeypatch.setattr(trial_module, "MAX_RUNS", 1)
        result = self.project.compact(static=False, **NO_FOLD)
        assert result.verification["fallback"] is True
        assert kinds(result) == [("field", "shop.Item.dead")]

    def test_nothing_new_to_blame(self):
        unlockable(self.project)
        script(self.monkeypatch, [([FAILURE], []), None], lambda *args, **kwargs: {})
        result = self.project.compact(static=False)
        assert result.verification["fallback"] is True

    def test_trial_plan_worse_than_static(self):
        unlockable(self.project)
        script(self.monkeypatch, [None, None], None)
        real = trial_module.make_result  # type: ignore[attr-defined]
        calls: list[int] = []

        def make_result(*args: Any) -> Any:
            calls.append(1)
            result = real(*args)
            if len(calls) == 2:  # the first candidate's
                result.removed_positions = frozenset()
            return result

        self.monkeypatch.setattr(trial_module, "make_result", make_result)
        messages: list[str] = []
        result = self.project.compact(
            static=False, progress=lambda m, note=False: messages.append(m)
        )
        assert result.verification["fallback"] is True
        assert any("using the static plan" in m for m in messages)

    def test_static_part_blamed(self):
        unlockable(self.project)

        def attribute(self: Any, solution: Any, f: Any, d: Any, **kw: Any) -> Any:
            as_noop(self, static_key(solution))
            return {static_key(solution): "x", legacy_key(solution): "y"}

        keys = script(self.monkeypatch, [([FAILURE], []), None], attribute)
        result = self.project.compact(static=False, **NO_FOLD)
        assert ":dead" not in keys[-1]
        assert result.removals == []

    def test_static_plan_blamed(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_dead", DEAD)
        self.project.migration(
            "shop", "0003_dead", 'migrations.RemoveField("item", "dead"),'
        )

        def attribute(self: Any, solution: Any, f: Any, d: Any, **kw: Any) -> Any:
            as_noop(self, static_key(solution))
            return {static_key(solution): "x"}

        script(self.monkeypatch, [([FAILURE], []), None], attribute)
        assert self.project.compact(static=False, **NO_FOLD).removals == []

    def test_noop_layer_dropped(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_dead", DEAD)
        self.project.migration(
            "shop", "0003_dead", 'migrations.RemoveField("item", "dead"),'
        )

        def attribute(self: Any, solution: Any, f: Any, d: Any, **kw: Any) -> Any:
            self.noop[(0, 0)] = "nothing"
            return {}

        script(self.monkeypatch, [([FAILURE], []), None], attribute)
        result = self.project.compact(static=False)
        assert any(d.startswith("--noop-data") for d in result.verification["dropped"])

    def test_second_baseline_fails(self):
        marker = self.tmp_path / "ran"
        with_data_migration(
            self.project,
            "import pathlib\n"
            f"marker = pathlib.Path({str(marker)!r})\n"
            "if marker.exists():\n"
            "    raise ValueError('second run')\n"
            "marker.write_text('')\n",
        )
        with pytest.raises(CompactError, match="not a second time: .*second run"):
            self.project.compact(static=False)

    def test_scratch_database_failure(self):

        with_data_migration(self.project, MENTIONED)

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise ScratchDatabaseError("no room")

        self.monkeypatch.setattr(trial_module, "scratch_database", fail)
        with pytest.raises(CompactError, match="no room"):
            self.project.compact(static=False)

    def test_child_crash(self):
        with_data_migration(self.project, MENTIONED)

        class Odd(Exception):
            def __init__(self) -> None:
                super().__init__("odd")
                self.callback = lambda: None

        def migrate(*args: Any, **kwargs: Any) -> Any:
            raise Odd()

        self.monkeypatch.setattr(trial_module.Trial, "_migrate", migrate)
        with pytest.raises(CompactError, match="crashed: Odd: odd"):
            self.project.compact(static=False)

    def test_elidable_in_excluded_app_at_risk(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_add", LEGACY)
        self.project.migration(
            "other",
            "0001_initial",
            'migrations.RunSQL("UPDATE shop_item SET legacy = 1", elidable=True),',
            deps=[("shop", "0002_add")],
        )
        self.project.migration(
            "shop",
            "0003_remove",
            'migrations.RemoveField("item", "legacy"),',
            extra_deps=[("other", "0001_initial")],
        )
        result = self.project.compact(exclude=["other"], static=False, **NO_FOLD)
        # Kept: removing the column breaks the UPDATE, which can't go.
        assert result.removals == []
        assert "no such column: legacy" in blocked(result)["shop.Item.legacy"][0]

    def test_static_removal_blamed(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_coupon",
            """
            migrations.CreateModel(
                name="Coupon", fields=[("id", models.AutoField(primary_key=True))]
            ),
            migrations.RunPython(forwards),
            migrations.DeleteModel("Coupon"),
            """,
            extra="def forwards(apps, schema_editor):\n"
            "    alias = schema_editor.connection.alias\n"
            "    apps.get_model('shop', 'Cou' + 'pon').objects.using(alias).count()\n",
        )
        assert kinds(self.project.compact()) == [("model", "shop.Coupon")]
        result = self.project.check_equivalence(static=False)
        assert result.removals == []
        (reason,) = blocked(result)["shop.Coupon"]
        assert "doesn't have a 'Coupon' model" in reason

    def test_no_plan_passes(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_dead", DEAD)
        self.project.migration(
            "shop", "0003_dead", 'migrations.RemoveField("item", "dead"),'
        )
        script(self.monkeypatch, [([FAILURE], [])], lambda *args, **kwargs: {})
        with pytest.raises(trial_module.TrialFailed, match="found no plan") as info:
            self.project.compact(static=False)
        assert "boom" in str(info.value)

    def test_trial_blames_until_a_plan_passes(self):

        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_add", LEGACY)
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        self.project.migration(
            "shop",
            "0004_qty",
            'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
        )

        real = trial_module.Trial._run

        def sabotage(self, label, operations, **kwargs):
            if label.startswith("Trial run"):
                failing = migrations.RunSQL("SELECT * FROM nowhere")
                operations = {k: [*ops, failing] for k, ops in operations.items()}
            return real(self, label, operations, **kwargs)

        self.monkeypatch.setattr(trial_module.Trial, "_run", sabotage)
        # Every edited migration fails: blamed until nothing is edited.
        self.monkeypatch.setattr(trial_module, "MAX_RUNS", 1)
        result = self.project.compact(static=False)
        assert result.removals == []
        assert result.verification["dropped"] == []
        assert "no such table: nowhere" in blocked(result)["shop.Item.legacy"][0]

    def test_dates_compared(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_seed",
            "migrations.RunPython(seed),",
            extra="""
            def seed(apps, schema_editor):
                alias = schema_editor.connection.alias
                apps.get_model("shop", "Item").objects.using(alias).create(name="a")
            """,
        )
        self.project.migration(
            "shop",
            "0003_add",
            'migrations.AddField("item", "starts", models.DateField('
            "default=datetime.date(2000, 1, 1))),",
            extra="import datetime",
        )
        self.project.migration(
            "shop",
            "0004_alter",
            'migrations.AlterField("item", "starts", models.DateField('
            "default=datetime.date(2020, 1, 1))),",
            extra="import datetime",
        )
        result = self.project.compact(static=False)
        assert result.removals == []
        assert (
            result.verification["trial_blocked"] == 2
        )  # the fold into the AddField, and the chain into CreateModel

    def test_times_that_differ_between_runs_masked(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM + 'migrations.AddField("item", "at", models.DateTimeField(null=True)),'
            "migrations.RunPython(stamp),",
            extra="""
            def stamp(apps, schema_editor):
                from django.utils import timezone

                alias = schema_editor.connection.alias
                apps.get_model("shop", "Item").objects.using(alias).create(
                    name="a", at=timezone.now()
                )
            """,
        )
        result = self.project.compact(static=False)
        assert result.verification["nondeterministic_columns"] == {"shop_item": ["at"]}

    def test_next_id_compared(self):
        with_data_migration(
            self.project,
            'Item.objects.using(alias).create(name="probe").delete();'
            " Item.objects.using(alias).filter(legacy=1).count()",
        )
        for path in self.project.path("shop").glob("0002_*.py"):
            path.write_text(
                path.read_text().replace(
                    "RunPython(forwards)", "RunPython(forwards, elidable=True)"
                )
            )
        assert self.project.compact(**NO_FOLD).removals != []
        result = self.project.compact(static=False, **NO_FOLD)
        assert kinds(result) == []

    def test_router_skipped_operations_not_trialled(self):
        (self.project.root / "trial_router.py").write_text(ROUTER)
        self.enterContext(override_settings(DATABASE_ROUTERS=["trial_router.Router"]))
        with_data_migration(
            self.project, "Item.objects.using(alias).filter(legacy=1).count()"
        )
        path = self.project.path("shop", "0002_add")
        path.write_text(
            path.read_text().replace(
                "RunPython(forwards)",
                'RunPython(forwards, hints={"database": "other"})',
            )
        )
        result = self.project.compact(static=False, **NO_FOLD)
        assert result.removals == []
        assert result.verification["not_trialled"] == ["shop.0002_add #2"]
        assert blocked(result)["shop.Item.legacy"] == (
            "RunPython in shop.0002_add mentions 'legacy' (it doesn't run on the"
            " database 'default', which the trial migrates (DATABASE_ROUTERS))",
        )
        out, _ = self.project.command_db("plan", "--skip-kind", "fold")
        assert (
            "Database routers keep these data migrations from running on 'default',"
            " so the trial can't check them and what they mention is kept:"
            " shop.0002_add #2.\n"
        ) in out

    def test_runs_isolated_from_each_other(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),\n'
            "migrations.RunPython(forwards),",
            extra=LEAKY,
        )
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = self.project.check_equivalence(static=False)
        # The static plan folds the AddField (its removal is blocked by the
        # mention); the trial then removes the field too.
        assert kinds(result) == [
            ("field", "shop.Item.legacy"),
            ("fold", "shop.Item.legacy"),
        ]
        assert result.verification["failures"] == []
        assert result.verification["runs"] == 3

    def test_pre_migrate_sent(self):
        def create_table(using: str, **kwargs: Any) -> None:

            with connections[using].cursor() as cursor:
                cursor.execute("CREATE TABLE IF NOT EXISTS aux (x int)")

        pre_migrate.connect(create_table, dispatch_uid="test_pre_migrate_sent")
        try:
            with_data_migration(
                self.project,
                "pass",
                extra_ops='migrations.RunSQL("INSERT INTO aux VALUES (1)"),',
            )
            # pre_migrate is only sent for apps with a models module.
            (self.project.path("shop").parent / "models.py").write_text("")
            result = self.project.compact(static=False)
        finally:
            pre_migrate.disconnect(dispatch_uid="test_pre_migrate_sent")
        assert result.verification["failures"] == []

    def test_nondeterministic_rows_not_blamed(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_seed",
            """
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.RunPython(seed),
            """,
            extra="""
            import uuid


            def seed(apps, schema_editor):
                alias = schema_editor.connection.alias
                Item = apps.get_model("shop", "Item")
                Item.objects.using(alias).create(name=uuid.uuid4().hex)
                Item.objects.using(alias).filter(name="legacy").update(name="x")
            """,
        )
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = self.project.check_equivalence(static=False)
        assert kinds(result) == [
            ("field", "shop.Item.legacy"),
            ("fold", "shop.Item.legacy"),
        ]
        assert result.verification["nondeterministic_columns"] == {
            "shop_item": ["name"]
        }
        assert result.verification["trial_blocked"] == 0

    def test_never_worse_than_static(self):
        with_data_migration(
            self.project, "Item.objects.using(alias).filter(legacy=1).count()"
        )
        self.project.migration(
            "shop",
            "0004_spare",
            """
            migrations.AddField("item", "spare", models.IntegerField(default=0)),
            migrations.RemoveField("item", "spare"),
            """,
        )
        static = self.project.compact()
        assert ("field", "shop.Item.spare") in kinds(static)
        original = Analysis.solve

        def solve(self: Any, aggressive: bool = False, **kwargs: Any) -> Any:
            # Simulate blame landing on the static plan's removals.
            if kwargs.get("trial_blocked") is not None:
                static_keys = original(self).unit_keys()
                kwargs["trial_blocked"] = dict(kwargs["trial_blocked"]) | dict.fromkeys(
                    static_keys, "test"
                )
            return original(self, aggressive=aggressive, **kwargs)

        self.monkeypatch.setattr(Analysis, "solve", solve)
        result = self.project.compact(static=False)
        # Candidates are solved on top of the static plan: whatever a trial
        # blocks, the static plan's removals stay.
        assert set(kinds(static)) <= set(kinds(result))

    def test_baseline_error_explains(self):
        self.project.migration(
            "shop", "0001_initial", "migrations.RunSQL('SELECT * FROM nope'),"
        )
        with pytest.raises(CompactError, match="another database alias") as info:
            self.project.compact(static=False)
        assert "nope" in str(info.value)

    def test_postgres_trial(self):
        pg = self.server("pg")
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),\n'
            "migrations.RunPython(forwards),",
            extra="def forwards(apps, schema_editor):\n"
            "    alias = schema_editor.connection.alias\n"
            "    apps.get_model('shop', 'Item').objects.using(alias).filter(name='legacy')"
            ".count()",
        )
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = self.project.check_equivalence(
            static=False, database=pg, noop_data=True
        )
        assert result.verification["vendor"] == "postgresql"
        assert sorted(r.kind for r in result.removals) == ["field", "noop"]

    @parametrize("server", [("mysql",), ("mariadb",)])
    def test_mysql_trial(self, server):
        my = self.server(server)
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel("Parent", [("id", models.AutoField(primary_key=True))]),
            migrations.CreateModel(
                "Child",
                [
                    ("id", models.AutoField(primary_key=True)),
                    ("x", models.IntegerField()),
                    ("parent", models.ForeignKey("shop.parent", models.CASCADE)),
                ],
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0002_add",
            'migrations.AlterUniqueTogether("child", {("parent", "x")}),',
        )
        self.project.migration(
            "shop", "0003_drop", 'migrations.AlterUniqueTogether("child", set()),'
        )
        static = self.project.compact()
        assert static.options["backends"] == [SERVERS_KIND[my]]
        # Only adding the entry folds; dropping it again is kept (see backends).
        assert [r.description for r in static.removals] == [
            "AlterUniqueTogether in 0002_add folded into CreateModel in 0001_initial"
        ]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(backends, "configured", lambda: frozenset())
            assert self.project.compact().removals
            result = self.project.check_equivalence(static=False, database=my)
        assert result.removals == []
        (reason,) = blocked(result)["shop.Child unique_together (parent, x)"]
        assert reason.startswith("trial: schema of table shop_child differs (index")
        info = result.verification
        assert (info["vendor"], info["server"]) == ("mysql", SERVER_NAMES[my])

    @parametrize("server", [("mysql",), ("mariadb",)])
    def test_mysql_noop_trial(self, server):
        my = self.server(server)
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_data",
            "migrations.RunPython(migrations.RunPython.noop),",
        )
        self.project.migration(
            "shop",
            "0003_qty",
            'migrations.AddField("item", "qty", models.TextField(default="a")),',
        )
        result = self.project.check_equivalence(
            static=False, noop_data=True, prune=True, database=my
        )
        assert [r.kind for r in result.removals if r.kind == "noop"] == ["noop"]
        # The TEXT default: kept out of the CreateModel on MySQL only.
        folded = any(r.kind == "fold" for r in result.removals)
        assert folded is (my == "mariadb")

    def test_missing_names(self):

        def unit(key: str, **kwargs: Any) -> UnitInfo:
            base: dict[str, Any] = {
                "start": 0,
                "end": None,
                "tables": frozenset(),
                "names": frozenset(),
                "at_risk": frozenset(),
            }
            return UnitInfo(key, **(base | kwargs))

        legacy = unit("f", fields=frozenset({"legacy", "legacy_id"}), app="shop")
        coupon = unit(
            "m",
            models=frozenset({"coupon"}),
            tables=frozenset({"shop_coupon"}),
            app="shop",
        )
        user = unit("u", fields=frozenset({"user"}), app="shop")
        units = [legacy, coupon, user]
        missing = trial_module._missing
        assert missing(
            "Cannot resolve keyword 'legacy' into field. Choices are: user", units
        ) == [legacy]
        assert missing("'User' object has no attribute 'legacy_id'", units) == [legacy]
        assert missing('column "legacy" does not exist', units) == [legacy]
        assert missing("App 'shop' doesn't have a 'Coupon' model.", units) == [coupon]
        assert missing('relation "shop_coupon" does not exist', units) == [coupon]
        assert missing("No installed app with label 'shop'.", units) == [coupon]
        assert missing("Cannot resolve keyword 'legacy__in' into field", units) == [
            legacy
        ]
        assert missing("something else about User", units) == []
        # The model, when named, narrows; folds never make anything missing.
        team_user = unit(
            "t", fields=frozenset({"user"}), tables=frozenset({"shop_team"})
        )
        fold = unit("fold:x", fields=frozenset({"user"}))
        both = [user, team_user, fold]
        assert missing("Team has no field named 'user'", both) == [team_user]
        assert missing("Cannot resolve keyword 'user' into field", both) == [
            user,
            team_user,
        ]

    def test_in_window(self):
        trial = bare_trial(self.project)
        assert trial._in_window(unit("u", 0, 2), 1) is True
        assert trial._in_window(unit("u", 1, 2), 0) is False
        assert trial._in_window(unit("u", 0, 1), 2) is False
        assert trial._in_window(unit("u", 0, None), 2) is True

    def test_attribute_vague_failures(self):
        trial = bare_trial(self.project)
        sql = trial.analysis.history.ops[1]
        units = [unit("a", 0, 2), unit("b", 0, 2), unit("fold:c", 1, 2)]
        solution = SimpleNamespace(units=units)
        failures = [
            trial_module.TrialFailure(
                ("shop", "0002_sql"), "RunSQL", "x", "", True, sql.seq
            ),
            trial_module.TrialFailure(
                ("shop", "0002_sql"), "RunSQL", "y", "", True, sql.seq
            ),
        ]
        # Half the suspects, for the first vague failure only.
        assert list(trial._attribute(solution, failures, [])) == ["a"]  # type: ignore[arg-type]
        create = trial.analysis.history.ops[0]
        schema = trial_module.TrialFailure(
            ("shop", "0002_sql"), "AddField", "z", "", True, create.seq
        )
        # A schema operation failing blames what was folded into its migration.
        assert list(trial._attribute(solution, [schema], [])) == ["fold:c"]  # type: ignore[arg-type]

    def test_attribute_data_differences(self):
        trial = bare_trial(self.project)
        units = [
            unit("field:a", tables=frozenset({"shop_item"}), fields=frozenset({"a"})),
            unit("field:b", tables=frozenset({"shop_item"}), fields=frozenset({"b"})),
            unit("risky", at_risk=frozenset({(1, 0)})),
        ]
        solution = SimpleNamespace(units=units)
        named = Difference("shop_item", "data", "rows differ", ("b",))
        assert list(trial._attribute(solution, [], [named])) == ["field:b"]  # type: ignore[arg-type]
        elsewhere = Difference("other", "data", "rows differ")
        assert list(trial._attribute(solution, [], [elsewhere])) == ["risky"]  # type: ignore[arg-type]

    def test_duration(self):
        assert trial_module._duration(30) == "30s"
        assert trial_module._duration(600) == "10 min"

    def test_unattributed_difference_halved(self):
        trial = bare_trial(self.project)
        solution = SimpleNamespace(units=[unit("a"), unit("b")])
        odd = Difference(None, "schema", "extension x missing")
        first = trial._attribute(solution, [], [odd], include_static=True)  # type: ignore[arg-type]
        again = trial._attribute(solution, [], [odd], include_static=True)  # type: ignore[arg-type]
        assert (list(first), list(again)) == (["a"], ["b"])
        assert first["a"] == "trial: schema of the schema differs (extension x missing)"
        failure = trial_module.TrialFailure(
            ("shop", "0002_sql"), "RunSQL", "x", "", True
        )
        other: Any = SimpleNamespace(units=[unit("c", 2)])
        assert list(trial._attribute(other, [failure], [], include_static=True)) == [
            "c"
        ]
