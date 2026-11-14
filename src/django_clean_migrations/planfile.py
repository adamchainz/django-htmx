"""
Plan files: a JSON document holding everything needed to review and apply a
plan without analysing the history again.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from django_clean_migrations.core import (
    Blocked,
    CleanError,
    FileChange,
    Location,
    Removal,
    Result,
    _read_text,
)

FORMAT = "django-clean-migrations-plan"
FORMAT_VERSION = 1


def tool_version() -> str:
    try:
        return version("django-clean-migrations")
    except PackageNotFoundError:  # pragma: no cover
        return "unknown"


def _display_path(path: Path) -> str:
    """Relative to the current directory when inside it, else absolute."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path.resolve())


def _location_dict(loc: Location) -> dict[str, Any]:
    return {
        "migration": list(loc.migration),
        "path": _display_path(loc.path) if loc.path is not None else None,
        "line": loc.line,
        "operation": loc.operation,
        "detail": loc.detail,
    }


def plan_data(result: Result) -> dict[str, Any]:
    removals = []
    for removal in result.removals:
        data = asdict(removal)
        data["elements"] = [_location_dict(loc) for loc in removal.elements]
        data["targets"] = [_location_dict(loc) for loc in removal.targets]
        removals.append(data)
    return {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "tool_version": tool_version(),
        "options": result.options,
        "verification": result.verification,
        "summary": {
            "removed_operations": result.removed_operations,
            "edited_operations": result.edited_operations,
            "folded_operations": result.folded_operations,
            "empty_migrations": [list(k) for k in result.empty_migrations],
        },
        "warnings": list(result.warnings),
        "removals": removals,
        "blocked": [asdict(b) for b in result.blocked],
        "files": [
            {
                "path": _display_path(change.path),
                "migration": list(change.migration),
                "module": change.module,
                "sha256_before": change.sha256_before,
                "operations_after": list(change.operations_after),
                "content_after": change.new,
            }
            for change in result.files
        ],
    }


def save_plan(result: Result, path: Path) -> None:
    text = json.dumps(plan_data(result), indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else Path.cwd() / p


def _location(loc: dict[str, Any]) -> Location:
    return Location(
        migration=(loc["migration"][0], loc["migration"][1]),
        path=_resolve(loc["path"]) if loc["path"] else None,
        line=loc["line"],
        operation=loc["operation"],
        detail=loc["detail"],
    )


def load_plan(path: Path) -> Result:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CleanError(f"Could not read plan file {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise CleanError(f"{path} is not a django-clean-migrations plan file.")
    if data.get("version") != FORMAT_VERSION:
        raise CleanError(
            f"{path} uses plan format version {data.get('version')!r}; this"
            f" version of django-clean-migrations reads version {FORMAT_VERSION}."
        )
    try:
        files = []
        for f in data["files"]:
            file_path = _resolve(f["path"])
            try:
                original = _read_text(file_path)
            except OSError as exc:
                raise CleanError(
                    f"Migration file {f['path']} from the plan is missing ({exc})."
                    " Run apply from the directory the plan was made in."
                ) from exc
            files.append(
                FileChange(
                    path=file_path,
                    migration=(f["migration"][0], f["migration"][1]),
                    module=f["module"],
                    original=original,
                    new=f["content_after"],
                    operations_after=tuple(f["operations_after"]),
                    expected_sha256=f["sha256_before"],
                )
            )
        removals = [
            Removal(
                **{
                    **r,
                    "elements": tuple(_location(loc) for loc in r["elements"]),
                    "targets": tuple(_location(loc) for loc in r.get("targets", [])),
                }
            )
            for r in data["removals"]
        ]
        blocked = [
            Blocked(**{**b, "reasons": tuple(b["reasons"])}) for b in data["blocked"]
        ]
        summary = data["summary"]
        return Result(
            removals=removals,
            blocked=blocked,
            warnings=list(data["warnings"]),
            files=files,
            removed_operations=summary["removed_operations"],
            edited_operations=summary["edited_operations"],
            folded_operations=summary.get("folded_operations", 0),
            empty_migrations=[(a, n) for a, n in summary["empty_migrations"]],
            options=data["options"],
            verification=data["verification"],
        )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise CleanError(f"Malformed plan file {path}: {exc!r}") from exc
