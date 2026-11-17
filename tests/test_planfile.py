"""TOML plan files: layout, awkward file contents, and refusing damage."""

from __future__ import annotations

import subprocess
import tomllib
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError
from unittest_parametrize import parametrize

from django_compact_migrations import history as history_module
from django_compact_migrations import planfile
from django_compact_migrations.core import CompactError, sha256_text
from django_compact_migrations.patch import apply_diff, make_diff
from django_compact_migrations.planfile import dumps
from tests.project import (
    ITEM,
    Project,
    ProjectTestCase,
    with_data_migration,
)

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
    project.command("plan", "--static", "-o", str(plan_path))
    data = tomllib.loads(plan_path.read_text(encoding="utf-8"))
    project.command("apply", "--plan", str(plan_path))
    return data, project.path("shop", "0002_add").read_bytes()


# Plan files are input: they can only edit the project's migrations, and
# only as the analysis would.


def crafted(project: Project, tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    history(project)
    plan_path = tmp_path / "plan.toml"
    project.command("plan", "--static", "-o", str(plan_path))
    return plan_path, tomllib.loads(plan_path.read_text())


def retarget(entry: dict[str, Any], path: Path, new: str | None, base: Path) -> None:
    """Point a plan entry at another file, with consistent hashes and diff."""
    old = path.read_text()
    entry["path"] = str(path)
    entry["sha256_before"] = sha256_text(old)
    if new is not None:
        entry["sha256_after"] = sha256_text(new)
        entry["diff"] = make_diff(old, new, str(path))


def apply_refused(
    project: Project, plan_path: Path, data: dict[str, Any], match: str
) -> None:
    plan_path.write_text(dumps(data))
    before = project.files()
    with pytest.raises(CommandError, match=match):
        project.command("apply", "--plan", str(plan_path))
    assert project.files() == before


class PlanfileTests(ProjectTestCase):
    def test_layout(self):
        history(self.project)
        plan_path = self.tmp_path / "plan.toml"
        self.project.command(
            "plan", "--static", "--prune", "-v", "2", "-o", str(plan_path)
        )
        text = plan_path.read_text()
        order = [
            text.index('format = "django-compact-migrations-plan"'),
            text.index("[summary]"),
            text.index("[options]"),
            text.index("[verification]"),
            text.index("[[removals]]"),
            text.index("[[blocked]]"),
            text.index("[[files]]"),
        ]
        assert order == sorted(order)
        data = tomllib.loads(text)
        # qty folds into the CreateModel; 0002_add is then empty, and pruned
        # (0003_remove is the app's latest migration).
        (edit,) = [
            f for f in data["files"] if f["migration"] == ["shop", "0001_initial"]
        ]
        assert edit["diff"].startswith("--- a/")
        assert (
            "+                ('qty', models.IntegerField(default=1))," in edit["diff"]
        )
        assert edit["sha256_before"] != edit["sha256_after"]
        assert [d["migration"] for d in data["deleted"]] == [["shop", "0002_add"]]
        assert "tool_version" in data and "created" in data
        assert "tool_path" not in data

    def test_blocked_only_when_asked(self):
        history(self.project)
        plan_path = self.tmp_path / "plan.toml"
        self.project.command("plan", "--static", "-o", str(plan_path))
        assert "[[blocked]]" not in plan_path.read_text()

    def test_awkward_content(self):
        path = history(self.project)
        text = path.read_text()
        # Triple quotes, CRLF endings, a Latin-1 coding cookie and no newline
        # at the end of the file.
        text = (
            "# -*- coding: latin-1 -*-\n# calf\xe9 '''quoted''' \\back\\slash\n"
            + text.rstrip("\n")
        ).replace("\n", "\r\n")
        path.write_bytes(text.encode("latin-1"))
        expected = self.project.compact().files
        (change,) = [c for c in expected if c.path == path]
        data, written = round_trip(self.project, self.tmp_path)
        assert written == change.new.encode("latin-1")
        assert written.startswith(b"# -*- coding: latin-1 -*-\r\n# calf\xe9 '''")
        assert not written.endswith(b"\n")
        assert b"legacy" not in written
        (edit,) = [f for f in data["files"] if f["migration"] == ["shop", "0002_add"]]
        assert edit["encoding"] == "iso-8859-1"

    def test_tampered_diff_refused(self):
        history(self.project)
        plan_path = self.tmp_path / "plan.toml"
        self.project.command("plan", "--static", "-o", str(plan_path))
        before = self.project.files()
        text = plan_path.read_text()
        # A changed removed line doesn't apply.
        tampered = text.replace("-        migrations.AddField(", "-        x(", 1)
        assert tampered != text
        plan_path.write_text(tampered)
        with pytest.raises(CommandError, match="doesn't apply"):
            self.project.command("apply", "--plan", str(plan_path))
        # A changed added line still applies, but gives another file.
        plan_path.write_text(
            text.replace("+    operations = []", "+    operations = [] ")
        )
        with pytest.raises(CommandError, match="doesn't give the file"):
            self.project.command("apply", "--plan", str(plan_path))
        assert self.project.files() == before

    def test_stale_file_refused(self):
        path = history(self.project)
        plan_path = self.tmp_path / "plan.toml"
        self.project.command("plan", "--static", "-o", str(plan_path))
        path.write_text(path.read_text() + "\n# edited\n")
        before = self.project.files()
        with pytest.raises(CommandError, match="changed since the plan was made"):
            self.project.command("apply", "--plan", str(plan_path))
        assert self.project.files() == before

    @parametrize(
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
    def test_toml_strings(self, value):
        assert tomllib.loads(dumps({"v": value}))["v"] == value

    def test_toml_shapes(self):
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

    def test_tool_version_not_installed(self):

        def missing(name: str) -> str:
            raise PackageNotFoundError(name)

        self.monkeypatch.setattr(planfile, "version", missing)
        assert planfile.tool_version() == "unknown"

    def test_plan_file_with_missing_migration(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        plan_path = self.tmp_path / "plan.toml"
        self.project.command("plan", "--static", "-o", str(plan_path))
        self.project.path("shop", "0002_add").unlink()
        self.project.purge()
        with pytest.raises(CompactError, match="Could not load migrations"):
            planfile.load_plan(plan_path)

    def test_crafted_plan_cant_edit_other_files(self):
        plan_path, data = crafted(self.project, self.tmp_path)
        settings_file = self.project.root / "settings_like.py"
        settings_file.write_text("DEBUG = False\n")
        entry = data["files"][0]
        retarget(entry, settings_file, "DEBUG = False\nimport os\n", self.tmp_path)
        # A module that isn't the migration's.
        entry["module"] = "settings_like"
        apply_refused(self.project, plan_path, data, "module of shop.0001_initial")
        assert settings_file.read_text() == "DEBUG = False\n"
        # The right module, but a path elsewhere: the path is never used.
        entry["module"] = f"{self.project.package}.shop.migrations.0001_initial"
        apply_refused(self.project, plan_path, data, "changed since the plan was made")
        assert settings_file.read_text() == "DEBUG = False\n"

    def test_crafted_plan_cant_delete_other_files(self):
        plan_path, data = crafted(self.project, self.tmp_path)
        victim = self.tmp_path / "victim.txt"
        victim.write_text("keep me")
        entry = {
            "migration": ["shop", "9999_nothing"],
            "module": "no.such.module",
            "path": str(victim),
        }
        retarget(entry, victim, None, self.tmp_path)
        data["deleted"] = [entry]
        apply_refused(
            self.project, plan_path, data, "isn't a migration of this project"
        )
        assert victim.read_text() == "keep me"
        # Named twice.
        data["deleted"] = [data["files"][0] | {}]
        data["deleted"][0].pop("diff")
        apply_refused(self.project, plan_path, data, "names shop.0001_initial twice")

    def test_plan_cant_edit_read_only_apps(self):
        plan_path, data = crafted(self.project, self.tmp_path)
        self.monkeypatch.setattr(
            history_module,
            "read_only_reason",
            lambda path, root, targeted: "installed package",
        )
        apply_refused(
            self.project, plan_path, data, r"not editable \(installed package\)"
        )

    def test_static_plan_recomputed(self):
        plan_path, data = crafted(self.project, self.tmp_path)
        # A consistent edit (hashes and diff match) that the analysis doesn't make.
        (entry,) = [f for f in data["files"] if f["migration"][1] == "0002_add"]
        path = self.project.path("shop", "0002_add")
        retarget(entry, path, path.read_text() + "\n# injected\n", self.tmp_path)
        apply_refused(self.project, plan_path, data, "aren't the ones its options give")
        # Made with other backends configured.
        data = tomllib.loads(plan_path.read_text())
        data["options"]["backends"] = ["mysql"]
        (entry,) = [f for f in data["files"] if f["migration"][1] == "0002_add"]
        retarget(entry, path, path.read_text() + "\n# injected\n", self.tmp_path)
        apply_refused(
            self.project, plan_path, data, "other database backends configured"
        )

    def test_trial_plan_verified_after_writing(self):
        history(self.project)
        plan_path = self.tmp_path / "plan.toml"
        self.project.command_db("plan", "-o", str(plan_path))
        data = tomllib.loads(plan_path.read_text())
        assert data["verification"]["mode"] == "trial"
        # The same final state and operations, but the fields in another order:
        # only the in-memory check after writing sees it.
        (entry,) = [f for f in data["files"] if f["migration"][1] == "0001_initial"]
        path = self.project.path("shop", "0001_initial")
        rewritten = apply_diff(path.read_text(), entry["diff"])
        name = "                ('name', models.CharField(max_length=10)),\n"
        swapped = rewritten.replace(name, "").replace(
            "            ],\n", name + "            ],\n", 1
        )
        assert swapped != rewritten
        retarget(entry, path, swapped, self.tmp_path)
        apply_refused(
            self.project, plan_path, data, "in another order.*may have been edited"
        )

    def test_recorded_verification_labelled(self):
        with_data_migration(self.project, "pass")
        plan_path = self.tmp_path / "plan.toml"
        self.project.command("plan", "-o", str(plan_path))
        out, _ = self.project.command("apply", "--plan", str(plan_path))
        assert "Trial (recorded in the plan): " in out

    def test_apply_plan_from_elsewhere(self):
        git = [
            "git",
            "-C",
            str(self.tmp_path),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
        ]
        subprocess.run([*git, "init", "-q"], check=True)
        with_data_migration(self.project, "pass")
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "init"], check=True)
        plans = self.tmp_path / "plans"
        plans.mkdir()
        self.monkeypatch.chdir(plans)
        self.project.command("plan", "--static", "-o", "plan.toml")
        data = tomllib.loads((plans / "plan.toml").read_text())
        assert all(f["path"].startswith("../") for f in data["files"])
        elsewhere = self.tmp_path / "elsewhere"
        elsewhere.mkdir()
        self.monkeypatch.chdir(elsewhere)
        self.project.command("apply", "--plan", str(plans / "plan.toml"))
        assert 'AddField("item", "legacy"' not in self.project.read("shop", "0002_add")
