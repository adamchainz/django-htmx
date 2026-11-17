from __future__ import annotations

import tomllib

import pytest
from unittest_parametrize import parametrize

from django_compact_migrations.patch import PatchError, apply_diff, make_diff
from django_compact_migrations.planfile import dumps
from tests.project import Base


class PatchTests(Base):
    @parametrize(
        ("old", "new"),
        [
            ("a\nb\nc\n", "a\nB\nc\n"),
            ("a\r\nb\r\nc", "a\r\nb\r\nC"),
            ("x\x0cy\nz\n", "x\x0cy\nZ\n"),
            ("one", "two"),
            ("", "new\n"),
            ("old\n", ""),
            ("a\nb'''\n", "a\nb'''\nc\n"),
            (
                "".join(f"{i}\n" for i in range(30)),
                "".join(f"{i}\n" for i in range(1, 31)),
            ),
        ],
    )
    def test_patch_round_trip(self, old, new):
        diff = make_diff(old, new, "f.py")
        assert apply_diff(old, diff) == new
        data = tomllib.loads(dumps({"diff": diff}))
        assert data["diff"] == diff

    def test_patch_strict(self):
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

    def test_patch_hunks_out_of_order(self):
        old = "".join(f"{i}\n" for i in range(20))
        new = old.replace("1\n", "one\n", 1).replace("18\n", "eighteen\n")
        diff = make_diff(old, new, "f")
        header, first, second = diff.split("@@ -", 2)
        swapped = header + "@@ -" + second + "@@ -" + first
        with pytest.raises(PatchError, match="out of order"):
            apply_diff(old, swapped)
