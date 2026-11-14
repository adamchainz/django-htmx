from __future__ import annotations

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


def is_exact_run_op(op: Operation) -> bool:
    return type(op) is m.RunPython or type(op) is m.RunSQL
