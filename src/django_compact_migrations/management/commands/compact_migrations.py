from __future__ import annotations

import argparse
import itertools
import os
import sys
from argparse import ArgumentParser
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.management.base import CommandError
from django.db import connections
from django_rich.management import RichCommand
from rich.console import Console
from rich.text import Text

from django_compact_migrations import backends
from django_compact_migrations.core import (
    CompactError,
    Removal,
    Result,
    count_noun,
    plan,
)
from django_compact_migrations.planfile import load_plan, save_plan
from django_compact_migrations.solver import KINDS

KIND_STYLES = {
    "model": "red",
    "field": "cyan",
    "index": "blue",
    "constraint": "magenta",
    "together": "green",
    "superseded": "yellow",
    "elidable": "bright_magenta",
    "fold": "bright_cyan",
    "noop": "bright_yellow",
    "prune": "bright_red",
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
        "Remove or merge migration operations that cancel out over the whole"
        " history, so that migrating a fresh database is faster."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        subparsers = parser.add_subparsers(
            dest="subcommand",
            metavar="{plan,apply}",
            title="subcommands",
        )
        plan_parser = subparsers.add_parser(
            "plan",
            help="Report what can be removed, optionally saving a plan file.",
            description="Analyse the migration history and report what can be"
            " removed. Nothing is written, except the plan file with --output.",
        )
        _add_selection_arguments(plan_parser)
        _add_trial_arguments(plan_parser)
        plan_parser.add_argument(
            "-o",
            "--output",
            metavar="FILE",
            help="Save the plan as a TOML file, to review and then apply.",
        )
        plan_parser.add_argument(
            "--check",
            action="store_true",
            help="Exit with status 1 if anything is removable.",
        )
        _add_common_arguments(plan_parser, parser)
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
        apply_parser.add_argument(
            "--no-database-check",
            action="store_true",
            help="Don't check that the configured databases have applied every"
            " migration this edits or deletes.",
        )
        apply_parser.add_argument(
            "--allow-dirty",
            action="store_true",
            help="Edit migration files that have changes Git hasn't committed"
            " (refused by default, so the edit can be reviewed and reverted"
            " on its own).",
        )
        _add_selection_arguments(apply_parser)
        _add_trial_arguments(apply_parser)
        _add_common_arguments(apply_parser, parser)

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            self._handle(options)
        except KeyboardInterrupt:
            # Scratch databases are dropped and written files restored on
            # the way out; a traceback would only hide that.
            self._stderr_console(options).print(
                "Interrupted: scratch databases were dropped and no migration"
                " file was changed.",
                style="yellow",
                highlight=False,
            )
            raise SystemExit(130) from None

    def _handle(self, options: dict[str, Any]) -> None:
        subcommand = options.get("subcommand")
        if subcommand is None:
            self.print_help("manage.py", "compact_migrations")
            return
        verbosity: int = options["verbosity"]
        show_blocked = verbosity >= 2
        if subcommand == "apply" and options["plan_file"]:
            if _has_selection(options):
                raise CommandError(
                    "--plan can't be combined with targets or analysis options:"
                    " the plan file already fixes them."
                )
            try:
                result = load_plan(options["plan_file"])
            except CompactError as exc:
                raise CommandError(str(exc)) from None
        else:
            result = self._analyse(options, verbosity)
        self._report(result, verbosity, show_blocked, options)
        if subcommand == "plan":
            if verbosity > 0 and result.changed:
                self._print(Text(summary(result, False), style="bold green"))
                self._print(Text(_edited_apps(result, False), style="dim"))
                self._report_warnings(result)
            if options["output"]:
                save_plan(result, options["output"], blocked=show_blocked)
                if verbosity > 0:
                    self._print(
                        Text(
                            f"Saved the plan to {options['output']}. Apply it with:"
                            f" manage.py compact_migrations apply --plan"
                            f" {options['output']}",
                            style="dim",
                        )
                    )
            elif verbosity > 0 and result.changed:
                self._print(
                    Text("Run 'compact_migrations apply' to apply.", style="dim")
                )
            if options["check"] and result.changed:
                raise SystemExit(1)
            return
        if not result.changed:
            return
        # Warnings first: the summary says what was done, so it comes once
        # the files are written and verified.
        try:
            notes = result.check_worktree(allow_dirty=options["allow_dirty"])
            if not options["no_database_check"]:
                notes += result.check_databases()
        except CompactError as exc:
            raise CommandError(str(exc)) from None
        if verbosity > 0:
            self._report_warnings(result)
            for note in notes:
                self._print(Text(note, style="yellow"))
            self._print(Text(_edited_apps(result, True), style="dim"))
        try:
            result.write(allow_dirty=options["allow_dirty"])
        except CompactError as exc:
            raise CommandError(str(exc)) from None
        if verbosity > 0:
            self._print(Text(summary(result, True), style="bold green"))

    def _analyse(self, options: dict[str, Any], verbosity: int) -> Result:
        if verbosity > 0 and not options["static"]:
            self._print(Text(_trial_preface(options["database"]), style="yellow"))
        try:
            if verbosity > 0 and not self.console.is_terminal:
                # No live status line: one plain line per run on stderr, so
                # logs show progress.
                err = self._stderr_console(options)
                last = [""]

                def plain(message: str, note: bool = False) -> None:
                    if note:
                        self._print(Text(message, style="dim"))
                        return
                    label = message.split(":", 1)[0]
                    if label != last[0]:
                        last[0] = label
                        err.print(message, soft_wrap=True, highlight=False)

                return self._plan(options, plain)
            if verbosity > 0:
                with self.console.status(
                    "Analysing migrations...", spinner="dots"
                ) as status:

                    def progress(message: str, note: bool = False) -> None:
                        if note:
                            self._print(Text(message, style="dim"))
                        else:
                            status.update(message)

                    return self._plan(options, progress)
            return self._plan(options, None)
        except CompactError as exc:
            raise CommandError(str(exc)) from None

    @staticmethod
    def _plan(options: dict[str, Any], progress: Any) -> Result:
        return plan(
            options["targets"],
            exclude=options["exclude"],
            skip_kinds=options["skip_kinds"] or (),
            elidable=options["elidable"],
            static=options["static"],
            database=options["database"],
            noop_data=options["noop_data"],
            prune=options["prune"],
            progress=progress,
        )

    def _report_trial(self, result: Result, verbosity: int) -> None:
        info = result.verification
        if info.get("mode") != "trial":
            if result.changed:
                self._print(
                    Text(
                        "Checked against Django's model state only, not against"
                        " a database (--static): without it, a trial migrates"
                        " scratch databases to check the plan.",
                        style="dim",
                    )
                )
            return
        line = (
            ("Trial (recorded in the plan): " if result.from_plan else "Trial: ")
            + f"{count_noun(info['runs'], 'migrate run')} on"
            f" {info.get('server') or info['vendor']} (database"
            f" {info['database']!r}) in {info['seconds']:.1f}s;"
            f" unlocked {count_noun(info['unlocked'], 'removal')} of"
            f" {info['at_risk']} that the name scan blocks"
        )
        if info["trial_blocked"]:
            line += f", blocked {info['trial_blocked']} on evidence"
        if info.get("noop") is not None:
            line += (
                f"; {count_noun(len(info['noop']), 'data migration operation')}"
                f" did nothing on a fresh {info.get('server') or info['vendor']}"
                " database"
            )
        self._print(Text(line + ".", style="bold"))
        if info["not_trialled"]:
            self._print(
                Text(
                    "Database routers keep these data migrations from running on"
                    f" {info['database']!r}, so the trial can't check them and"
                    " what they mention is kept: "
                    + ", ".join(info["not_trialled"])
                    + ".",
                    style="yellow",
                )
            )
        if info["fallback"]:
            self._print(
                Text(
                    "The trial didn't converge, so this is the static plan,"
                    " which passed its own trial run.",
                    style="yellow",
                )
            )
        if info["vendor"] == "sqlite":
            self._print(
                Text(
                    "SQLite trials can't compare the names of unique and"
                    " foreign key constraints, or what check constraints check;"
                    " if production doesn't run on SQLite, trial on its backend"
                    " with --database.",
                    style="yellow",
                )
            )
        masked = list(info["nondeterministic_tables"]) + [
            f"{table}.{column}"
            for table, columns in info.get("nondeterministic_columns", {}).items()
            for column in columns
        ]
        if masked:
            self._print(
                Text(
                    "Differs between runs of the original history (random or"
                    " time-based values?), so not compared: " + ", ".join(masked) + ".",
                    style="yellow",
                )
            )
        if verbosity >= 2:
            for failure in info["failures"]:
                self._print(
                    Text(
                        f"  harvested: {failure['migration'][0]}."
                        f"{failure['migration'][1]} {failure['operation']}:"
                        f" {failure['error']}",
                        style="dim",
                    )
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
        self._report_trial(result, verbosity)
        if not result.changed:
            self._print("No operations to remove.")

    def _report_warnings(self, result: Result) -> None:
        made_for = sorted(result.options["backends"])
        now = sorted(backends.configured())
        if made_for != now:
            self._print(
                Text(
                    "This plan was made with "
                    + (backends.describe(made_for) or "no MySQL or MariaDB")
                    + " database configured, and "
                    + (backends.describe(now) or "none")
                    + " is configured now: some index names on MySQL and"
                    " MariaDB depend on the history, so make the plan again.",
                    style="yellow",
                )
            )
        elif not made_for:
            self._print(
                Text(
                    "No MySQL or MariaDB database is configured: if production"
                    " runs on one, make the plan with it configured (some of"
                    " its index names depend on the history).",
                    style="dim",
                )
            )
        if result.verification.get("mode") != "trial" and any(
            r.kind == "elidable" for r in result.removals
        ):
            self._print(
                Text(
                    "Elidable operations are removed on trust: one that writes"
                    " on a fresh database (a content type, a seed row) changes"
                    " the data and later ids; a trial (without --static)"
                    " checks it.",
                    style="yellow",
                )
            )

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
        help="app_label, app_label.Model or app_label.Model.field to compact.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="TARGET",
        help="Never remove anything matching this target (repeatable).",
    )
    parser.add_argument(
        "--skip-kind",
        action="append",
        choices=KINDS,
        dest="skip_kinds",
        help="Don't consider this kind of removal, e.g. fold (repeatable).",
    )
    parser.add_argument(
        "--elidable",
        action="store_true",
        help="Also remove every elidable RunPython/RunSQL operation.",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Delete migrations left with no operations, and make the"
        " migrations depending on them depend on their dependencies instead."
        " Changes the set of migration names.",
    )


def _add_common_arguments(parser: ArgumentParser, main: ArgumentParser) -> None:
    """
    Accept Django's display options after the subcommand too
    (``compact_migrations plan -v 2``), with Django's help; SUPPRESS keeps
    the values given before it when they aren't repeated.
    """
    django_help = {
        option: action.help
        for action in main._actions
        for option in action.option_strings
    }
    parser.add_argument(
        "-v",
        "--verbosity",
        type=int,
        choices=[0, 1, 2, 3],
        default=argparse.SUPPRESS,
        help=django_help.get("--verbosity"),
    )
    for flag in ("--traceback", "--no-color", "--force-color", "--skip-checks"):
        parser.add_argument(
            flag,
            action="store_true",
            default=argparse.SUPPRESS,
            help=django_help.get(flag),
        )
    # Read by manage.py before parsing; accepted here so they can follow the
    # subcommand too.
    for flag in ("--settings", "--pythonpath"):
        parser.add_argument(flag, default=argparse.SUPPRESS, help=django_help.get(flag))


def _add_trial_arguments(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--static",
        action="store_true",
        help="Don't run a trial: check the plan against Django's model state"
        " only, without migrating scratch databases (fewer removals, no"
        " database needed).",
    )
    parser.add_argument(
        "--database",
        default="default",
        help="Database alias whose backend and settings trial runs use"
        " (default: 'default').",
    )
    parser.add_argument(
        "--noop-data",
        action="store_true",
        help="Also remove RunPython/RunSQL operations that write nothing on a"
        " fresh database, as a trial run observes them (not with --static)."
        " Their reverse code and any effect outside the database are lost.",
    )


def _has_selection(options: dict[str, Any]) -> bool:
    return bool(
        options["targets"]
        or options["exclude"]
        or options["skip_kinds"]
        or options["elidable"]
        or options["static"]
        or options["noop_data"]
        or options["prune"]
    )


def _trial_preface(alias: str) -> str:
    settings_dict = connections[alias].settings_dict
    where = settings_dict.get("HOST") or "localhost"
    if connections[alias].vendor == "sqlite":
        where = "files in the temporary directory"
    module = settings.SETTINGS_MODULE or os.environ.get(
        "DJANGO_SETTINGS_MODULE", "configured in code"
    )
    where += f"; settings {module}"
    return (
        f"The trial creates scratch databases next to {alias!r}"
        f" ({connections[alias].display_name}, {where}) and runs every"
        " migration there, data"
        " migrations included: whatever they do outside the database (email,"
        " files, network calls) happens for real."
    )


def _edited_apps(result: Result, write: bool) -> str:
    apps = sorted({c.migration[0] for c in result.files})
    verb = "Editing" if write else "Would edit"
    return f"{verb} migration files of: {', '.join(apps)}."


def _ops(count: int) -> str:
    return count_noun(count, "operation")


def _row(indent: int, removal: Removal, name: Text) -> Row:
    span = Text(removal.start, style="dim")
    if removal.end:
        span.append(" -> ", style="dim")
        span.append(removal.end, style="dim")
    # A pruned migration is a file, not operations.
    count = None if removal.kind == "prune" else len(removal.elements)
    return Row(indent, name, removal.kind, span, count, removal.note)


def _locations(removal: Removal, indent: int, verbosity: int) -> list[Text]:
    if verbosity < 2:
        return []
    lines = []
    for loc in (*removal.elements, *removal.targets):
        line = Text("  " * (indent + 1))
        # Only operations that could be located are ever removed.
        line.append(f"{loc.display_path()}:{loc.line}")
        line.append(f"  {loc.operation}", style="bold")
        if loc.detail:
            line.append(f" ({loc.detail})", style="dim")
        lines.append(line)
    return lines


def summary(result: Result, write: bool) -> str:
    """E.g. "Would remove 3 operations, fold 2 and edit 1 in 2 migration files."."""
    actions: list[str] = []
    for count, done, todo in (
        (result.removed_operations, "removed", "remove"),
        (result.folded_operations, "folded", "fold"),
        (result.edited_operations, "edited", "edit"),
    ):
        if count:
            verb = done if write else todo
            if not actions:
                verb = verb.capitalize() if write else f"Would {verb}"
            actions.append(f"{verb} {count_noun(count, 'operation')}")
    pruned = len(result.pruned_migrations)
    if actions:
        joined = (
            actions[0]
            if len(actions) == 1
            else ", ".join(actions[:-1]) + " and " + actions[-1]
        )
        text = joined + f" in {count_noun(len(result.files), 'migration file')}."
        if pruned:
            text += f" {count_noun(pruned, 'migration')} " + (
                "deleted." if write else "would be deleted."
            )
    else:
        text = f"{count_noun(pruned, 'migration')} " + (
            "deleted." if write else "would be deleted."
        )
    noop = sum(1 for r in result.removals if r.kind == "noop")
    if noop:
        text += (
            f" Of the operations removed, {noop}"
            f" {'is a data migration' if noop == 1 else 'are data migrations'}"
            " that did nothing on a fresh database."
        )
    empty = len(result.empty_migrations)
    if empty:
        text += f" {count_noun(empty, 'migration')} "
        if write:
            text += (
                "now has no operations." if empty == 1 else "now have no operations."
            )
        else:
            text += "would be left with no operations."
    return text
