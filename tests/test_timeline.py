"""Replaying odd histories: what the timeline tracks, and what it gives up on."""

from __future__ import annotations

from typing import Any

import pytest
from django.db import migrations, models
from django.db.models import F, Q

from django_compact_migrations.core import CompactError, analyse
from django_compact_migrations.timeline import (
    Element,
    FieldLife,
    ModelLife,
    Timeline,
    TogetherLife,
    _option_field_names,
    _same,
    mentioned_names,
)
from tests.project import Project
from tests.test_compact import ITEM

STATE_OPS = "from tests.state_ops import Change"


def replay(project: Project) -> Timeline:
    project.activate()
    with project.db_blocker.unblock():
        return analyse().timeline


def poisoned(timeline: Timeline) -> dict[str, list[str]]:
    """Poison reasons by label (lives sharing a name share an entry)."""
    reasons: dict[str, list[str]] = {}
    for life in timeline.lives:
        if life.poisoned:
            reasons.setdefault(life.label, []).extend(life.poison_reasons)
    return reasons


def lives(timeline: Timeline, label: str) -> list[Any]:
    return [life for life in timeline.lives if life.label == label]


# Helpers


def test_mentioned_names() -> None:
    class Odd:
        def deconstruct(self) -> Any:
            raise ValueError

        def get_source_expressions(self) -> Any:
            raise ValueError

    class Plain:
        def deconstruct(self) -> Any:
            return "not a tuple"

    class Expression:
        def get_source_expressions(self) -> Any:
            return [F("c")]

    values = ["author_id", Odd(), Plain(), Expression(), F("a"), Q(b=1)]
    assert mentioned_names(values) == {"author_id", "author", "a", "b", "c"}


def test_default_related_names() -> None:
    model = ModelLife("shop", "Item")
    model.default_related_names = {"+", "%(class)s_things"}
    fk = FieldLife(model, "parent")
    element = Element(None, None, fk)  # type: ignore[arg-type]
    fk.defs.append((element, models.ForeignKey("shop.item", models.CASCADE), None))
    assert fk.scan_names() == {"parent", "item", "item_things"}


def test_option_field_names() -> None:
    assert _option_field_names({"get_latest_by": "-created"}) == {"created"}


def test_same_with_failing_comparison() -> None:
    class Odd:
        def __eq__(self, other: object) -> bool:
            raise ValueError

    assert _same(Odd(), 1) is False


# Known operations


def test_reused_names(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_again",
        f"""
        {ITEM.strip()}
        migrations.AddField("item", "name", models.IntegerField(default=0)),
        migrations.AddField("item", "size", models.IntegerField(default=0)),
        migrations.RenameField("item", "size", "name"),
        migrations.AddIndex("item", models.Index(fields=["id"], name="idx")),
        migrations.AddIndex("item", models.Index(fields=["id"], name="idx")),
        migrations.AddConstraint(
            "item", models.CheckConstraint(condition=Q(id__gt=0), name="c")
        ),
        migrations.AddConstraint(
            "item", models.CheckConstraint(condition=Q(id__gt=0), name="c")
        ),
        """,
        extra="from django.db.models import Q",
    )
    reasons = poisoned(replay(project))
    assert (
        reasons["shop.Item"]
        == ["model name reused without DeleteModel in CreateModel in shop.0002_again"]
        * 2
    )
    name = " ".join(reasons["shop.Item.name"])
    assert "field name reused without RemoveField" in name
    assert "name taken over" in name
    assert "index name reused" in reasons["shop.Item.idx"][0]
    assert "constraint name reused" in reasons["shop.Item.c"][0]


def test_unknown_names(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_unknown",
        """
        migrations.RemoveIndex("item", "nope"),
        migrations.RenameIndex("item", new_name="new", old_name="nope"),
        migrations.RenameIndex("item", new_name="from_fields", old_fields=["name"]),
        migrations.RemoveConstraint("item", "nope"),
        """,
    )
    reasons = poisoned(replay(project))
    assert reasons["shop.Item"] == [
        "unknown index 'nope' in RemoveIndex in shop.0002_unknown",
        "unknown index 'nope' in RenameIndex in shop.0002_unknown",
        "unknown constraint 'nope' in RemoveConstraint in shop.0002_unknown",
    ]
    assert reasons["shop.Item.from_fields"] == [
        "index_together entry not found in RenameIndex in shop.0002_unknown"
    ]


@pytest.mark.parametrize(
    "operation",
    [
        'migrations.RemoveField("item", "nope")',
        'migrations.AlterField("item", "nope", models.IntegerField())',
        'migrations.RenameField("item", "nope", "other")',
    ],
)
def test_unknown_field(project: Project, operation: str) -> None:
    project.migration("shop", "0001_initial", ITEM + operation + ",")
    with pytest.raises(CompactError, match="refers to the field shop.Item.nope"):
        project.compact()


@pytest.mark.skipif(
    not hasattr(migrations, "AlterConstraint"), reason="Django 6.0+ operation"
)
def test_alter_constraint(project: Project) -> None:
    check = "models.CheckConstraint(condition=Q(id__gt=0), name={!r})"
    project.migration(
        "shop",
        "0001_initial",
        ITEM + f'migrations.AddConstraint("item", {check.format("c")}),',
        extra="from django.db.models import Q",
    )
    project.migration(
        "shop",
        "0002_alter",
        f"""
        migrations.AlterConstraint("item", "c", {check.format("renamed")}),
        migrations.AlterConstraint("item", "nope", {check.format("nope")}),
        """,
        extra="from django.db.models import Q",
    )
    timeline = replay(project)
    assert poisoned(timeline)["shop.Item"] == [
        "unknown constraint 'nope' in AlterConstraint in shop.0002_alter"
    ]
    (life,) = lives(timeline, "shop.Item.renamed")
    assert life.names == ["c", "renamed"]


def test_validate_constraint(project: Project) -> None:
    project.migration(
        "shop",
        "0001_initial",
        ITEM
        + """
        migrations.AddConstraint(
            "item", models.CheckConstraint(condition=Q(id__gt=0), name="c")
        ),
        ValidateConstraint("item", "c"),
        ValidateConstraint("item", "nope"),
        """,
        extra="from django.contrib.postgres.operations import ValidateConstraint\n"
        "from django.db.models import Q",
    )
    timeline = replay(project)
    (constraint,) = lives(timeline, "shop.Item.c")
    (model,) = lives(timeline, "shop.Item")
    assert [e.rec.op.name for e in constraint.elements[1:]] == ["c"]
    assert [e.rec.op.name for e in model.elements[1:]] == ["nope"]


def test_together_options(project: Project) -> None:
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
            options={
                "unique_together": [("id", "name"), ("id", "name"), ("id", "nope")],
            },
        ),
        migrations.AlterUniqueTogether(
            "item", [("id", "name"), ("id", "name"), ("name", "nope")]
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterUniqueTogether("item", [("id", "name")]),
                migrations.AlterModelOptions("item", {"ordering": ["name"]}),
                migrations.RenameField("item", "name", "title"),
            ]
        ),
        """,
    )
    timeline = replay(project)
    reasons = poisoned(timeline)
    assert reasons["shop.Item"] == [
        "unique_together names unknown fields in CreateModel in shop.0001_initial",
        "unique_together names unknown fields in AlterUniqueTogether in shop.0001_initial",
        "changed by SeparateDatabaseAndState in shop.0001_initial",
    ]
    (together,) = [life for life in timeline.lives if isinstance(life, TogetherLife)]
    assert together.label == "shop.Item unique_together (id, title)"
    assert together.poison_reasons == [
        "unique_together could not be tracked in AlterUniqueTogether in shop.0001_initial",
        "changed by SeparateDatabaseAndState in shop.0001_initial",
    ]
    assert repr(together) == "<TogetherLife shop.Item unique_together (id, title)>"


def test_model_options(project: Project) -> None:
    project.migration(
        "shop",
        "0001_initial",
        """
        migrations.CreateModel(
            name="Parent",
            fields=[("id", models.AutoField(primary_key=True))],
        ),
        migrations.CreateModel(
            name="Item",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("parent", models.ForeignKey("shop.parent", models.CASCADE)),
                ("legacy", models.IntegerField()),
            ],
            options={"order_with_respect_to": "parent", "ordering": ["legacy"]},
            bases=("shop.parent", "plain.base", models.Model),
        ),
        migrations.AlterModelTable("item", None),
        migrations.RemoveField("item", "legacy"),
        """,
    )
    _plain_app(project)
    timeline = replay(project)
    (item,) = lives(timeline, "shop.Item")
    (parent_field,) = lives(timeline, "shop.Item.parent")
    (legacy,) = lives(timeline, "shop.Item.legacy")
    (parent,) = lives(timeline, "shop.Parent")
    assert parent_field in item.creation.refs
    assert parent in item.creation.refs
    assert parent.child_models == {item}
    assert item.tables == set()
    (soft,) = legacy.soft_refs
    assert soft.end == legacy.end_pos


def _plain_app(project: Project) -> None:
    """An app without migrations, with a model a migrated model extends."""
    app = project.root / project.package / "plain"
    app.mkdir()
    (app / "__init__.py").write_text("")
    (app / "models.py").write_text(
        "from django.db import models\n\n\nclass Base(models.Model):\n    pass\n"
    )
    project.apps.append("plain")


def test_relations(project: Project) -> None:
    project.migration(
        "shop",
        "0001_initial",
        """
        migrations.CreateModel(
            name="Tag",
            fields=[
                ("label", models.CharField(max_length=5)),
                ("code", models.CharField(max_length=5, primary_key=True)),
            ],
        ),
        migrations.CreateModel(
            name="Item",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("tag", models.ForeignKey("shop.tag", models.CASCADE, related_name="items")),
                ("plain", models.ForeignKey("shop.plain", models.CASCADE)),
                ("tags", models.ManyToManyField("shop.tag", db_table="item_tags")),
                (
                    "links",
                    models.ManyToManyField(
                        "shop.tag", through="shop.Link", through_fields=("item", "tag")
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Plain",
            fields=[("name", models.CharField(max_length=5))],
        ),
        migrations.AddField(
            "item", "plain2", models.ForeignKey("shop.plain", models.CASCADE)
        ),
        migrations.CreateModel(
            name="Link",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("item", models.ForeignKey("shop.item", models.CASCADE)),
                ("tag", models.ForeignKey("shop.tag", models.CASCADE)),
            ],
        ),
        migrations.AddField(
            "item",
            "links2",
            models.ManyToManyField(
                "shop.tag", through="shop.Link", through_fields=("item", "tag")
            ),
        ),
        migrations.CreateModel(
            name="Box",
            fields=[("id", models.AutoField(primary_key=True))],
        ),
        migrations.CreateModel(
            name="Crate",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("box", models.ForeignKey("shop.box", models.CASCADE)),
            ],
        ),
        migrations.RenameModel("Box", "Crate2"),
        migrations.RenameModel("Crate2", "Crate"),
        """,
    )
    timeline = replay(project)
    (tag_field,) = lives(timeline, "shop.Item.tag")
    (code,) = lives(timeline, "shop.Tag.code")
    assert code in tag_field.creation.refs
    assert "items" in tag_field.related_names
    (tags,) = lives(timeline, "shop.Item.tags")
    assert "item_tags" in tags.extra_names
    (links2,) = lives(timeline, "shop.Item.links2")
    link_item, link_tag = (
        lives(timeline, "shop.Link.item"),
        lives(timeline, "shop.Link.tag"),
    )
    assert {*link_item, *link_tag} <= set(links2.creation.refs)
    (plain2,) = lives(timeline, "shop.Item.plain2")
    (plain,) = lives(timeline, "shop.Plain")
    assert plain2.creation.refs == [plain]  # but no primary key field
    reasons = poisoned(timeline)
    assert (
        "name taken over in RenameModel in shop.0001_initial" in reasons["shop.Crate"]
    )


# Opaque operations changing the state


ITEM_WITH_OPTIONS = """
migrations.CreateModel(
    name="Item",
    fields=[
        ("id", models.AutoField(primary_key=True)),
        ("name", models.CharField(max_length=10)),
        ("code", models.CharField(max_length=10)),
        ("size", models.IntegerField()),
        ("legacy", models.IntegerField()),
        ("later", models.ForeignKey("shop.later", models.CASCADE, null=True)),
    ],
    options={
        "indexes": [
            models.Index(fields=["code"], name="kept_idx"),
            models.Index(fields=["size"], name="old_idx"),
        ],
        "constraints": [models.CheckConstraint(condition=Q(size__gt=0), name="c")],
        "unique_together": [("name", "code"), ("name", "size")],
    },
),
"""


def test_opaque_state_changes(project: Project) -> None:
    project.migration(
        "shop",
        "0001_initial",
        ITEM_WITH_OPTIONS
        + """
        Change("change_item"),
        Change("rename_case"),
        Change("create"),
        Change("delete"),
        """,
        extra=f"{STATE_OPS}\nfrom django.db.models import Q",
    )
    timeline = replay(project)
    reasons = poisoned(timeline)
    changed = "changed by Change in shop.0001_initial"
    for label in (
        "shop.ITEM",
        "shop.ITEM.legacy",
        "shop.ITEM.name",
        "shop.ITEM.new_idx",
        "shop.ITEM.c",
        "shop.ITEM unique_together (name, code)",
        "shop.ITEM unique_together (name, size)",
    ):
        assert changed in reasons[label], label
    assert reasons["shop.ITEM.extra"] == ["created by Change in shop.0001_initial"]
    assert "shop.ITEM.kept_idx" not in reasons
    (old_idx,) = lives(timeline, "shop.ITEM.old_idx")
    assert old_idx.end is not None
    (item,) = lives(timeline, "shop.ITEM")
    assert item.names == ["Item", "ITEM"]
    (later,) = lives(timeline, "shop.Later")
    (later_field,) = lives(timeline, "shop.ITEM.later")
    assert later in later_field.creation.refs
    assert later.end is not None


def test_opaque_state_inside_run_sql(project: Project) -> None:
    project.migration(
        "shop",
        "0001_initial",
        ITEM
        + """
        migrations.RunSQL(
            "", state_operations=[CreateCollation("c", "und"), Change("rename_case")]
        ),
        """,
        extra=STATE_OPS
        + "\nfrom django.contrib.postgres.operations import CreateCollation",
    )
    (item,) = lives(replay(project), "shop.ITEM")
    assert item.poison_reasons == ["changed by RunSQL in shop.0001_initial"]


def test_opaque_state_failure(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM + 'Change("fail"),', extra=STATE_OPS)
    with pytest.raises(CompactError, match="Could not apply Change"):
        project.compact()


def test_opaque_element_reason(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM + 'migrations.RunSQL("SELECT 1"),')
    timeline = replay(project)
    (element,) = timeline.elements[1]
    assert element.not_editable_reason() == (
        "RunSQL in shop.0001_initial is an opaque operation"
    )
