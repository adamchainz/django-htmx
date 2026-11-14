"""TOML plan files: layout, awkward file contents, and refusing damage."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError

from django_clean_migrations.patch import PatchError, apply_diff, make_diff
from django_clean_migrations.planfile import dumps
from tests.project import Project
from tests.test_clean import ITEM

LEGACY = 'migrations.AddField("item", "legacy", models.IntegerField(default=0)),'
REMOVE = 'migrations.RemoveField("item", "legacy"),'


def history(project: Project) -> Path:
    project.migration("shop", "0001_initial", ITEM)
    path = project.migration(
        "shop",
        "0002_add",
        LEGACY
        + '\nmigrations.AddField("item", "qty", models.IntegerField(default=1)),',
    )
    project.migration("shop", "0003_remove", REMOVE)
    return path


def round_trip(project: Project, tmp_path: Path) -> tuple[dict[str, Any], bytes]:
    plan_path = tmp_path / "plan.toml"
    project.command("plan", "-o", str(plan_path))
    data = tomllib.loads(plan_path.read_text(encoding="utf-8"))
    project.command("apply", "--plan", str(plan_path))
    return data, project.path("shop", "0002_add").read_bytes()


def test_layout(project: Project, tmp_path: Path) -> None:
    history(project)
    plan_path = tmp_path / "plan.toml"
    project.command("plan", "--prune", "--show-blocked", "-o", str(plan_path))
    text = plan_path.read_text()
    order = [
        text.index('format = "django-clean-migrations-plan"'),
        text.index("[summary]"),
        text.index("[options]"),
        text.index("[verification]"),
        text.index("[[removals]]"),
        text.index("[[blocked]]"),
        text.index("[[files]]"),
    ]
    assert order == sorted(order)
    data = tomllib.loads(text)
    (edit,) = [f for f in data["files"] if f["migration"] == ["shop", "0002_add"]]
    assert edit["diff"].startswith("--- a/")
    assert '-        migrations.AddField("item", "legacy"' in edit["diff"]
    assert edit["sha256_before"] != edit["sha256_after"]
    assert "tool_path" in data and "created" in data


def test_blocked_only_when_asked(project: Project, tmp_path: Path) -> None:
    history(project)
    plan_path = tmp_path / "plan.toml"
    project.command("plan", "-o", str(plan_path))
    assert "[[blocked]]" not in plan_path.read_text()


def test_awkward_content(project: Project, tmp_path: Path) -> None:
    path = history(project)
    text = path.read_text()
    # Triple quotes, CRLF endings, a Latin-1 coding cookie and no newline
    # at the end of the file.
    text = (
        "# -*- coding: latin-1 -*-\n# caf\xe9 '''quoted''' \\back\\slash\n"
        + text.rstrip("\n")
    ).replace("\n", "\r\n")
    path.write_bytes(text.encode("latin-1"))
    expected = project.clean().files
    (change,) = [c for c in expected if c.path == path]
    data, written = round_trip(project, tmp_path)
    assert written == change.new.encode("latin-1")
    assert written.startswith(b"# -*- coding: latin-1 -*-\r\n# caf\xe9 '''")
    assert not written.endswith(b"\n")
    assert b"legacy" not in written
    (edit,) = [f for f in data["files"] if f["migration"] == ["shop", "0002_add"]]
    assert edit["encoding"] == "iso-8859-1"


def test_tampered_diff_refused(project: Project, tmp_path: Path) -> None:
    history(project)
    plan_path = tmp_path / "plan.toml"
    project.command("plan", "-o", str(plan_path))
    before = project.files()
    text = plan_path.read_text()
    plan_path.write_text(text.replace("default=1", "default=2", 1))
    with pytest.raises(CommandError, match="doesn't apply|doesn't give the file"):
        project.command("apply", "--plan", str(plan_path))
    # A changed added line still applies, but gives another file.
    plan_path.write_text(text.replace("+    operations = []", "+    operations = [] "))
    with pytest.raises(CommandError, match="doesn't give the file"):
        project.command("apply", "--plan", str(plan_path))
    assert project.files() == before


def test_stale_file_refused(project: Project, tmp_path: Path) -> None:
    path = history(project)
    plan_path = tmp_path / "plan.toml"
    project.command("plan", "-o", str(plan_path))
    path.write_text(path.read_text() + "\n# edited\n")
    before = project.files()
    with pytest.raises(CommandError, match="changed since the plan was made"):
        project.command("apply", "--plan", str(plan_path))
    assert project.files() == before


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("a\nb\nc\n", "a\nB\nc\n"),
        ("a\r\nb\r\nc", "a\r\nb\r\nC"),
        ("x\x0cy\nz\n", "x\x0cy\nZ\n"),
        ("one", "two"),
        ("", "new\n"),
        ("old\n", ""),
        ("a\nb'''\n", "a\nb'''\nc\n"),
        ("".join(f"{i}\n" for i in range(30)), "".join(f"{i}\n" for i in range(1, 31))),
    ],
)
def test_patch_round_trip(old: str, new: str) -> None:
    diff = make_diff(old, new, "f.py")
    assert apply_diff(old, diff) == new
    data = tomllib.loads(dumps({"diff": diff}))
    assert data["diff"] == diff


def test_patch_strict() -> None:
    diff = make_diff("a\nb\nc\n", "a\nB\nc\n", "f.py")
    with pytest.raises(PatchError, match="doesn't match"):
        apply_diff("a\nx\nc\n", diff)
    with pytest.raises(PatchError, match="line counts"):
        apply_diff("a\nb\nc\n", diff.replace(" c\n", ""))
    with pytest.raises(PatchError, match="hunk header"):
        apply_diff("a\nb\nc\n", "--- a\n+++ b\n@@ nonsense\n")
    with pytest.raises(PatchError, match="before the first hunk"):
        apply_diff("a\n", "garbage\n")
    with pytest.raises(PatchError, match="unexpected line in a hunk"):
        apply_diff("a\n", "@@ -1 +1 @@\n?a\n")


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        'quote " and \\ backslash',
        "tab\tand\x01control",
        "multi\nline\nwith ''' quotes\n",
        "multi\nline ending in a quote'",
        "crlf\r\nlines\r\n",
        "multi\nline\x7f\n",
    ],
)
def test_toml_strings(value: str) -> None:
    assert tomllib.loads(dumps({"v": value}))["v"] == value


def test_toml_shapes() -> None:
    data = {
        "n": 1,
        "f": 1.5,
        "b": True,
        "none": None,
        "list": [1, None, "x"],
        "inline": ({"a": 1, "b": None}, {"a": 2}),
        "odd key": "v",
        "t": {"x": 1, "sub": {"y": [2]}},
        "rows": [{"k": "a", "nested": [{"z": 1}]}, {"k": "b"}],
        "empty": [],
    }
    assert tomllib.loads(dumps(data)) == {
        "n": 1,
        "f": 1.5,
        "b": True,
        "list": [1, "x"],
        "inline": [{"a": 1}, {"a": 2}],
        "odd key": "v",
        "t": {"x": 1, "sub": {"y": [2]}},
        "rows": [{"k": "a", "nested": [{"z": 1}]}, {"k": "b"}],
        "empty": [],
    }
