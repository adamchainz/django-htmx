"""
An operation the tool doesn't know (so an opaque one), changing the project
state in ways the timeline has to follow.
"""

from __future__ import annotations

from collections.abc import Callable

from django.db import models
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ModelState, ProjectState


def _change_item(state: ProjectState, app_label: str) -> None:
    ms = state.models[app_label, "item"]
    ms.fields.pop("legacy")
    ms.fields["extra"] = models.ForeignKey("shop.item", models.CASCADE, null=True)
    ms.fields["name"] = models.CharField(max_length=20)
    kept, _ = ms.options["indexes"]
    ms.options["indexes"] = [kept, models.Index(fields=["name"], name="new_idx")]
    ms.options["constraints"] = [
        models.CheckConstraint(condition=models.Q(size__gt=1), name="c")
    ]
    ms.options["unique_together"] = {("name", "code"), ("name", "nope")}
    ms.options["ordering"] = ["name"]


def _rename_case(state: ProjectState, app_label: str) -> None:
    state.models[app_label, "item"].name = "ITEM"


def _create(state: ProjectState, app_label: str) -> None:
    state.add_model(
        ModelState(
            app_label,
            "Later",
            [
                ("id", models.AutoField(primary_key=True)),
                ("parent", models.ForeignKey("shop.later", models.CASCADE)),
            ],
            options={"unique_together": [("id",)]},
        )
    )


def _delete(state: ProjectState, app_label: str) -> None:
    state.remove_model(app_label, "later")


def _fail(state: ProjectState, app_label: str) -> None:
    raise ValueError("no such state")


CHANGES: dict[str, Callable[[ProjectState, str], None]] = {
    "change_item": _change_item,
    "rename_case": _rename_case,
    "create": _create,
    "delete": _delete,
    "fail": _fail,
}


class Change(Operation):
    def __init__(self, change: str) -> None:
        self.change = change

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        CHANGES[self.change](state, app_label)
