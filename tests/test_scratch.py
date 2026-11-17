from __future__ import annotations

import os
import signal
import tempfile
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command
from django.db import connections

from django_compact_migrations import trial as trial_module
from django_compact_migrations.scratch import ScratchDatabaseError, scratch_database


def made_temporary_directories(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    made: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(**kwargs: str) -> str:
        made.append(real_mkdtemp(**kwargs))
        return made[-1]

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    return made


def test_cleaned_up_on_interrupt(project, monkeypatch):
    project.migration("shop", "0001_initial", "")
    made = made_temporary_directories(monkeypatch)

    def interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(trial_module, "run_isolated", interrupt)
    project.activate()
    err = StringIO()
    with project.db_blocker.unblock(), pytest.raises(SystemExit) as info:
        call_command("compact_migrations", "plan", stdout=StringIO(), stderr=err)
    assert info.value.code == 130
    assert err.getvalue() == (
        "Interrupted: scratch databases were dropped and no migration file was"
        " changed.\n"
    )
    assert made and not Path(made[0]).exists()
    assert connections["default"].settings_dict["NAME"] == ":memory:"


def test_sigterm_cleans_up(project, monkeypatch):
    project.migration("shop", "0001_initial", "")
    made = made_temporary_directories(monkeypatch)

    def terminate(*args: object, **kwargs: object) -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(trial_module, "run_isolated", terminate)
    with pytest.raises(SystemExit):
        project.command_db("plan")
    assert made and not Path(made[0]).exists()
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def test_unknown_alias():
    with pytest.raises(ScratchDatabaseError, match="no database alias 'nope'"):
        scratch_database("nope").__enter__()


def test_listing(project, tmp_path, monkeypatch):
    project.migration("shop", "0001_initial", "")
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    out, _ = project.command_db("scratch")
    assert out == "No scratch databases found.\n"
    leftover = temp / "compactmig_dead"
    leftover.mkdir()
    out, _ = project.command_db("scratch")
    assert out == f"{leftover}\n"
    with pytest.raises(CommandError, match="is not a scratch"):
        project.command_db("scratch", "--drop", str(tmp_path / "other"))
    out, _ = project.command_db("scratch", "--drop-all")
    assert out == f"Dropped {leftover}.\n"
    assert not leftover.exists()
