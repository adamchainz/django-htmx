from __future__ import annotations

from typing import Any

import pytest
from django.conf import settings
from django.db import connections
from django.db.migrations.loader import MigrationLoader
from django.db.models.signals import post_migrate, pre_migrate
from django.test import override_settings

from django_compact_migrations.runner import RunError, run_history
from django_compact_migrations.scratch import scratch_database
from tests.project import ITEM, ProjectTestCase, shop_history


class RunnerTests(ProjectTestCase):
    def test_hooks_removed(self):
        history = shop_history(self.project)
        history.activate()
        history.purge()
        hooked: list[tuple[str, str]] = []

        def hook(key: Any, index: int, op: Any, forwards: Any) -> Any:
            hooked.append(key)
            return forwards

        with history.db_blocker.unblock(), scratch_database() as connection:
            run_history(connection, hook=hook)
        assert len(hooked) == 8
        loader = MigrationLoader(None, ignore_no_migrations=True)
        for migration in loader.graph.nodes.values():
            for op in migration.operations:
                assert "database_forwards" not in op.__dict__

    def test_run_history_needs_empty_database(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.activate()
        with self.project.db_blocker.unblock(), scratch_database() as connection:
            run_history(connection)
            with pytest.raises(RunError, match="already has applied migrations"):
                run_history(connection)

    def test_shared_operation_hooked_once(self):
        self.project.migration("shop", "0001_initial", ITEM)
        (self.project.path("shop").parent / "shared.py").write_text(
            "from django.db import migrations\n\n"
            "NOTHING = migrations.RunPython(migrations.RunPython.noop)\n"
        )
        extra = f"from {self.project.package}.shop.shared import NOTHING"
        self.project.migration("shop", "0002_a", "NOTHING,", extra=extra)
        self.project.migration("shop", "0003_b", "NOTHING,", extra=extra)
        self.project.activate()
        hooked: list[str] = []

        def hook(key: Any, index: int, op: Any, forwards: Any) -> Any:
            hooked.append(f"{key[1]} {type(op).__name__}")
            return forwards

        with self.project.db_blocker.unblock(), scratch_database() as connection:
            run_history(connection, hook=hook)
        assert hooked == ["0001_initial CreateModel", "0002_a RunPython"]

    def test_signal_handler_failures(self):
        self.project.migration("shop", "0001_initial", ITEM)
        (self.project.path("shop").parent / "models.py").write_text("")
        self.project.activate()

        def fail(**kwargs: Any) -> None:
            raise ValueError("handler")

        for signal_, name in (
            (pre_migrate, "pre_migrate"),
            (post_migrate, "post_migrate"),
        ):
            signal_.connect(fail, dispatch_uid="test_signal_handler_failures")
            try:
                with (
                    self.project.db_blocker.unblock(),
                    scratch_database() as connection,
                    pytest.raises(RunError, match=f"A {name} signal handler failed"),
                ):
                    run_history(connection)
            finally:
                signal_.disconnect(dispatch_uid="test_signal_handler_failures")
        assert connections["default"].vendor == "sqlite"

    def test_post_migrate_models_in_state_order(self):
        # A reloaded model moves to the end of the delayed apps cache;
        # migrate clears it, so post_migrate handlers (create_permissions
        # numbers permissions in this order) see the state's order.
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel("A", [("id", models.AutoField(primary_key=True)), ("x", models.IntegerField())]),
            migrations.CreateModel("B", [("id", models.AutoField(primary_key=True))]),
            migrations.AlterField("a", "x", models.BigIntegerField()),
            """,
        )
        (self.project.path("shop").parent / "models.py").write_text("")
        # An app without migrations: migrate renders its models again.
        plain = self.project.root / self.project.package / "plain"
        plain.mkdir()
        (plain / "__init__.py").write_text("")
        (plain / "models.py").write_text(
            "from django.db import models\n\n\nclass Thing(models.Model):\n    pass\n"
        )
        self.project.activate()
        self.enterContext(
            override_settings(
                INSTALLED_APPS=[
                    *settings.INSTALLED_APPS,
                    f"{self.project.package}.plain",
                ]
            )
        )
        seen: list[list[str]] = []

        def record(sender: Any, apps: Any, **kwargs: Any) -> None:
            if sender.label == "shop":
                seen.append(
                    [m.__name__ for m in apps.get_app_config("shop").get_models()]
                )

        post_migrate.connect(record, dispatch_uid="test_post_migrate_models")
        try:
            with scratch_database() as connection:
                run_history(connection)
        finally:
            post_migrate.disconnect(dispatch_uid="test_post_migrate_models")
        assert seen == [["A", "B"]]
