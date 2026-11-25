from __future__ import annotations

import os
import signal
import tempfile
import threading
from io import StringIO
from pathlib import Path
from typing import Any

import psycopg
import pytest
from django.core.management import call_command
from django.db import connections
from django.db.utils import ConnectionHandler

from django_compact_migrations import scratch as scratch_module
from django_compact_migrations import trial as trial_module
from django_compact_migrations.scratch import (
    ScratchDatabaseError,
    scratch_database,
)
from tests.project import SERVERS, ProjectTestCase


def made_temporary_directories(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    made: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(**kwargs: str) -> str:
        made.append(real_mkdtemp(**kwargs))
        return made[-1]

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    return made


def every_alias_retargeted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sqlite = "django.db.backends.sqlite3"
    primary, other = str(tmp_path / "primary.db"), str(tmp_path / "other.db")
    handler = ConnectionHandler(
        {
            "default": {"ENGINE": sqlite, "NAME": primary},
            "replica": {"ENGINE": sqlite, "NAME": primary},
            "mirror": {"ENGINE": sqlite, "NAME": other, "TEST": {"MIRROR": "default"}},
            "other": {"ENGINE": sqlite, "NAME": other},
            "nowhere": {"ENGINE": "django.db.backends.dummy"},
        }
    )
    monkeypatch.setattr(scratch_module, "connections", handler)
    with scratch_module.scratch_database("default") as connection:
        names = {alias: handler[alias].settings_dict["NAME"] for alias in handler}
        assert connection.settings_dict["NAME"] == names["default"]
        # The replica and the mirror see the primary's scratch database.
        assert names["replica"] == names["mirror"] == names["default"]
        # Another database gets its own, empty one.
        assert names["other"] not in (other, names["default"])
        assert primary not in names.values()
        with handler["replica"].cursor() as cursor:
            cursor.execute("CREATE TABLE t (x int)")
        with handler["default"].cursor() as cursor:
            cursor.execute("SELECT * FROM t")
    assert handler["default"].settings_dict["NAME"] == primary
    assert handler["other"].settings_dict["NAME"] == other
    assert handler["replica"].connection is None
    assert not Path(names["default"]).exists()
    assert not Path(names["other"]).exists()
    assert not Path(primary).exists()


class ScratchTests(ProjectTestCase):
    def test_cleaned_up_on_interrupt(self):
        self.project.migration("shop", "0001_initial", "")
        made = made_temporary_directories(self.monkeypatch)

        def interrupt(*args: object, **kwargs: object) -> None:
            raise KeyboardInterrupt

        self.monkeypatch.setattr(trial_module, "run_isolated", interrupt)
        self.project.activate()
        err = StringIO()
        with self.project.db_blocker.unblock(), pytest.raises(SystemExit) as info:
            call_command("compact_migrations", "plan", stdout=StringIO(), stderr=err)
        assert info.value.code == 130
        assert err.getvalue() == (
            "Interrupted: scratch databases were dropped and no migration file was"
            " changed.\n"
        )
        assert made and not Path(made[0]).exists()
        assert connections["default"].settings_dict["NAME"] == ":memory:"

    def test_sigterm_cleans_up(self):
        self.project.migration("shop", "0001_initial", "")
        made = made_temporary_directories(self.monkeypatch)

        def terminate(*args: object, **kwargs: object) -> None:
            os.kill(os.getpid(), signal.SIGTERM)

        self.monkeypatch.setattr(trial_module, "run_isolated", terminate)
        with pytest.raises(SystemExit):
            self.project.command_db("plan")
        assert made and not Path(made[0]).exists()
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL

    def test_unknown_alias(self):
        with pytest.raises(ScratchDatabaseError, match="no database alias 'nope'"):
            scratch_database("nope").__enter__()

    def test_every_alias_retargeted(self):
        with self.db_blocker.unblock():
            every_alias_retargeted(self.tmp_path, self.monkeypatch)

    def test_unsupported_alias_refused(self):
        handler = ConnectionHandler(
            {
                "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "x"},
                "odd": {"ENGINE": "django.db.backends.sqlite3", "NAME": "y"},
            }
        )
        handler["odd"].vendor = "oracle"
        self.monkeypatch.setattr(scratch_module, "connections", handler)
        with pytest.raises(scratch_module.ScratchDatabaseError, match="'odd'"):
            scratch_module.scratch_database("default").__enter__()

    def test_dummy_alias_refused(self):

        handler = ConnectionHandler({"default": {"ENGINE": "django.db.backends.dummy"}})
        self.monkeypatch.setattr(scratch_module, "connections", handler)
        with pytest.raises(ScratchDatabaseError, match="dummy backend"):
            scratch_database("default").__enter__()

    def test_postgres_drop_forced(self):
        pg = self.server("pg")

        config = SERVERS[pg]
        with scratch_database(pg) as connection:
            name = connection.settings_dict["NAME"]
            # A connection the command doesn't know about: plain DROP DATABASE
            # fails, WITH (FORCE) ends it.
            other = psycopg.connect(
                host=config["HOST"],
                port=config["PORT"],
                user=config["USER"],
                password=config["PASSWORD"],
                dbname=name,
            )
        with pytest.raises(psycopg.OperationalError):
            other.execute("SELECT 1")
        other.close()
        creation: Any = connections[pg].creation
        with creation._nodb_cursor() as cursor:
            cursor.execute("SELECT datname FROM pg_database")
            assert name not in [row[0] for row in cursor.fetchall()]

    def test_postgres_leftover_replaced(self):
        pg = self.server("pg")
        real = connections[pg].settings_dict["NAME"]
        name = f"compactmig_{real}"
        scratch_module._create(connections[pg], name)
        # What a killed run left behind doesn't stop the next one.
        with scratch_database(pg) as connection:
            assert connection.settings_dict["NAME"] == name
        creation: Any = connections[pg].creation
        with creation._nodb_cursor() as cursor:
            cursor.execute("SELECT datname FROM pg_database")
            assert name not in [row[0] for row in cursor.fetchall()]

    def test_postgres_scratch_name_taken(self):
        pg = self.server("pg")
        self.monkeypatch.setattr(
            scratch_module, "_scratch_name", lambda name, length: name
        )
        with pytest.raises(ScratchDatabaseError, match="it is a real database's name"):
            scratch_database(pg).__enter__()

    def test_mirror_errors(self):

        sqlite = "django.db.backends.sqlite3"
        handler = ConnectionHandler(
            {
                "default": {"ENGINE": sqlite, "NAME": "a"},
                "broken": {
                    "ENGINE": sqlite,
                    "NAME": "b",
                    "TEST": {"MIRROR": "nowhere"},
                },
            }
        )
        self.monkeypatch.setattr(scratch_module, "connections", handler)
        with pytest.raises(ScratchDatabaseError, match="mirrors 'nowhere'"):
            scratch_module._groups("default")
        handler = ConnectionHandler(
            {
                "default": {"ENGINE": sqlite, "NAME": "a"},
                "m1": {"ENGINE": sqlite, "NAME": "b", "TEST": {"MIRROR": "m2"}},
                "m2": {"ENGINE": sqlite, "NAME": "c", "TEST": {"MIRROR": "default"}},
            }
        )
        self.monkeypatch.setattr(scratch_module, "connections", handler)
        (group,) = scratch_module._groups("default")
        assert sorted(group.aliases) == ["default", "m1", "m2"]

    def test_signals_outside_main_thread(self):
        restores: list[Any] = []
        thread = threading.Thread(
            target=lambda: restores.append(scratch_module._exit_on_termination())
        )
        thread.start()
        thread.join()
        restores[0]()

        self.monkeypatch.delattr(signal, "SIGHUP")
        scratch_module._exit_on_termination()()

    def test_drop_failure_reported(self):
        class Creation:
            def sql_table_creation_suffix(self) -> str:
                return ""

            def _nodb_cursor(self) -> Any:
                raise OSError("no server")

        class Connection:
            vendor = "mysql"
            creation = Creation()

            class ops:  # noqa: N801
                @staticmethod
                def quote_name(name: str) -> str:
                    return name

        with pytest.raises(ScratchDatabaseError, match="please drop it by hand"):
            scratch_module._drop(Connection(), "compactmig_x")
        with pytest.raises(ScratchDatabaseError, match="Could not create"):
            scratch_module._create(Connection(), "compactmig_x")

    def test_postgres_drop_failure_reported(self):
        pg = self.server("pg")
        real = scratch_module._drop

        def drop(connection: Any, name: str) -> None:
            real(connection, name)
            raise ScratchDatabaseError(f"Could not drop the scratch database {name!r}")

        self.monkeypatch.setattr(scratch_module, "_drop", drop)
        with pytest.raises(ScratchDatabaseError, match="Could not drop"):  # noqa: SIM117
            with scratch_database(pg):
                assert connections[pg].vendor == "postgresql"
