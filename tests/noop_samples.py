"""
Data migration functions whose source the no-op scan reads; they are never
run (so this module is left out of coverage).
"""

from __future__ import annotations

from typing import Any


def _registers(apps: Any, schema_editor: Any) -> None:
    from django.db.models.signals import post_migrate

    post_migrate.connect(lambda **kwargs: None)


def _vendor(apps: Any, schema_editor: Any) -> None:
    if schema_editor.connection.vendor == "postgresql":
        pass


def _sequences(apps: Any, schema_editor: Any) -> None:
    schema_editor.connection.ops.sequence_reset_sql(None, [])


def _files(apps: Any, schema_editor: Any) -> None:
    open("/dev/null").close()


def _helper() -> None:
    helper_called()


def helper_called() -> None:
    pass


def _local_cache(apps: Any, schema_editor: Any) -> None:
    cache = {}
    for item in apps.get_model("shop", "Item").objects.all():
        cache[item.pk] = item


def _quiet(apps: Any, schema_editor: Any) -> None:
    apps.get_model("shop", "Item")


def _calls_something(apps: Any, schema_editor: Any) -> None:
    _helper()


def _loop(apps: Any, schema_editor: Any) -> None:
    for item in apps.get_model("shop", "Item").objects.all():
        item.save()


def _calls_subscript(apps: Any, schema_editor: Any) -> None:
    [print][0]("x")


def _imports_json(apps: Any, schema_editor: Any) -> None:
    import email.utils  # noqa: F401
    import json  # noqa: F401


exec("def _no_source(): pass", globals())


def _calls_unreadable(apps: Any, schema_editor: Any) -> None:
    _no_source()  # type: ignore[name-defined]  # noqa: F821
