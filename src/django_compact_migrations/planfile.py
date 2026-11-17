"""
Plan files: a TOML document holding everything needed to review and apply a
plan without analysing the history again, laid out for reading top-down:
what the plan does and how it was checked, then the removals, then the
files, each edit as a unified diff.
"""

from __future__ import annotations

import datetime
import os
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import tomli_w
from django.db.migrations.loader import MigrationLoader

from django_compact_migrations.core import (
    Analysis,
    Blocked,
    CompactError,
    FileChange,
    Location,
    Removal,
    Result,
    analyse,
    build,
    make_result,
    sha256_text,
)
from django_compact_migrations.history import History, Key, MigrationInfo
from django_compact_migrations.patch import PatchError, apply_diff, make_diff
from django_compact_migrations.solver import KINDS

FORMAT = "django-compact-migrations-plan"
FORMAT_VERSION = 1


def tool_version() -> str:
    try:
        return version("django-compact-migrations")
    except PackageNotFoundError:
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


def dumps(data: dict[str, Any]) -> str:
    """Write TOML, leaving out None values (TOML has no null), with multi-line
    strings (diffs) on their own lines.

    TOML readers turn CRLF inside multi-line strings into LF, so when any
    string has a carriage return, all strings are written on one line with
    escapes instead.
    """
    data = _without_none(data)
    return tomli_w.dumps(data, multiline_strings=not _has_carriage_return(data))


def _has_carriage_return(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_carriage_return(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_carriage_return(v) for v in value)
    return isinstance(value, str) and "\r" in value


def _without_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list | tuple):
        return [_without_none(v) for v in value if v is not None]
    return value


def save_plan(
    result: Result, path: str | os.PathLike[str], *, blocked: bool = False
) -> None:
    """Write the plan as a reviewable TOML file; ``blocked`` includes what
    was kept and why."""
    path = Path(path)
    text = dumps(plan_data(result, path.resolve().parent, blocked=blocked))
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def _location(loc: dict[str, Any], base: Path) -> Location:
    path = loc.get("path")
    return Location(
        migration=(loc["migration"][0], loc["migration"][1]),
        path=(Path(path) if Path(path).is_absolute() else base / path)
        if path
        else None,
        line=loc.get("line"),
        operation=loc["operation"],
        detail=loc.get("detail"),
    )


def _migration_file(f: dict[str, Any], history: History) -> MigrationInfo:
    """
    The file of the migration an entry names, as the project's migration
    loader finds it now: the plan's ``module`` and ``path`` are never used
    to find a file, only checked.
    """
    key = (str(f["migration"][0]), str(f["migration"][1]))
    info = history.by_key.get(key)
    if info is None:
        raise CompactError(
            f"The plan names {key[0]}.{key[1]}, which isn't a migration of this"
            " project, so nothing was written."
        )
    package, _ = MigrationLoader.migrations_module(key[0])
    module = f"{package}.{key[1]}"
    if f["module"] != module or type(info.migration).__module__ != module:
        raise CompactError(
            f"The plan gives {f['module']!r} as the module of {info.label},"
            f" which is {module!r}, so nothing was written."
        )
    if not info.editable:
        raise CompactError(
            f"The plan edits {info.label}, which is not editable"
            f" ({info.read_only}), so nothing was written."
        )
    return info


def _file_change(
    f: dict[str, Any], history: History, deleted: bool, seen: set[Key]
) -> FileChange:
    info = _migration_file(f, history)
    key = info.key
    if key in seen:
        raise CompactError(f"The plan names {key[0]}.{key[1]} twice.")
    seen.add(key)
    # Editable: its source was read when the history was loaded.
    assert info.source is not None and info.path is not None
    file_path = info.path
    original, encoding = info.source.text, info.source.encoding
    if sha256_text(original, encoding) != f["sha256_before"]:
        raise CompactError(
            f"{file_path} changed since the plan was made, so nothing was"
            " written. Make a new plan."
        )
    new = ""
    if not deleted:
        try:
            new = apply_diff(original, f["diff"])
        except PatchError as exc:
            raise CompactError(
                f"The plan's diff for {f['path']} doesn't apply ({exc}): the"
                " plan was edited or is damaged. Make a new plan."
            ) from exc
        if sha256_text(new, encoding) != f["sha256_after"]:
            raise CompactError(
                f"The plan's diff for {f['path']} doesn't give the file it was"
                " made for: the plan was edited or is damaged. Make a new plan."
            )
    return FileChange(
        path=file_path,
        migration=key,
        module=f["module"],
        original=original,
        new=new,
        operations_after=() if deleted else tuple(f["operations_after"]),
        expected_sha256=f["sha256_before"],
        deleted=deleted,
        encoding=encoding,
    )


def _check_recomputed(result: Result, analysis: Analysis) -> None:
    """A static plan must be exactly what the project gives now with the
    plan's options: anything else was edited, or made from other files."""
    analysis.options["noop_data"] = False
    analysis.options["prune"] = bool(result.options.get("prune"))
    solution = analysis.solve()
    fresh = make_result(analysis, solution, build(analysis, solution))
    edits = {c.migration: (c.deleted, c.new) for c in result.files}
    if edits != {c.migration: (c.deleted, c.new) for c in fresh.files}:
        backends_note = ""
        if sorted(result.options.get("backends", [])) != analysis.options["backends"]:
            backends_note = (
                " The plan was made with other database backends configured"
                f" ({', '.join(result.options.get('backends', [])) or 'none'})."
            )
        raise CompactError(
            "The plan's edits aren't the ones its options give for this"
            " project now, so nothing was written: the plan was edited, or"
            f" made from other files or settings.{backends_note} Make a new plan."
        )


def load_plan(path: str | os.PathLike[str]) -> Result:
    """Read a plan saved by save_plan(); ``Result.write()`` applies it."""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CompactError(f"Could not read plan file {path}: {exc}") from exc
    if data.get("format") != FORMAT:
        raise CompactError(f"{path} is not a django-compact-migrations plan file.")
    if data.get("version") != FORMAT_VERSION:
        raise CompactError(
            f"{path} uses plan format version {data.get('version')!r}; this"
            f" version of django-compact-migrations reads version {FORMAT_VERSION}."
        )
    base = path.resolve().parent
    try:
        options = data["options"]
        try:
            analysis = analyse(
                options["targets"],
                exclude=options["exclude"],
                skip_kinds=sorted(set(KINDS) - set(options["kinds"])),
                elidable=options["elidable"],
            )
        except CompactError as exc:
            raise CompactError(f"The plan's options don't apply here: {exc}") from exc
        seen: set[Key] = set()
        files = [_file_change(f, analysis.history, False, seen) for f in data["files"]]
        files += [
            _file_change(f, analysis.history, True, seen) for f in data["deleted"]
        ]
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
            for r in data["removals"]
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
        result = Result(
            removals=removals,
            blocked=blocked,
            warnings=list(data["warnings"]),
            files=files,
            removed_operations=summary["removed_operations"],
            edited_operations=summary["edited_operations"],
            folded_operations=summary["folded_operations"],
            empty_migrations=[(a, n) for a, n in summary["empty_migrations"]],
            pruned_migrations=[(a, n) for a, n in summary["pruned_migrations"]],
            options=options,
            verification=data["verification"],
            from_plan=True,
            _reference=analysis,
        )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise CompactError(f"Malformed plan file {path}: {exc!r}") from exc
    if result.verification.get("mode") != "trial":
        _check_recomputed(result, analysis)
    return result
