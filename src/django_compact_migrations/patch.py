"""
Unified diffs of migration files for plan files, and a strict patcher.

The diff is made from the exact text (line endings kept), with the usual
"\\ No newline at end of file" marker. Applying it requires every context
and removed line to match exactly where the hunk says; there is no fuzz and
no searching. The caller also checks the hash of the result.
"""

from __future__ import annotations

import difflib
import re

NO_NEWLINE = "\\ No newline at end of file"
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    pass


def make_diff(old: str, new: str, name: str, context: int = 3) -> str:
    """A unified diff from ``old`` to ``new`` (exact line endings kept)."""
    out: list[str] = []
    for index, line in enumerate(
        difflib.unified_diff(
            _lines(old), _lines(new), f"a/{name}", f"b/{name}", n=context
        )
    ):
        if index < 2 or line.startswith("@@"):
            out.append(line.rstrip("\n") + "\n")
            continue
        out.append(line)
        if not line.endswith("\n"):
            out.append("\n" + NO_NEWLINE + "\n")
    return "".join(out)


def apply_diff(old: str, diff: str) -> str:
    """Apply a diff made by make_diff() to exactly the text it was made from."""
    source = _lines(old)
    result: list[str] = []
    position = 0
    lines = _lines(diff)
    i = 0
    while i < len(lines) and not lines[i].startswith("@@"):
        if not lines[i].startswith(("---", "+++")):
            raise PatchError(f"unexpected line before the first hunk: {lines[i]!r}")
        i += 1
    while i < len(lines):
        match = _HUNK.match(lines[i])
        if match is None:
            raise PatchError(f"expected a hunk header, found {lines[i]!r}")
        start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_count = int(match.group(4) or "1")
        begin = start - 1 if old_count else start
        if begin < position or begin > len(source):
            raise PatchError("hunks out of order or out of range")
        result.extend(source[position:begin])
        position = begin
        seen_old = seen_new = 0
        i += 1
        while i < len(lines) and not lines[i].startswith("@@"):
            line = lines[i]
            tag, text = line[:1], line[1:]
            if i + 1 < len(lines) and lines[i + 1].rstrip("\r\n") == NO_NEWLINE:
                text = text.removesuffix("\n")
                i += 1
            if tag in (" ", "-"):
                if position >= len(source) or source[position] != text:
                    raise PatchError(
                        f"line {position + 1} doesn't match the plan's diff"
                    )
                position += 1
                seen_old += 1
            if tag in (" ", "+"):
                result.append(text)
                seen_new += 1
            if tag not in (" ", "-", "+"):
                raise PatchError(f"unexpected line in a hunk: {line!r}")
            i += 1
        if seen_old != old_count or seen_new != new_count:
            raise PatchError("a hunk's line counts don't match its header")
    result.extend(source[position:])
    return "".join(result)


def _lines(diff: str) -> list[str]:
    """Lines ending after each "\\n", the last one possibly without: "\\r\\n"
    endings, form feeds and the like stay inside lines (of files, and of
    diffs, whose own lines end with "\\n")."""
    lines: list[str] = []
    start = 0
    for index, char in enumerate(diff):
        if char == "\n":
            lines.append(diff[start : index + 1])
            start = index + 1
    if start < len(diff):
        lines.append(diff[start:])
    return lines
