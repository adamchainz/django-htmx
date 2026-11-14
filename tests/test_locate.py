"""Finding the parts of CreateModel and Alter*Together calls in the source."""

from __future__ import annotations

import pytest
from django.db import models
from django.db.migrations.operations import AlterUniqueTogether, CreateModel
from django.db.migrations.operations.base import Operation

from django_clean_migrations.history import load_history
from django_clean_migrations.locate import (
    CreateModelLoc,
    TogetherLoc,
    locate_create_model,
    locate_together_op,
    normalize_together,
)
from tests.project import Project

ID = '("id", models.AutoField(primary_key=True))'


def locate(project: Project, operation: str, extra: str = "") -> object:
    project.migration("shop", "0001_initial", operation + ",", extra=extra)
    project.activate()
    with project.db_blocker.unblock():
        rec = load_history().ops[-1]
    if rec.name == "CreateModel":
        return locate_create_model(rec)
    return locate_together_op(rec)


@pytest.mark.parametrize(
    ("operation", "extra", "problem"),
    [
        (
            "migrations.CreateModel(**KWARGS)",
            f'KWARGS = {{"name": "Item", "fields": [{ID}]}}',
            "call uses * or ** arguments",
        ),
        (
            'migrations.CreateModel("Item", [FIELD])',
            f"FIELD = {ID}",
            "fields entry is not a 2-tuple",
        ),
        (
            'migrations.CreateModel("Item", [(NAME, models.AutoField(primary_key=True))])',
            'NAME = "id"',
            "field name is not a string literal",
        ),
        (
            f'migrations.CreateModel("Item", [{ID}], {{KEY: []}})',
            'KEY = "ordering"',
            "options keys are not string literals",
        ),
        (
            f'migrations.CreateModel("Item", [{ID}], {{"indexes": INDEXES}})',
            "INDEXES = []",
            "indexes option is not a matching literal",
        ),
        (
            f'migrations.CreateModel("Item", [{ID}], {{"unique_together": {{"id", "pk"}}}})',
            "",
            "together value mismatch",
        ),
        (
            f'migrations.CreateModel("Item", [{ID}], {{"unique_together": [("id",), ("id",)]}})',
            "",
            "together entry is not a literal",
        ),
        (
            'migrations.AlterUniqueTogether("item", [ENTRY])',
            'ENTRY = ("id",)',
            "together entry is not a literal",
        ),
        (
            'migrations.AlterUniqueTogether("item", [("id", 1)])',
            "",
            "together entry is not a literal",
        ),
        (
            f'migrations.CreateModel("Item", [{ID}], {{"ordering": lambda a, b: 0}})',
            "",
            "unexpected source layout",
        ),
    ],
)
def test_not_locatable(
    project: Project, operation: str, extra: str, problem: str
) -> None:
    project.migration("shop", "0000_item", f"migrations.CreateModel('Item', [{ID}]),")
    result = locate(project, operation, extra)
    assert isinstance(result, str)
    assert result.startswith(problem)


def test_options(project: Project) -> None:
    loc = locate(
        project,
        f"""migrations.CreateModel(
            "Item",
            [{ID}],
            options={{
                "ordering": ["id"],
                "unique_together": ("id", "id"),
                "index_together": set(),
                "indexes": [models.Index(fields=["id"], name="i")],
            }},
        )""",
    )
    assert isinstance(loc, CreateModelLoc)
    assert loc.option_index == {
        "ordering": 0,
        "unique_together": 1,
        "index_together": 2,
        "indexes": 3,
    }
    assert loc.together["unique_together"].flat
    assert loc.together["index_together"] == TogetherLoc(None, False, {})


def test_no_options(project: Project) -> None:
    loc = locate(project, f'migrations.CreateModel("Item", [{ID}], options=None)')
    assert isinstance(loc, CreateModelLoc)
    assert loc.options is None


def test_normalize_flat() -> None:
    assert normalize_together(("a", "b")) == [("a", "b")]


# Operations an earlier round edited no longer match their source


def edited(project: Project, operation: str, new: Operation) -> object:
    project.migration("shop", "0000_item", f"migrations.CreateModel('Item', [{ID}]),")
    project.migration("shop", "0001_initial", operation + ",")
    project.activate()
    with project.db_blocker.unblock():
        rec = load_history().ops[-1]
    rec.op = new
    if rec.name == "CreateModel":
        return locate_create_model(rec)
    return locate_together_op(rec)


@pytest.mark.parametrize(
    ("source", "options", "problem"),
    [
        ("", {"ordering": ["id"]}, "options not found"),
        (", options=None", {"unique_together": {("id",)}}, "options not found"),
        (
            ', {"unique_together": None}',
            {"unique_together": {("id",)}},
            "together value not found",
        ),
        (
            ', {"unique_together": set()}',
            {"unique_together": {("id",)}},
            "together value mismatch",
        ),
        (
            ', {"unique_together": [("id",)]}',
            {"unique_together": [("pk",)]},
            "together value mismatch",
        ),
    ],
)
def test_edited_create_model(
    project: Project, source: str, options: dict[str, object], problem: str
) -> None:
    new = CreateModel("Item", [("id", models.AutoField(primary_key=True))], options)
    result = edited(project, f'migrations.CreateModel("Item", [{ID}]{source})', new)
    assert isinstance(result, str) and result.startswith(problem)


def test_edited_together_operation(project: Project) -> None:
    result = edited(
        project,
        'migrations.AlterUniqueTogether("item", {("id",)})',
        AlterUniqueTogether("item", set()),
    )
    assert isinstance(result, str) and result.startswith("together value mismatch")


def test_together_none(project: Project) -> None:
    loc = locate(
        project, f'migrations.CreateModel("Item", [{ID}], {{"unique_together": None}})'
    )
    assert isinstance(loc, CreateModelLoc)
    assert loc.together["unique_together"] == TogetherLoc(None, False, {})
