"""
Plan files: a TOML document holding everything needed to review and apply a
plan without analysing the history again, laid out for reading top-down:
what the plan does and how it was checked, then the removals, then the
files, each edit as a unified diff.
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import tomllib
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
    sha256_text,
)
from django_clean_migrations.patch import PatchError, apply_diff, make_diff
from django_clean_migrations.tomlwriter import dumps

FORMAT = "django-clean-migrations-plan"
FORMAT_VERSION = 2


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


def _key(key: tuple[str, str]) -> list[str]:
    return [key[0], key[1]]


def plan_data(result: Result, base: Path, *, blocked: bool = False) -> dict[str, Any]:
    """The plan as TOML data; paths relative to ``base``, the directory the
    plan file is saved in. ``blocked`` adds what was kept and why."""
    removals = sorted(
        result.removals,
        key=lambda r: (r.app_label, r.model or "", r.kind, r.label, r.start),
    )
    data: dict[str, Any] = {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "tool_version": tool_version(),
        # Which copy of the tool made the plan: an installed or older copy
        # on the path can shadow the one meant.
        "tool_path": str(Path(__file__).resolve().parent),
        "created": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "warnings": list(result.warnings),
        "summary": {
            "removed_operations": result.removed_operations,
            "folded_operations": result.folded_operations,
            "edited_operations": result.edited_operations,
            "files_changed": sum(1 for c in result.files if not c.deleted),
            "files_deleted": sum(1 for c in result.files if c.deleted),
            "empty_migrations": [_key(k) for k in result.empty_migrations],
            "pruned_migrations": [_key(k) for k in result.pruned_migrations],
        },
        "options": result.options,
        "verification": result.verification,
        "removals": [
            {
                "app": r.app_label,
                "model": r.model,
                "kind": r.kind,
                "label": r.label,
                "name": r.name or None,
                "start": r.start or None,
                "end": r.end or None,
                "description": r.description,
                "note": r.note or None,
                "elements": tuple(_location_dict(loc, base) for loc in r.elements),
                "targets": tuple(_location_dict(loc, base) for loc in r.targets)
                or None,
            }
            for r in removals
        ],
    }
    if blocked:
        data["blocked"] = [
            {
                "app": b.app_label,
                "model": b.model,
                "kind": b.kind,
                "label": b.label,
                "description": b.description or None,
                "reasons": list(b.reasons),
            }
            for b in sorted(
                result.blocked, key=lambda b: (b.app_label, b.model or "", b.label)
            )
        ]
    data["deleted"] = [
        {
            "migration": _key(c.migration),
            "module": c.module,
            "path": _relative_path(c.path, base),
            "sha256_before": c.sha256_before,
        }
        for c in result.files
        if c.deleted
    ]
    data["files"] = [
        {
            "migration": _key(c.migration),
            "module": c.module,
            "path": _relative_path(c.path, base),
            "encoding": c.encoding if c.encoding != "utf-8" else None,
            "operations_after": list(c.operations_after),
            "sha256_before": c.sha256_before,
            "sha256_after": c.sha256_after,
            "diff": make_diff(c.original, c.new, _relative_path(c.path, base)),
        }
        for c in result.files
        if not c.deleted
    ]
    return data


def save_plan(result: Result, path: Path, *, blocked: bool = False) -> None:
    text = dumps(plan_data(result, path.resolve().parent, blocked=blocked))
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
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
        path=_resolve(loc["path"], base) if loc.get("path") else None,
        line=loc.get("line"),
        operation=loc["operation"],
        detail=loc.get("detail"),
    )


def _file_change(f: dict[str, Any], base: Path, deleted: bool) -> FileChange:
    # The migration module, as imported now, wins: the plan may have been
    # moved since; the hash checks catch any mix-up.
    file_path = _module_file(f["module"]) or _resolve(f["path"], base)
    try:
        original, encoding = _read_text(file_path)
    except (OSError, UnicodeDecodeError, SyntaxError, LookupError) as exc:
        raise CleanError(
            f"Migration file {f['path']} from the plan can't be read ({exc})."
        ) from exc
    migration = (f["migration"][0], f["migration"][1])
    if sha256_text(original, encoding) != f["sha256_before"]:
        raise CleanError(
            f"{file_path} changed since the plan was made, so nothing was"
            " written. Make a new plan."
        )
    new = ""
    if not deleted:
        try:
            new = apply_diff(original, f["diff"])
        except PatchError as exc:
            raise CleanError(
                f"The plan's diff for {f['path']} doesn't apply ({exc}): the"
                " plan was edited or is damaged. Make a new plan."
            ) from exc
        if sha256_text(new, encoding) != f["sha256_after"]:
            raise CleanError(
                f"The plan's diff for {f['path']} doesn't give the file it was"
                " made for: the plan was edited or is damaged. Make a new plan."
            )
    return FileChange(
        path=file_path,
        migration=migration,
        module=f["module"],
        original=original,
        new=new,
        operations_after=tuple(f.get("operations_after", ())),
        expected_sha256=f["sha256_before"],
        deleted=deleted,
        encoding=encoding,
    )


def load_plan(path: Path) -> Result:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CleanError(f"Could not read plan file {path}: {exc}") from exc
    if data.get("format") != FORMAT:
        raise CleanError(f"{path} is not a django-clean-migrations plan file.")
    if data.get("version") != FORMAT_VERSION:
        raise CleanError(
            f"{path} uses plan format version {data.get('version')!r}; this"
            f" version of django-clean-migrations reads version {FORMAT_VERSION}."
        )
    base = path.resolve().parent
    try:
        files = [_file_change(f, base, False) for f in data.get("files", [])]
        files += [_file_change(f, base, True) for f in data.get("deleted", [])]
        removals = [
            Removal(
                kind=r["kind"],
                app_label=r["app"],
                model=r.get("model"),
                label=r["label"],
                description=r["description"],
                elements=tuple(_location(loc, base) for loc in r["elements"]),
                name=r.get("name", ""),
                start=r.get("start", ""),
                end=r.get("end", ""),
                note=r.get("note", ""),
                targets=tuple(_location(loc, base) for loc in r.get("targets", [])),
            )
            for r in data.get("removals", [])
        ]
        blocked = [
            Blocked(
                kind=b["kind"],
                app_label=b["app"],
                model=b.get("model"),
                label=b["label"],
                reasons=tuple(b["reasons"]),
                description=b.get("description", ""),
            )
            for b in data.get("blocked", [])
        ]
        summary = data["summary"]
        return Result(
            removals=removals,
            blocked=blocked,
            warnings=list(data.get("warnings", [])),
            files=files,
            removed_operations=summary["removed_operations"],
            edited_operations=summary["edited_operations"],
            folded_operations=summary.get("folded_operations", 0),
            empty_migrations=[(a, n) for a, n in summary["empty_migrations"]],
            pruned_migrations=[(a, n) for a, n in summary["pruned_migrations"]],
            options=data["options"],
            verification=data["verification"],
        )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise CleanError(f"Malformed plan file {path}: {exc!r}") from exc
