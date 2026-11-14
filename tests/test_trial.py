from __future__ import annotations

import tomllib
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.db.migrations.operations.base import Operation

from django_clean_migrations import trial as trial_module
from django_clean_migrations.core import CleanError, UnitInfo, analyse
from django_clean_migrations.snapshot import Difference, Snapshot
from tests.project import Project
from tests.test_clean import ITEM, blocked, kinds

LEGACY = 'migrations.AddField("item", "legacy", models.IntegerField(default=0)),'


def history(project: Project, code: str, extra_ops: str = "") -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        LEGACY + "\n" + extra_ops + "\nmigrations.RunPython(forwards),",
        extra="def forwards(apps, schema_editor):\n"
        "    Item = apps.get_model('shop', 'Item')\n"
        "    alias = schema_editor.connection.alias\n"
        + "\n".join(f"    {line}" for line in code.strip().splitlines()),
    )
    project.migration(
        "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
    )


def test_mention_without_effect_is_unlocked(project: Project) -> None:
    history(
        project,
        """
        for item in Item.objects.using(alias).all():
            item.legacy = 1
            item.save()
        """,
    )
    assert project.clean().removals == []
    result = project.check_equivalence(trial=True)
    assert kinds(result) == [("field", "shop.Item.legacy")]
    info = result.verification
    assert info["mode"] == "trial"
    assert info["vendor"] == "sqlite"
    assert info["unlocked"] == 1
    assert info["runs"] == 3
    assert info["failures"] == []


def test_failure_harvested_and_blocked(project: Project) -> None:
    history(
        project,
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
    result = project.check_equivalence(trial=True)
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


def test_seeded_rows(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
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
    project.migration(
        "shop",
        "0003_remove",
        """
        migrations.RemoveField("item", "legacy"),
        migrations.RemoveField("item", "spare"),
        """,
    )
    result = project.check_equivalence(trial=True)
    # "spare" is only mentioned through its name in the seed function's
    # source; seeding works without it and leaves identical rows.
    assert kinds(result) == [("field", "shop.Item.spare")]
    assert blocked(result)["shop.Item.legacy"][0].startswith(
        "trial migrate failed in shop.0002_seed RunPython:"
    )


def test_soft_reference_crash_blocked_by_trial(project: Project) -> None:
    project.migration(
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
    project.migration(
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
    project.migration(
        "shop",
        "0003_natural",
        """
        migrations.AlterModelOptions("item", {"ordering": ("name", "pk")}),
        migrations.RemoveField("item", "_name"),
        """,
    )
    result = project.check_equivalence(trial=True)
    assert result.removals == []
    (reason,) = blocked(result)["shop.Item._name"]
    assert "FieldError: Cannot resolve keyword '_name'" in reason


@pytest.fixture
def break_index_once() -> Iterator[None]:
    def hook(operations: dict[tuple[str, str], list[Operation]], n: int) -> None:
        if n == 0:
            from django.db.migrations.loader import MigrationLoader

            loader = MigrationLoader(None, ignore_no_migrations=True)
            key = next(k for k in loader.graph.nodes if k[1] == "0004_index")
            ops = operations.get(key, loader.graph.nodes[key].operations)
            operations[key] = [o for o in ops if type(o).__name__ != "AddIndex"]

    trial_module._candidate_hook = hook
    try:
        yield
    finally:
        trial_module._candidate_hook = None


@pytest.mark.usefixtures("break_index_once")
def test_schema_difference_detected(project: Project) -> None:
    # The mention makes legacy's removal depend on the trial (the static plan
    # keeps it); removals the static plan makes are never blamed.
    history(project, 'Item.objects.using(alias).count()\nunused = "legacy"')
    project.migration(
        "shop",
        "0004_index",
        'migrations.AddIndex("item", models.Index(fields=["name"], name="name_idx")),',
    )
    result = project.check_equivalence(trial=True)
    assert result.removals == []
    (reason,) = blocked(result)["shop.Item.legacy"]
    assert reason == (
        "trial: schema of table shop_item differs (index sql name_idx missing)"
    )


def test_baseline_failure(project: Project) -> None:
    project.migration(
        "shop", "0001_initial", "migrations.RunSQL('SELECT * FROM nope'),"
    )
    with pytest.raises(CleanError, match="original migration history doesn't migrate"):
        project.clean(trial=True)


def test_plan_file_round_trip(project: Project, tmp_path: Path) -> None:
    history(
        project,
        """
        for item in Item.objects.using(alias).all():
            item.legacy = 1
        """,
    )
    plan_path = tmp_path / "plan.toml"
    out, _ = project.command("plan", "--trial", "-o", str(plan_path))
    assert "Trial: 3 migrate runs on sqlite in " in out
    assert "unlocked 1 removal of 1 that the static scan blocks" in out
    data = tomllib.loads(plan_path.read_text())
    assert data["verification"]["mode"] == "trial"
    assert data["verification"]["unlocked"] == 1
    assert data["options"]["targets"] == []
    project.command("apply", "--plan", str(plan_path))
    assert 'AddField("item", "legacy"' not in project.read("shop", "0002_add")


def test_trial_with_opaque_policy(project: Project) -> None:
    history(project, "pass")
    from django.core.management import CommandError

    with pytest.raises(CommandError, match="--trial replaces the --opaque"):
        project.command("plan", "--trial", "--opaque", "block")


def test_non_atomic_failure_ends_pass(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
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
    project.migration(
        "shop",
        "0003_remove",
        'migrations.RemoveField("item", "legacy"),',
        deps=[("shop", "0002_add")],
    )
    result = project.check_equivalence(trial=True)
    assert result.removals == []
    assert [f["harvested"] for f in result.verification["failures"]] == [False]
    assert blocked(result)["shop.Item.legacy"][0].startswith(
        "trial migrate failed in shop.0002_add RunPython: FieldError"
    )


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


def test_vague_failure_bisected(project: Project) -> None:
    two_fields(project, "alpha")
    result = project.check_equivalence(trial=True)
    # The first half of the suspects is blocked; the failure goes away, so
    # the other half stays removed.
    assert kinds(result) == [("field", "shop.Item.beta")]
    assert list(blocked(result)) == ["shop.Item.alpha"]
    assert result.verification["runs"] == 4


def test_vague_failure_bisected_second_half(project: Project) -> None:
    two_fields(project, "beta")
    result = project.check_equivalence(trial=True)
    # Blocking the first half didn't help: the failure came back, so the
    # rest is blocked (the innocent first half stays blocked: conservative).
    assert result.removals == []
    assert sorted(blocked(result)) == ["shop.Item.alpha", "shop.Item.beta"]
    assert result.verification["runs"] == 5


def test_missing_names() -> None:
    from django_clean_migrations.core import UnitInfo

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
        "m", models=frozenset({"coupon"}), tables=frozenset({"shop_coupon"}), app="shop"
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
    assert missing("Cannot resolve keyword 'legacy__in' into field", units) == [legacy]
    assert missing("something else about User", units) == []
    # The model, when named, narrows; folds never make anything missing.
    team_user = unit("t", fields=frozenset({"user"}), tables=frozenset({"shop_team"}))
    fold = unit("fold:x", fields=frozenset({"user"}))
    both = [user, team_user, fold]
    assert missing("Team has no field named 'user'", both) == [team_user]
    assert missing("Cannot resolve keyword 'user' into field", both) == [
        user,
        team_user,
    ]


def test_shorten_error() -> None:
    message = "Cannot resolve keyword 'x' into field. Choices are: " + "a, " * 200
    assert trial_module.shorten_error(message) == (
        "Cannot resolve keyword 'x' into field. (choices omitted)"
    )
    assert len(trial_module.shorten_error("y" * 500)) == 200


def test_sqlite_column_order(project: Project) -> None:
    project.migration(
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
    project.migration(
        "shop",
        "0002_longer",
        'migrations.AlterField("item", "name", models.CharField(max_length=20)),',
    )
    static = project.clean(skip_kinds=[])
    assert [r.kind for r in static.removals] == ["fold"]
    # Folding keeps the column where CreateModel had it, while the
    # AlterField rebuilt the SQLite table with its field last.
    result = project.clean(trial=True, skip_kinds=[])
    assert result.removals == []
    assert result.verification["trial_blocked"] == 1
    assert result.verification["unlocked"] <= result.verification["at_risk"]
    loose = project.clean(trial=True, skip_kinds=[], trial_ignore_column_order=True)
    assert len(loose.removals) == 1
    out, _ = project.command("plan", "--kind", "fold")
    assert "physical order of columns" in out
    out, _ = project.command("plan", "--kind", "fold", "--trial")
    assert "physical order of columns" not in out


def test_elidable_writer_kept(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
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
    static = project.clean()
    # The elidable operation goes with the model it mentions...
    assert [r.kind for r in static.removals] == ["elidable", "model"]
    # ...but it wrote a row on a fresh database: the trial keeps both.
    out, _ = project.command("plan")
    assert "Elidable operations are removed on trust" in out
    result = project.check_equivalence(trial=True)
    assert result.removals == []
    assert result.verification["nondeterministic_tables"] == []


def test_post_migrate_flag(project: Project) -> None:
    from django.db.models.signals import post_migrate

    project.migration("shop", "0001_initial", ITEM)
    project.migration(
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

    def create(using: str, apps: Any, **kwargs: Any) -> None:
        apps.get_model("shop", "Item").objects.using(using).get_or_create(name="p")

    # post_migrate is only sent for apps with a models module.
    (project.path("shop").parent / "models.py").write_text("")
    post_migrate.connect(create, dispatch_uid="test_post_migrate_flag")
    try:
        # The history leaves no row behind: without post_migrate, the
        # elidable operation's removal shows nowhere.
        assert len(project.clean(trial=True).removals) == 2
        # With it, the row post_migrate creates gets another id.
        assert project.clean(trial=True, trial_post_migrate=True).removals == []
    finally:
        post_migrate.disconnect(dispatch_uid="test_post_migrate_flag")


# The trial loop, driven by scripted outcomes


MENTIONED = """
for item in Item.objects.using(alias).all():
    item.legacy = 1
"""
DEAD = 'migrations.AddField("item", "dead", models.IntegerField(default=0)),'


def unlockable(project: Project) -> None:
    """legacy: a removal only a trial unlocks; dead: one the static plan makes."""
    history(project, MENTIONED)
    project.migration("shop", "0004_dead", DEAD)
    project.migration("shop", "0005_dead", 'migrations.RemoveField("item", "dead"),')


FAILURE = trial_module.TrialFailure(("shop", "0002_add"), "RunPython", "boom", "", True)


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


def test_max_runs_reached(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    unlockable(project)
    script(
        monkeypatch,
        [([FAILURE], []), None],
        lambda self, solution, f, d: {legacy_key(solution): "x"},
    )
    result = project.clean(trial=True, trial_max_runs=1)
    assert result.verification["fallback"] is True
    assert kinds(result) == [("field", "shop.Item.dead")]


def test_nothing_new_to_blame(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlockable(project)
    script(monkeypatch, [([FAILURE], []), None], lambda *args: {})
    result = project.clean(trial=True)
    assert result.verification["fallback"] is True


def test_trial_plan_worse_than_static(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlockable(project)
    script(monkeypatch, [None, None], None)
    real = trial_module.make_result  # type: ignore[attr-defined]
    calls: list[int] = []

    def make_result(*args: Any) -> Any:
        calls.append(1)
        result = real(*args)
        if len(calls) == 2:  # the first candidate's
            result.removed_positions = frozenset()
        return result

    monkeypatch.setattr(trial_module, "make_result", make_result)
    messages: list[str] = []
    result = project.clean(
        trial=True, progress=lambda m, note=False: messages.append(m)
    )
    assert result.verification["fallback"] is True
    assert any("using the static plan" in m for m in messages)


def as_noop(trial: Any, key: str) -> None:
    """As if the removal ``key`` relied on a data migration that did nothing."""
    trial.noop_keys[key] = (0, 0)
    trial.noop[(0, 0)] = "nothing"


def test_static_part_blamed(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    unlockable(project)

    def attribute(self: Any, solution: Any, f: Any, d: Any) -> Any:
        as_noop(self, static_key(solution))
        return {static_key(solution): "x", legacy_key(solution): "y"}

    keys = script(monkeypatch, [([FAILURE], []), None], attribute)
    result = project.clean(trial=True)
    assert ":dead" not in keys[-1]
    assert result.removals == []


def test_static_plan_blamed(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_dead", DEAD)
    project.migration("shop", "0003_dead", 'migrations.RemoveField("item", "dead"),')

    def attribute(self: Any, solution: Any, f: Any, d: Any) -> Any:
        as_noop(self, static_key(solution))
        return {static_key(solution): "x"}

    script(monkeypatch, [([FAILURE], []), None], attribute)
    assert project.clean(trial=True).removals == []


def test_noop_layer_dropped(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_dead", DEAD)
    project.migration("shop", "0003_dead", 'migrations.RemoveField("item", "dead"),')

    def attribute(self: Any, solution: Any, f: Any, d: Any) -> Any:
        self.noop[(0, 0)] = "nothing"
        return {}

    script(monkeypatch, [([FAILURE], []), None], attribute)
    result = project.clean(trial=True)
    assert any(d.startswith("--noop-data") for d in result.verification["dropped"])


def test_single_baseline(project: Project) -> None:
    history(project, MENTIONED)
    result = project.clean(trial=True, trial_single_baseline=True)
    assert result.verification["runs"] == 2
    assert kinds(result) == [("field", "shop.Item.legacy")]


def test_second_baseline_fails(project: Project, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    history(
        project,
        "import pathlib\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "if marker.exists():\n"
        "    raise ValueError('second run')\n"
        "marker.write_text('')\n",
    )
    with pytest.raises(CleanError, match="not a second time: .*second run"):
        project.clean(trial=True)


def test_scratch_database_failure(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    from django_clean_migrations.scratch import ScratchDatabaseError

    history(project, MENTIONED)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ScratchDatabaseError("no room")

    monkeypatch.setattr(trial_module, "scratch_database", fail)
    with pytest.raises(CleanError, match="no room"):
        project.clean(trial=True)


def test_child_crash(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    history(project, MENTIONED)

    class Odd(Exception):
        def __init__(self) -> None:
            super().__init__("odd")
            self.callback = lambda: None

    def migrate(*args: Any, **kwargs: Any) -> Any:
        raise Odd()

    monkeypatch.setattr(trial_module.Trial, "_migrate", migrate)
    with pytest.raises(CleanError, match="crashed: Odd: odd"):
        project.clean(trial=True)


# Attribution, directly


def bare_trial(project: Project) -> trial_module.Trial:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_sql", 'migrations.RunSQL("SELECT 1"),')
    project.migration("shop", "0003_more", 'migrations.RunSQL("SELECT 2"),')
    project.activate()
    with project.db_blocker.unblock():
        return trial_module.Trial(analyse(), "default")


def unit(key: str, start: int = 0, end: int | None = None, **kwargs: Any) -> UnitInfo:
    values: dict[str, Any] = {
        "tables": frozenset(),
        "names": frozenset(),
        "at_risk": frozenset(),
    }
    values.update(kwargs)
    return UnitInfo(key, start, end, **values)


def test_in_window(project: Project) -> None:
    trial = bare_trial(project)
    assert trial._in_window(unit("u", 0, 2), 1) is True
    assert trial._in_window(unit("u", 1, 2), 0) is False
    assert trial._in_window(unit("u", 0, 1), 2) is False
    assert trial._in_window(unit("u", 0, None), 2) is True


def test_attribute_vague_failures(project: Project) -> None:
    trial = bare_trial(project)
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


def test_attribute_data_differences(project: Project) -> None:
    trial = bare_trial(project)
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


def test_mask_nondeterminism(project: Project) -> None:
    trial = bare_trial(project)
    trial.single_baseline = True
    reference = Snapshot("sqlite", data={"t": Counter({("1",): 1}), "u": Counter()})
    snapshot = Snapshot(
        "sqlite", data={"t": Counter({("2",): 1}), "u": Counter({("1",): 1})}
    )
    diffs = reference.differences(snapshot, trial.mask, trial.masked)
    solution = SimpleNamespace(units=[unit("u", tables=frozenset({"u"}))])
    # t: no removal relates to it, so its rows are random; u's are not.
    (left,) = trial._mask_nondeterminism(solution, diffs, reference, snapshot)  # type: ignore[arg-type]
    assert left.table == "u"
    assert trial.masked == {"t"}


def test_duration() -> None:
    assert trial_module._duration(30) == "30s"
    assert trial_module._duration(600) == "10 min"


def test_elidable_in_excluded_app_at_risk(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_add", LEGACY)
    project.migration(
        "other",
        "0001_initial",
        'migrations.RunSQL("UPDATE shop_item SET legacy = 1", elidable=True),',
        deps=[("shop", "0002_add")],
    )
    project.migration(
        "shop",
        "0003_remove",
        'migrations.RemoveField("item", "legacy"),',
        extra_deps=[("other", "0001_initial")],
    )
    result = project.clean(exclude=["other"], trial=True)
    # Kept: removing the column breaks the UPDATE, which can't go.
    assert result.removals == []
    assert "no such column: legacy" in blocked(result)["shop.Item.legacy"][0]
