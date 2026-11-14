from __future__ import annotations

import argparse
import itertools
import sys
from argparse import ArgumentParser
from dataclasses import dataclass
from typing import Any

from django.core.management.base import CommandError
from django_rich.management import RichCommand
from rich.console import Console
from rich.text import Text

from django_clean_migrations.core import CleanError, Removal, Result, load_plan, plan
from django_clean_migrations.solver import KINDS, OPAQUE_POLICIES

KIND_STYLES = {
    "model": "red",
    "field": "cyan",
    "index": "blue",
    "constraint": "magenta",
    "together": "green",
    "superseded": "yellow",
    "elidable": "bright_magenta",
}


@dataclass
class Row:
    indent: int
    name: Text
    kind: str = ""
    span: Text | None = None
    count: int | None = None
    note: str = ""


class Command(RichCommand):
    help = (
        "Remove migration operations that cancel out over the whole history,"
        " without changing the final schema."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        subparsers = parser.add_subparsers(
            dest="subcommand", metavar="{plan,apply}", title="subcommands"
        )
        plan_parser = subparsers.add_parser(
            "plan",
            help="Report what can be removed, optionally saving a plan file.",
            description="Analyse the migration history and report what can be"
            " removed. Nothing is written, except the plan file with --output.",
        )
        _add_selection_arguments(plan_parser)
        plan_parser.add_argument(
            "-o",
            "--output",
            metavar="FILE",
            help="Save the plan as a JSON file, to review and then apply.",
        )
        plan_parser.add_argument(
            "--check",
            action="store_true",
            help="Exit with status 1 if anything is removable.",
        )
        _add_show_blocked(plan_parser)
        _add_common_arguments(plan_parser)
        apply_parser = subparsers.add_parser(
            "apply",
            help="Edit the migration files, from a plan file or a fresh analysis.",
            description="Apply a saved plan (--plan FILE), or analyse and apply"
            " in one go. The result is verified on disk and rolled back on"
            " failure.",
        )
        apply_parser.add_argument(
            "--plan",
            metavar="FILE",
            dest="plan_file",
            help="A plan file saved by 'plan --output'. Refused if any"
            " migration file changed since.",
        )
        _add_selection_arguments(apply_parser)
        _add_show_blocked(apply_parser)
        _add_common_arguments(apply_parser)

    def handle(self, *args: Any, **options: Any) -> None:
        subcommand = options.get("subcommand")
        if subcommand is None:
            self.print_help("manage.py", "clean_migrations")
            return
        verbosity: int = options["verbosity"]
        show_blocked = options["show_blocked"] or verbosity >= 2
        if subcommand == "apply" and options["plan_file"]:
            if _has_selection(options):
                raise CommandError(
                    "--plan can't be combined with targets or analysis options:"
                    " the plan file already fixes them."
                )
            try:
                result = load_plan(options["plan_file"])
            except CleanError as exc:
                raise CommandError(str(exc)) from exc
        else:
            result = self._analyse(options, verbosity)
        if subcommand == "plan":
            self._report(result, verbosity, show_blocked, False, options)
            if options["output"]:
                result.save(options["output"])
                if verbosity > 0:
                    self._print(
                        Text(
                            f"Saved the plan to {options['output']}. Apply it with:"
                            f" manage.py clean_migrations apply --plan"
                            f" {options['output']}",
                            style="dim",
                        )
                    )
            elif verbosity > 0 and result.changed:
                self._print(Text("Run 'clean_migrations apply' to apply.", style="dim"))
            if options["check"] and result.changed:
                raise SystemExit(1)
            return
        self._report(result, verbosity, show_blocked, True, options)
        if result.changed:
            try:
                result.write()
            except CleanError as exc:
                raise CommandError(str(exc)) from exc

    def _analyse(self, options: dict[str, Any], verbosity: int) -> Result:
        try:
            if verbosity > 0:
                with self.console.status("Analysing migrations...", spinner="dots"):
                    return self._plan(options)
            return self._plan(options)
        except CleanError as exc:
            raise CommandError(str(exc)) from exc

    @staticmethod
    def _plan(options: dict[str, Any]) -> Result:
        return plan(
            options["targets"],
            exclude=options["exclude"],
            kinds=options["kinds"],
            opaque=options["opaque"],
            elidable=options["elidable"],
        )

    def _stderr_console(self, options: dict[str, Any]) -> Console:
        force_terminal: bool | None = None
        if options["no_color"]:
            force_terminal = False
        elif options["force_color"]:
            force_terminal = True
        return Console(
            file=options.get("stderr") or sys.stderr, force_terminal=force_terminal
        )

    def _print(self, text: Text | str, **kwargs: Any) -> None:
        self.console.print(text, soft_wrap=True, highlight=False, **kwargs)

    def _report(
        self,
        result: Result,
        verbosity: int,
        show_blocked: bool,
        write: bool,
        options: dict[str, Any],
    ) -> None:
        if verbosity == 0:
            return
        if result.warnings:
            err = self._stderr_console(options)
            for warning in result.warnings:
                err.print(
                    Text(f"Warning: {warning}", style="yellow"),
                    soft_wrap=True,
                    highlight=False,
                )
        self._report_removals(result.removals, verbosity)
        if show_blocked and result.blocked:
            self._report_blocked(result)
        if not result.changed:
            self._print("No operations to remove.")
            return
        self._print(Text(summary(result, write), style="bold green"))

    def _report_removals(self, removals: list[Removal], verbosity: int) -> None:
        rows: list[tuple[Row, list[Text]]] = []
        for app, app_group in itertools.groupby(removals, key=lambda r: r.app_label):
            rows.append((Row(0, Text(app, style="bold")), []))
            by_model: dict[str | None, list[Removal]] = {}
            for removal in app_group:
                by_model.setdefault(removal.model, []).append(removal)
            for model, group in by_model.items():
                children = group
                indent = 1
                if model is not None:
                    indent = 2
                    head = next((r for r in group if r.kind == "model"), None)
                    if head is not None:
                        children = [r for r in group if r is not head]
                        rows.append(
                            (
                                _row(1, head, Text(head.name, style="bold")),
                                _locations(head, 1, verbosity),
                            )
                        )
                    else:
                        rows.append((Row(1, Text(model, style="bold")), []))
                for removal in children:
                    rows.append(
                        (
                            _row(indent, removal, Text(removal.name, style="bold")),
                            _locations(removal, indent, verbosity),
                        )
                    )
        name_width = max((2 * r.indent + len(r.name) for r, _ in rows), default=0)
        kind_width = max((len(r.kind) for r, _ in rows), default=0)
        span_width = max((len(r.span) for r, _ in rows if r.span), default=0)
        count_width = max(
            (len(_ops(r.count)) for r, _ in rows if r.count is not None), default=0
        )
        for row, locations in rows:
            line = Text("  " * row.indent)
            line.append_text(row.name)
            line.append(" " * (name_width - 2 * row.indent - len(row.name) + 2))
            line.append(row.kind.ljust(kind_width), style=KIND_STYLES.get(row.kind, ""))
            line.append("  ")
            span = row.span or Text("")
            line.append_text(span)
            line.append(" " * (span_width - len(span)))
            if row.count is not None:
                line.append("  ")
                line.append(_ops(row.count).rjust(count_width), style="dim")
            if row.note:
                line.append(f"  ({row.note})", style="dim")
            line.rstrip()
            self._print(line)
            for location in locations:
                self._print(location)

    def _report_blocked(self, result: Result) -> None:
        self._print(Text("Blocked", style="bold yellow"))
        for blocked in result.blocked:
            line = Text("  ")
            line.append(blocked.label, style="bold")
            line.append(f"  {blocked.description}", style="dim")
            self._print(line)
            for reason in blocked.reasons:
                self._print(Text(f"    {reason}", style="yellow"))


def _add_selection_arguments(parser: ArgumentParser) -> None:
    parser.add_argument(
        "targets",
        nargs="*",
        metavar="target",
        help="app_label, app_label.Model or app_label.Model.field to clean.",
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


def _add_common_arguments(parser: ArgumentParser) -> None:
    """
    Accept Django's display options after the subcommand too
    (``clean_migrations plan -v 2``); SUPPRESS keeps the values given before
    it when they aren't repeated.
    """
    parser.add_argument(
        "-v",
        "--verbosity",
        type=int,
        choices=[0, 1, 2, 3],
        default=argparse.SUPPRESS,
        help="Verbosity level; 0=minimal output, 1=normal output, 2=verbose output.",
    )
    for flag in ("--traceback", "--no-color", "--force-color"):
        parser.add_argument(flag, action="store_true", default=argparse.SUPPRESS)


def _add_show_blocked(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--show-blocked",
        action="store_true",
        help="List removable-looking entities that are blocked, with reasons.",
    )


def _has_selection(options: dict[str, Any]) -> bool:
    return bool(
        options["targets"]
        or options["exclude"]
        or options["kinds"]
        or options["opaque"] != "scan"
        or options["elidable"]
    )


def _ops(count: int) -> str:
    return _count(count, "operation")


def _row(indent: int, removal: Removal, name: Text) -> Row:
    span = Text()
    if removal.start:
        span.append(removal.start, style="dim")
    if removal.end:
        span.append(" -> ", style="dim")
        span.append(removal.end, style="dim")
    return Row(indent, name, removal.kind, span, len(removal.elements), removal.note)


def _locations(removal: Removal, indent: int, verbosity: int) -> list[Text]:
    if verbosity < 2:
        return []
    lines = []
    for loc in removal.elements:
        line = Text("  " * (indent + 1))
        where = loc.display_path()
        if loc.line is not None:
            where += f":{loc.line}"
        line.append(where)
        line.append(f"  {loc.operation}", style="bold")
        if loc.detail:
            line.append(f" ({loc.detail})", style="dim")
        lines.append(line)
    return lines


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
