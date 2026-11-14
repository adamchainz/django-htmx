from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django_clean_migrations.core import Result
from tests.project import Project
from tests.test_clean import ITEM, blocked, kinds

FOLD: dict[str, Any] = {"skip_kinds": []}


def folds(result: Result) -> list[tuple[str, str]]:
    return sorted((r.label, r.description) for r in result.removals if r.kind == "fold")


def test_add_field_into_create_model(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_qty",
        'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
    )
    result = project.check_equivalence(**FOLD)
    assert folds(result) == [
        (
            "shop.Item.qty",
            "AddField in 0002_qty folded into CreateModel in 0001_initial",
        )
    ]
    assert result.folded_operations == 1
    assert result.removed_operations == 0
    assert result.edited_operations == 1
    assert project.read("shop", "0001_initial").endswith(
        """\
    operations = [
        migrations.CreateModel(
            name='Item',
            fields=[
                ('id', models.AutoField(primary_key=True)),
                ('name', models.CharField(max_length=10)),
                ('qty', models.IntegerField(default=1)),
            ],
        ),
    ]
"""
    )
    assert "operations = []" in project.read("shop", "0002_qty")


def test_alter_field_into_add_field_with_one_off_default(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_qty",
        """
        migrations.RunSQL("INSERT INTO shop_item (name) VALUES ('a')"),
        migrations.AddField("item", "qty", models.IntegerField(default=5), preserve_default=False),
        """,
    )
    project.migration(
        "shop",
        "0003_qty",
        'migrations.AlterField("item", "qty", models.IntegerField(null=True)),',
    )
    result = project.check_equivalence(**FOLD)
    assert folds(result) == [
        (
            "shop.Item.qty",
            "AlterField in 0003_qty folded into AddField in 0002_qty",
        )
    ]
    source = project.read("shop", "0002_qty")
    assert "migrations.AddField(\n" in source
    assert "field=models.IntegerField(default=5, null=True)," in source
    assert "preserve_default=False," in source
    assert "operations = []" in project.read("shop", "0003_qty")


def test_chain_into_create_model(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_qty",
        'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
    )
    project.migration(
        "shop",
        "0003_qty",
        'migrations.AlterField("item", "qty", models.BigIntegerField(default=2)),',
    )
    result = project.check_equivalence(**FOLD)
    assert result.folded_operations == 2
    assert result.empty_migrations == [("shop", "0002_qty"), ("shop", "0003_qty")]
    assert "('qty', models.BigIntegerField(default=2))," in project.read(
        "shop", "0001_initial"
    )


def test_order_stops_at_first_unfoldable(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_tag",
        """
        migrations.CreateModel(
            name="Tag",
            fields=[("id", models.AutoField(primary_key=True))],
        ),
        migrations.AddField("item", "tag", models.ForeignKey("shop.tag", models.CASCADE, null=True)),
        migrations.AddField("item", "qty", models.IntegerField(default=1)),
        """,
    )
    result = project.check_equivalence(**FOLD)
    # tag can't move before Tag exists, and qty can't jump the queue.
    assert folds(result) == []


def test_retarget_makes_model_dead(project: Project) -> None:
    project.migration(
        "shop",
        "0001_initial",
        ITEM
        + """
        migrations.CreateModel(name="Old", fields=[("id", models.AutoField(primary_key=True))]),
        migrations.CreateModel(name="New", fields=[("id", models.AutoField(primary_key=True))]),
        """,
    )
    project.migration(
        "shop",
        "0002_fk",
        'migrations.AddField("item", "ref", models.ForeignKey("shop.old", models.CASCADE, null=True)),',
    )
    project.migration(
        "shop",
        "0003_fk",
        'migrations.AlterField("item", "ref", models.ForeignKey("shop.new", models.CASCADE, null=True)),',
    )
    project.migration("shop", "0004_drop", 'migrations.DeleteModel("Old"),')
    result = project.check_equivalence(**FOLD)
    assert ("model", "shop.Old") in kinds(result)
    # The AddField can't move into Item's CreateModel: New comes after it.
    assert folds(result) == [
        ("shop.Item.ref", "AlterField in 0003_fk folded into AddField in 0002_fk")
    ]


def test_retarget_to_later_model_blocked(project: Project) -> None:
    project.migration(
        "shop",
        "0001_initial",
        ITEM
        + """
        migrations.CreateModel(name="Old", fields=[("id", models.AutoField(primary_key=True))]),
        migrations.AddField("item", "ref", models.ForeignKey("shop.old", models.CASCADE, null=True)),
        """,
    )
    project.migration(
        "shop",
        "0002_fk",
        """
        migrations.CreateModel(name="New", fields=[("id", models.AutoField(primary_key=True))]),
        migrations.AlterField("item", "ref", models.ForeignKey("shop.new", models.CASCADE, null=True)),
        """,
    )
    result = project.check_equivalence(**FOLD)
    assert (
        "shop.Item.ref",
        "AlterField in 0002_fk folded into AddField in 0001_initial",
    ) not in folds(result)


def test_column_collision_blocked(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_a",
        'migrations.AddField("item", "a", models.IntegerField(default=0, db_column="legacy")),',
    )
    project.migration(
        "shop",
        "0003_a",
        'migrations.AlterField("item", "a", models.IntegerField(default=0)),',
    )
    project.migration(
        "shop",
        "0004_legacy",
        'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
    )
    result = project.check_equivalence(**FOLD)
    assert not any(
        label == "shop.Item.a" and "AlterField" in d for label, d in folds(result)
    )


def test_opaque_scan_blocks_and_trial_unlocks(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_data",
        "migrations.RunPython(forwards),",
        extra="""
        def forwards(apps, schema_editor):
            alias = schema_editor.connection.alias
            for item in apps.get_model("shop", "Item").objects.using(alias).all():
                item.save()
        """,
    )
    project.migration(
        "shop",
        "0003_qty",
        'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
    )
    assert folds(project.clean(**FOLD)) == []
    result = project.check_equivalence(trial=True, **FOLD)
    assert [label for label, _ in folds(result)] == ["shop.Item.qty"]
    assert result.verification["unlocked"] == 1


def test_imports_added(project: Project) -> None:
    project.migration(
        "shop",
        "0001_initial",
        source="""
        from django.db import migrations, models


        class Migration(migrations.Migration):
            operations = [
                migrations.CreateModel(
                    name="Tag",
                    fields=[("id", models.AutoField(primary_key=True))],
                ),
                migrations.CreateModel(
                    name="Item",
                    fields=[("id", models.AutoField(primary_key=True))],
                ),
            ]
        """,
    )
    project.migration(
        "shop",
        "0002_tag",
        'migrations.AddField("item", "tag", models.ForeignKey("shop.tag", models.CASCADE)),',
        deps=[("shop", "0001_initial")],
    )
    project.check_equivalence(**FOLD)
    assert project.read("shop", "0001_initial").startswith(
        "from django.db import migrations, models\nimport django.db.models.deletion\n"
    )


def test_import_clash_blocks(project: Project) -> None:
    project.migration(
        "shop",
        "0001_initial",
        source="""
        from django.db import migrations
        import django.db.models as models


        class Migration(migrations.Migration):
            operations = [
                migrations.CreateModel(
                    name="Item",
                    fields=[("id", models.AutoField(primary_key=True))],
                ),
            ]
        """,
    )
    project.migration(
        "shop",
        "0002_qty",
        'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
        deps=[("shop", "0001_initial")],
    )
    assert folds(project.check_equivalence(**FOLD)) == []


def test_unserializable_blocks(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_qty",
        'migrations.AddField("item", "qty", models.IntegerField(default=make_default)),',
        extra="""
        def make_default():
            return 1
        """,
    )
    assert folds(project.check_equivalence(**FOLD)) == []


def test_index_and_constraint(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_idx",
        """
        migrations.AddIndex("item", models.Index(fields=["name"], name="name_idx")),
        migrations.AddConstraint("item", models.CheckConstraint(condition=models.Q(name__gt=""), name="name_set")),
        """,
    )
    result = project.check_equivalence(**FOLD)
    assert sorted(r.name for r in result.removals) == ["name_idx", "name_set"]
    source = project.read("shop", "0001_initial")
    assert "'indexes': [models.Index(fields=['name'], name='name_idx')]," in source


def test_constraint_scan_uses_model_names(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_idx",
        """
        migrations.RunSQL("INSERT INTO shop_item (name) VALUES ('')"),
        migrations.AddConstraint("item", models.CheckConstraint(condition=models.Q(name__gt=""), name="name_set")),
        """,
    )
    assert folds(project.clean(**FOLD)) == []


def test_index_name_collision(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    # The old index can't be removed (its source isn't a literal), so its name
    # is taken until RemoveIndex: Item's index can't exist from 0001.
    project.migration(
        "lib",
        "0001_initial",
        source="""
        from django.db import migrations, models

        OPTIONS = {"indexes": [models.Index(fields=["x"], name="the_idx")]}


        class Migration(migrations.Migration):
            operations = [
                migrations.CreateModel(
                    name="Other",
                    fields=[
                        ("id", models.AutoField(primary_key=True)),
                        ("x", models.IntegerField()),
                    ],
                    options=OPTIONS,
                ),
            ]
        """,
    )
    project.migration(
        "lib",
        "0002_drop",
        'migrations.RemoveIndex("other", "the_idx"),',
        deps=[("lib", "0001_initial")],
    )
    project.migration(
        "shop",
        "0002_idx",
        'migrations.AddIndex("item", models.Index(fields=["name"], name="the_idx")),',
        extra_deps=[("lib", "0002_drop")],
    )
    result = project.check_equivalence(**FOLD)
    assert folds(result) == []


def test_together(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_ut",
        'migrations.AlterUniqueTogether("item", {("id", "name")}),',
    )
    result = project.check_equivalence(**FOLD)
    assert folds(result) == [
        (
            "shop.Item",
            "AlterUniqueTogether in 0002_ut folded into CreateModel in 0001_initial",
        )
    ]
    assert "'unique_together': {('id', 'name')}," in project.read(
        "shop", "0001_initial"
    )


def test_model_options_and_comment(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_opts",
        """
        migrations.AlterModelOptions("item", {"verbose_name": "thing", "ordering": ["name"]}),
        migrations.AlterModelTableComment("item", "Things"),
        """,
    )
    result = project.check_equivalence(**FOLD)
    assert result.folded_operations == 2
    source = project.read("shop", "0001_initial")
    assert "'verbose_name': 'thing'," in source
    assert "'db_table_comment': 'Things'," in source


def test_default_managers_not_folded(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_managers",
        'migrations.AlterModelManagers("item", [("objects", models.Manager())]),',
    )
    # CreateModel can't express the default manager list explicitly.
    assert folds(project.check_equivalence(**FOLD)) == []


def test_managers(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_managers",
        'migrations.AlterModelManagers("item", [("things", models.Manager())]),',
    )
    result = project.check_equivalence(**FOLD)
    assert result.folded_operations == 1


def test_skip_kind_and_plan_file(project: Project, tmp_path: Path) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_qty",
        'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
    )
    out, _ = project.command("plan", "--skip-kind", "fold")
    assert out == "No operations to remove.\n"
    plan_path = tmp_path / "plan.json"
    out, _ = project.command("plan", "--kind", "fold", "-v", "2", "-o", str(plan_path))
    lines = out.splitlines()
    assert lines[2].split() == [
        "qty",
        "fold",
        "0002_qty",
        "->",
        "0001_initial",
        "1",
        "operation",
    ]
    assert lines[3].strip().endswith("0002_qty.py:11  AddField")
    assert lines[4].strip().endswith("0001_initial.py:10  CreateModel (regenerated)")
    assert lines[5] == (
        "Would fold 1 operation and edit 1 operation in 2 migration files."
        " 1 migration would be left with no operations."
    )
    data = json.loads(plan_path.read_text())
    assert data["summary"]["folded_operations"] == 1
    assert data["removals"][0]["targets"][0]["detail"] == "regenerated"
    project.command("apply", "--plan", str(plan_path))
    assert "('qty', models.IntegerField(default=1))," in project.read(
        "shop", "0001_initial"
    )


def test_blocked_reason_still_reported(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
    )
    project.migration(
        "shop",
        "0003_remove",
        """
        migrations.RunSQL("SELECT legacy FROM shop_item"),
        migrations.RemoveField("item", "legacy"),
        """,
    )
    result = project.clean(**FOLD)
    assert "shop.Item.legacy" in blocked(result)


def test_rounds_see_original_to_fields(project: Project) -> None:
    # RenameField rewrites to_fields of the FK in the project state in place;
    # later fold rounds must still see that the FK used the old name.
    project.migration(
        "shop",
        "0001_initial",
        ITEM
        + """
        migrations.CreateModel(
            name="Tag",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("code", models.CharField(max_length=5, unique=True)),
            ],
        ),
        """,
    )
    project.migration(
        "shop",
        "0002_fk",
        'migrations.AddField("item", "tag", models.ForeignKey("shop.tag", models.CASCADE, to_field="code", null=True)),',
    )
    project.migration(
        "shop", "0003_rename", 'migrations.RenameField("tag", "code", "slug"),'
    )
    project.migration(
        "shop",
        "0004_qty",
        'migrations.AddField("tag", "qty", models.IntegerField(default=1)),',
    )
    project.migration(
        "shop",
        "0005_fk",
        """
        migrations.AlterField("item", "tag", models.ForeignKey("shop.tag", models.CASCADE, null=True)),
        migrations.RemoveField("tag", "slug"),
        """,
    )
    result = project.check_equivalence(**FOLD)
    assert "shop.Tag.slug" in blocked(result)
    assert result.folded_operations >= 1


def test_names_must_be_stable_across_window(project: Project) -> None:
    project.migration(
        "shop",
        "0001_initial",
        """
        migrations.CreateModel(
            name="Item",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("name", models.CharField(max_length=10)),
                ("slug", models.CharField(max_length=10)),
            ],
        ),
        """,
    )
    project.migration(
        "shop", "0002_rename", 'migrations.RenameField("item", "name", "title"),'
    )
    project.migration(
        "shop",
        "0003_more",
        """
        migrations.AlterUniqueTogether("item", {("title", "slug")}),
        migrations.AddIndex("item", models.Index(fields=["title"], name="title_idx")),
        migrations.AlterModelOptions("item", {"ordering": ["title"]}),
        migrations.AddIndex("item", models.Index(fields=["slug"], name="slug_idx")),
        """,
    )
    result = project.check_equivalence(**FOLD)
    # Each of these names "title", which was "name" when the model was
    # created; the slug index can't jump the queue of indexes either.
    assert folds(result) == []
