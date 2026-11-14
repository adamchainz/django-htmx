from __future__ import annotations

from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from django_clean_migrations.core import CleanError, Result, clean
from django_clean_migrations.solver import KINDS, OPAQUE_POLICIES


class Command(BaseCommand):
    help = (
        "Remove migration operations that cancel out over the whole history,"
        " without changing the final schema."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "targets",
            nargs="*",
            metavar="target",
            help="app_label, app_label.Model or app_label.Model.field to clean.",
        )
        parser.add_argument(
            "--write", action="store_true", help="Edit the migration files."
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="Exit with status 1 if anything is removable. Never writes.",
        )
        parser.add_argument(
            "--exclude",
            action="append",
            default=[],
            metavar="TARGET",
            help="Never remove anything matching this target (repeatable).",
        )
        parser.add_argument(
            "--kind",
            action="append",
            choices=KINDS,
            dest="kinds",
            help="Only consider these kinds of removal (repeatable).",
        )
        parser.add_argument(
            "--opaque",
            choices=OPAQUE_POLICIES,
            default="scan",
            help="How RunPython/RunSQL/custom operations block removals.",
        )
        parser.add_argument(
            "--elidable",
            action="store_true",
            help="Also remove every elidable RunPython/RunSQL operation.",
        )
        parser.add_argument(
            "--show-blocked",
            action="store_true",
            help="List removable-looking entities that are blocked, with reasons.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["write"] and options["check"]:
            raise CommandError("--write and --check are mutually exclusive.")
        try:
            result = clean(
                options["targets"],
                exclude=options["exclude"],
                kinds=options["kinds"],
                opaque=options["opaque"],
                elidable=options["elidable"],
            )
        except CleanError as exc:
            raise CommandError(str(exc)) from exc
        verbosity: int = options["verbosity"]
        write: bool = options["write"]
        self._report(
            result, verbosity, options["show_blocked"] or verbosity >= 2, write
        )
        if write and result.changed:
            try:
                result.write()
            except CleanError as exc:
                raise CommandError(str(exc)) from exc
        if options["check"] and result.changed:
            raise SystemExit(1)

    def _report(
        self, result: Result, verbosity: int, show_blocked: bool, write: bool
    ) -> None:
        if verbosity == 0:
            return
        for warning in result.warnings:
            self.stderr.write(self.style.WARNING(f"Warning: {warning}"))
        for removal in result.removals:
            count = len(removal.elements)
            noun = "operation" if count == 1 else "operations"
            self.stdout.write(
                f"{self.style.MIGRATE_LABEL(removal.label)}: {removal.description}"
                f" ({count} {noun})"
            )
            if verbosity >= 2:
                for loc in removal.elements:
                    detail = f" ({loc.detail})" if loc.detail else ""
                    line = f":{loc.line}" if loc.line is not None else ""
                    self.stdout.write(
                        f"    {loc.display_path()}{line}  {loc.operation}{detail}"
                    )
        if show_blocked and result.blocked:
            self.stdout.write(self.style.MIGRATE_HEADING("Blocked:"))
            for blocked in result.blocked:
                self.stdout.write(f"  {blocked.label}: {blocked.description}")
                for reason in blocked.reasons:
                    self.stdout.write(f"    - {reason}")
        if not result.changed:
            self.stdout.write("No operations to remove.")
            return
        self.stdout.write(self.style.SUCCESS(summary(result, write)))


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def summary(result: Result, write: bool) -> str:
    """E.g. "Would remove 3 operations and edit 1 in 2 migration files."."""
    actions = []
    if result.removed_operations:
        verb = "Removed" if write else "Would remove"
        actions.append(f"{verb} {_count(result.removed_operations, 'operation')}")
    if result.edited_operations:
        verb = "edited" if write else "edit"
        if not actions:
            verb = verb.capitalize() if write else "Would edit"
        actions.append(f"{verb} {_count(result.edited_operations, 'operation')}")
    text = " and ".join(actions) + f" in {_count(len(result.files), 'migration file')}."
    empty = len(result.empty_migrations)
    if empty:
        text += f" {_count(empty, 'migration')} "
        if write:
            text += (
                "now has no operations." if empty == 1 else "now have no operations."
            )
        else:
            text += "would be left with no operations."
    return text
