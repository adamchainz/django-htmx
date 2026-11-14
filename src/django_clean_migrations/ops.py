from __future__ import annotations

import copy

from django.db.migrations import operations as m
from django.db.migrations.operations.base import Operation

POSTGRES_MODULE = "django.contrib.postgres.operations"

# Exact types only: a subclass may do anything in state_forwards / database_*.
_KNOWN: dict[type[Operation], str] = {
    cls: cls.__name__
    for cls in (
        m.CreateModel,
        m.DeleteModel,
        m.RenameModel,
        m.AlterModelTable,
        m.AlterModelTableComment,
        m.AlterModelOptions,
        m.AlterModelManagers,
        m.AlterUniqueTogether,
        m.AlterIndexTogether,
        m.AlterOrderWithRespectTo,
        m.AddField,
        m.RemoveField,
        m.AlterField,
        m.RenameField,
        m.AddIndex,
        m.RemoveIndex,
        m.RenameIndex,
        m.AddConstraint,
        m.RemoveConstraint,
        m.AlterConstraint,
    )
}

_POSTGRES_EQUIVALENTS = {
    "AddIndexConcurrently": "AddIndex",
    "RemoveIndexConcurrently": "RemoveIndex",
    "AddConstraintNotValid": "AddConstraint",
    "ValidateConstraint": "ValidateConstraint",
    "CreateCollation": "Inert",
    "RemoveCollation": "Inert",
}

OPAQUE = "Opaque"
INERT = "Inert"


def classify(op: Operation) -> str:
    """
    Canonical name of a known operation, "Inert" for operations that never
    touch model state, or "Opaque".
    """
    cls = type(op)
    known = _KNOWN.get(cls)
    if known is not None:
        return known
    if cls.__module__ == POSTGRES_MODULE:
        if cls.__name__ in _POSTGRES_EQUIVALENTS:
            return _POSTGRES_EQUIVALENTS[cls.__name__]
        if any(
            c.__name__ == "CreateExtension" and c.__module__ == POSTGRES_MODULE
            for c in cls.__mro__
        ):
            return INERT
    return OPAQUE


def isolated(op: Operation) -> Operation:
    """
    A copy of ``op`` with its own field objects, for replaying state.

    ProjectState.rename_field() rewrites to_fields/from_fields/field_name of
    the field objects held in the state in place, and those are the
    operations' own objects: replaying a history twice (fold rounds, trial
    runs) would otherwise see the renamed values where the original names
    belong.
    """
    cls = type(op)
    if cls is m.AddField or cls is m.AlterField:
        copied = copy.copy(op)
        copied.field = op.field.clone()  # type: ignore[attr-defined]
        return copied
    if cls is m.CreateModel:
        copied = copy.copy(op)
        copied.fields = [(n, f.clone()) for n, f in op.fields]  # type: ignore[attr-defined]
        return copied
    return op
