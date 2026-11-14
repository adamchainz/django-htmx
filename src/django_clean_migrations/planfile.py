"""
Plan files: a JSON document holding everything needed to review and apply a
plan without analysing the history again.
"""

from __future__ import annotations

import importlib.util
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


def _relative_path(path: Path, base: Path) -> str:
    """Relative to the plan file's directory (POSIX separators), so the plan
    can be applied from any directory."""
    try:
        return Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()
    except ValueError:  # pragma: no cover - another drive on Windows
        return str(path.resolve())


def _location_dict(loc: Location, base: Path) -> dict[str, Any]:
    return {
        "migration": list(loc.migration),
        "path": _relative_path(loc.path, base) if loc.path is not None else None,
        "line": loc.line,
        "operation": loc.operation,
        "detail": loc.detail,
    }


def plan_data(result: Result, base: Path) -> dict[str, Any]:
    """The plan as JSON data; paths relative to ``base``, the directory the
    plan file is saved in."""
    removals = []
    for removal in result.removals:
        data = asdict(removal)
        data["elements"] = [_location_dict(loc, base) for loc in removal.elements]
        data["targets"] = [_location_dict(loc, base) for loc in removal.targets]
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
            "pruned_migrations": [list(k) for k in result.pruned_migrations],
        },
        "warnings": list(result.warnings),
        "removals": removals,
        "blocked": [asdict(b) for b in result.blocked],
        "files": [
            {
                "path": _relative_path(change.path, base),
                "migration": list(change.migration),
                "module": change.module,
                "sha256_before": change.sha256_before,
                "operations_after": list(change.operations_after),
                "deleted": change.deleted,
                "content_after": None if change.deleted else change.new,
            }
            for change in result.files
        ],
    }


def save_plan(result: Result, path: Path) -> None:
    data = plan_data(result, path.resolve().parent)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _resolve(path: str, base: Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else base / p


def _module_file(module: str) -> Path | None:
    """Where the migration module is now, as Python would import it."""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin or not os.path.isfile(spec.origin):
        return None
    return Path(spec.origin)


def _location(loc: dict[str, Any], base: Path) -> Location:
    return Location(
        migration=(loc["migration"][0], loc["migration"][1]),
        path=_resolve(loc["path"], base) if loc["path"] else None,
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
    base = path.resolve().parent
    try:
        files = []
        for f in data["files"]:
            # The migration module, as imported now, wins: the plan may have
            # been moved since; the hash check below catches any mix-up.
            relative = _resolve(f["path"], base)
            file_path = _module_file(f["module"]) or relative
            try:
                original = _read_text(file_path)
            except OSError as exc:
                raise CleanError(
                    f"Migration file {f['path']} from the plan is missing ({exc})."
                ) from exc
            files.append(
                FileChange(
                    path=file_path,
                    migration=(f["migration"][0], f["migration"][1]),
                    module=f["module"],
                    original=original,
                    new=f["content_after"] or "",
                    operations_after=tuple(f["operations_after"]),
                    expected_sha256=f["sha256_before"],
                    deleted=bool(f.get("deleted", False)),
                )
            )
        removals = [
            Removal(
                **{
                    **r,
                    "elements": tuple(_location(loc, base) for loc in r["elements"]),
                    "targets": tuple(
                        _location(loc, base) for loc in r.get("targets", [])
                    ),
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
            pruned_migrations=[(a, n) for a, n in summary.get("pruned_migrations", [])],
            options=data["options"],
            verification=data["verification"],
        )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise CleanError(f"Malformed plan file {path}: {exc!r}") from exc
