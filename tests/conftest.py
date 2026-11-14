from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.project import Project


@pytest.fixture(autouse=True)
def no_bytecode(monkeypatch: pytest.MonkeyPatch) -> None:
    # Files are rewritten within the same second as they are first imported.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


@pytest.fixture
def project(tmp_path: Path, django_db_blocker: Any) -> Iterator[Project]:
    p = Project(tmp_path, django_db_blocker)
    try:
        yield p
    finally:
        p.close()
