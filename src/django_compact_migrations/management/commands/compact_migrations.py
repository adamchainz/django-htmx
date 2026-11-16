from __future__ import annotations

import argparse
import itertools
import json
import sys
from argparse import ArgumentParser
from dataclasses import dataclass
from typing import Any

from django.core.management.base import CommandError
from django.db import connections
from django_rich.management import RichCommand
from rich.console import Console
from rich.table import Table
from rich.text import Text

from django_compact_migrations.core import (
    CompactError,
    Removal,
    Result,
    count_noun,
    plan,
)
from django_compact_migrations.planfile import load_plan, save_plan
from django_compact_migrations.profile import Profile, TypeStats, profile
from django_compact_migrations.scratch import (
    ScratchDatabaseError,
    drop_leftover,
    list_leftovers,
)
from django_compact_migrations.solver import KINDS, OPAQUE_POLICIES

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
        "Remove migration operations that cancel out over the whole history,"
        " without changing the final schema."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        subparsers = parser.add_subparsers(
            dest="subcommand",
            metavar="{plan,apply,profile,scratch}",
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
        _add_trial_arguments(apply_parser)
        _add_show_blocked(apply_parser)
        _add_common_arguments(apply_parser)
        profile_parser = subparsers.add_parser(
            "profile",
            help="Migrate a scratch database from scratch and report where the"
            " time goes.",
            description="Create an empty scratch database, run the whole"
            " migration history on it, report timings, and drop it. The real"
            " database is never touched.",
        )
        profile_parser.add_argument(
            "--database",
            default="default",
            help="Database alias whose backend and settings to use (default:"
            " 'default').",
        )
        profile_parser.add_argument(
            "--top",
            type=int,
            default=10,
            metavar="N",
            help="How many of the slowest migrations to list (default 10).",
        )
        profile_parser.add_argument(
            "--json", action="store_true", help="Print the report as JSON."
        )
        _add_common_arguments(profile_parser)
        scratch_parser = subparsers.add_parser(
            "scratch",
            help="List (or drop) scratch databases left by interrupted runs.",
            description="Trial and profile runs drop their scratch databases"
            " (named compactmig_...) when they end, even on Ctrl-C or SIGTERM."
            " A killed process can leave one behind: list them, and drop those"
            " that no running command is using.",
        )
        scratch_parser.add_argument(
            "--database",
            default="default",
            help="Database alias whose server to look on (default: 'default').",
        )
        scratch_parser.add_argument(
            "--drop",
            action="append",
            default=[],
            metavar="NAME",
            help="Drop this scratch database (repeatable).",
        )
        scratch_parser.add_argument(
            "--drop-all",
            action="store_true",
            help="Drop every scratch database found. Only when no other"
            " trial or profile run is going on against this server.",
        )
        _add_common_arguments(scratch_parser)

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
        if subcommand == "profile":
            self._profile(options, verbosity)
            return
        if subcommand == "scratch":
            self._scratch(options)
            return
        show_blocked = options["show_blocked"] or verbosity >= 2
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
        if subcommand == "plan":
            self._report(result, verbosity, show_blocked, False, options)
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
        self._report(result, verbosity, show_blocked, True, options)
        if result.changed:
            try:
                result.write()
            except CompactError as exc:
                raise CommandError(str(exc)) from None

    def _scratch(self, options: dict[str, Any]) -> None:
        alias = options["database"]
        try:
            found = list_leftovers(alias)
            names = found if options["drop_all"] else options["drop"]
            for name in names:
                drop_leftover(name, alias)
                self._print(f"Dropped {name}.")
        except ScratchDatabaseError as exc:
            raise CommandError(str(exc)) from None
        if not names:
            if found:
                for name in found:
                    self._print(name)
            else:
                self._print("No scratch databases found.")

    def _profile(self, options: dict[str, Any], verbosity: int) -> None:
        def progress(
            event: str, key: tuple[str, str] | None, done: int, total: int
        ) -> None:
            if key is not None and status is not None:
                status.update(f"Migrating {key[0]}.{key[1]} ({done + 1}/{total})...")

        try:
            if verbosity > 0 and not options["json"]:
                with self.console.status(
                    "Creating a scratch database...", spinner="dots"
                ) as status:
                    result = profile(options["database"], progress)
            else:
                status = None
                result = profile(options["database"], progress)
        except CompactError as exc:
            raise CommandError(str(exc)) from None
        if options["json"]:
            self.stdout.write(json.dumps(result.as_dict(options["top"]), indent=2))
            return
        if verbosity > 0:
            self._report_profile(result, options["top"])

    def _report_profile(self, result: Profile, top: int) -> None:
        total = result.total or 1e-9

        def pct(seconds: float) -> str:
            return f"{100 * seconds / total:.1f}%"

        def table(title: str, first: str) -> Table:
            t = Table(title=title, title_justify="left")
            t.add_column(first)
            for name in ("Count", "Database", "State", "Total", "Share"):
                t.add_column(name, justify="right")
            return t

        def row(t: Table, name: str, stats: TypeStats) -> None:
            t.add_row(
                name,
                str(stats.count),
                f"{stats.seconds:.3f}",
                f"{stats.state_seconds:.3f}",
                f"{stats.total:.3f}",
                pct(stats.total),
            )

        self._print(
            Text(
                f"Migrated {len(result.migrations)} migrations in"
                f" {result.total:.2f}s on {result.server or result.vendor}:"
                f" {result.operations_total:.2f}s"
                f" in the database, {result.state_total:.2f}s updating the"
                f" project state, {result.overhead:.2f}s other.",
                style="bold",
            )
        )
        t = Table(title=f"Slowest {top} migrations", title_justify="left")
        t.add_column("Migration")
        for name in ("Operations", "Database", "State", "Seconds", "Share"):
            t.add_column(name, justify="right")
        for key, seconds in result.slowest(top):
            stats = result.by_migration.get(key, TypeStats())
            t.add_row(
                f"{key[0]}.{key[1]}",
                str(result.migration_ops.get(key, 0)),
                f"{stats.seconds:.3f}",
                f"{stats.state_seconds:.3f}",
                f"{seconds:.3f}",
                pct(seconds),
            )
        self._table(t)
        t = table("By operation type", "Operation")
        for name, stats in sorted(result.by_type.items(), key=lambda kv: -kv[1].total):
            row(t, name, stats)
        self._table(t)
        t = table("By app", "App")
        for app, stats in sorted(result.by_app.items(), key=lambda kv: -kv[1].total):
            row(t, app, stats)
        self._table(t)
        t = Table(
            title=f"Top {top} operations by project state cost",
            title_justify="left",
        )
        for name in ("Migration", "Operation", "State"):
            t.add_column(name, justify="right" if name == "State" else "left")
        for op in result.top_state[:top]:
            t.add_row(
                f"{op.migration[0]}.{op.migration[1]} #{op.index + 1}",
                op.operation,
                f"{op.state_seconds:.3f}",
            )
        self._table(t)
        if result.removable is None:
            self._print(
                Text(
                    f"Could not make the default plan: {result.plan_error}",
                    style="yellow",
                )
            )
        else:
            assert result.editable is not None
            self._print(
                Text(
                    f"The default plan removes {_ops(result.removable_count)}"
                    f" taking {result.removable:.3f}s ({pct(result.removable)}),"
                    f" and edits operations taking {result.editable:.3f}s"
                    f" ({pct(result.editable)}), counting database and project"
                    " state time.",
                    style="bold green",
                )
            )
        for note in result.notes:
            self._print(Text(note, style="dim"))

    def _analyse(self, options: dict[str, Any], verbosity: int) -> Result:
        if options["noop_data"] and options["opaque"] != "scan":
            raise CommandError(
                "--noop-data implies --trial, which can't be combined with --opaque."
            )
        if options["trial"] and options["opaque"] != "scan":
            raise CommandError(
                "--trial replaces the --opaque policy with trial runs; don't"
                " combine them."
            )
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
            kinds=options["kinds"],
            skip_kinds=options["skip_kinds"] or (),
            opaque=options["opaque"],
            elidable=options["elidable"],
            trial=options["trial"],
            database=options["database"],
            trial_max_runs=options["trial_max_runs"],
            trial_single_baseline=options["trial_single_baseline"],
            trial_ignore_column_order=options["trial_ignore_column_order"],
            trial_post_migrate=options["trial_post_migrate"],
            noop_data=options["noop_data"],
            prune=options["prune"],
            progress=progress,
        )

    def _report_trial(self, result: Result, verbosity: int) -> None:
        info = result.verification
        if info.get("mode") != "trial":
            return
        line = (
            f"Trial: {count_noun(info['runs'], 'migrate run')} on"
            f" {info.get('server') or info['vendor']} in {info['seconds']:.1f}s;"
            f" unlocked {count_noun(info['unlocked'], 'removal')} of"
            f" {info['at_risk']} that the static scan blocks"
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
                    "SQLite trials can't compare constraint names or column"
                    " order; if production doesn't run on SQLite, trial on its"
                    " backend with --database.",
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

    def _table(self, table: Table) -> None:
        # Not on a terminal (a pipe, a file, CI logs), rich would assume 80
        # columns and cut the tables: let them take the room they need.
        if self.console.is_terminal:
            self.console.print(table)
        else:
            Console(file=self.console.file, width=1000, color_system=None).print(
                table, crop=False
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
        self._report_trial(result, verbosity)
        if not result.changed:
            self._print("No operations to remove.")
            return
        self._print(Text(summary(result, write), style="bold green"))
        order_warning = sqlite_order_warning(
            result, options.get("database") or "default"
        )
        if order_warning:
            self._print(Text(order_warning, style="yellow"))
        if result.verification.get("mode") != "trial" and any(
            r.kind == "elidable" for r in result.removals
        ):
            self._print(
                Text(
                    "Elidable operations are removed on trust: one that writes"
                    " on a fresh database (a content type, a seed row) changes"
                    " the data and later ids; --trial checks it.",
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
        "--kind",
        action="append",
        choices=KINDS,
        dest="kinds",
        help="Only consider these kinds of removal (repeatable).",
    )
    parser.add_argument(
        "--skip-kind",
        action="append",
        choices=KINDS,
        dest="skip_kinds",
        help="Don't consider this kind of removal, e.g. fold (repeatable).",
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
        "--prune",
        action="store_true",
        help="Delete migrations left with no operations, and make the"
        " migrations depending on them depend on their dependencies instead."
        " Changes the set of migration names.",
    )


def _add_common_arguments(parser: ArgumentParser) -> None:
    """
    Accept Django's display options after the subcommand too
    (``compact_migrations plan -v 2``); SUPPRESS keeps the values given before
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
    for flag in ("--traceback", "--no-color", "--force-color", "--skip-checks"):
        parser.add_argument(flag, action="store_true", default=argparse.SUPPRESS)
    # Read by manage.py before parsing; accepted here so they can follow the
    # subcommand too.
    for flag in ("--settings", "--pythonpath"):
        parser.add_argument(flag, default=argparse.SUPPRESS)


def _add_trial_arguments(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--trial",
        action="store_true",
        help="Verify by migrating scratch databases instead of relying on the"
        " name scan of data migrations; unlocks more removals.",
    )
    parser.add_argument(
        "--database",
        default="default",
        help="Database alias whose backend and settings trial runs use"
        " (default: 'default').",
    )
    parser.add_argument(
        "--trial-max-runs",
        type=int,
        default=8,
        metavar="N",
        help="Trial runs of candidate plans before falling back to the"
        " static plan (default 8).",
    )
    parser.add_argument(
        "--noop-data",
        action="store_true",
        help="Also remove RunPython/RunSQL operations that write nothing on a"
        " fresh database, as a trial run observes them (implies --trial)."
        " Their reverse code and any effect outside the database are lost.",
    )
    parser.add_argument(
        "--trial-post-migrate",
        action="store_true",
        help="Send post_migrate at the end of every trial run, as migrate"
        " does, so the content types and permissions it creates are compared"
        " too (their ids show rows the history created first).",
    )
    parser.add_argument(
        "--trial-ignore-column-order",
        action="store_true",
        help="On SQLite, don't compare the physical order of table columns,"
        " which removals and folds of AlterField and RenameField can change"
        " (other backends never move columns).",
    )
    parser.add_argument(
        "--trial-single-baseline",
        action="store_true",
        help="Migrate the original history once, not twice: saves a run, but"
        " random or time-based data it creates is then only recognised late,"
        " and may block removals.",
    )


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
        or options["skip_kinds"]
        or options["opaque"] != "scan"
        or options["elidable"]
        or options["trial"]
        or options["noop_data"]
        or options["prune"]
    )


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


def sqlite_order_warning(result: Result, alias: str) -> str | None:
    """
    On SQLite, Django rebuilds tables in the order the project state lists
    their fields, which AlterField and RenameField change: removing or
    folding operations can change the physical column order. Only a trial
    that compares it shows whether this plan does.
    """
    info = result.verification
    if alias not in connections or connections[alias].vendor != "sqlite":
        return None
    if info.get("mode") == "trial" and not result.options.get(
        "trial_ignore_column_order"
    ):
        return None
    # Removing dead fields or models doesn't reorder the columns that stay;
    # dropping or moving an AlterField or RenameField (or folding anything
    # past one) can.
    if not any(
        r.kind == "fold"
        or any(loc.operation in ("AlterField", "RenameField") for loc in r.elements)
        for r in result.removals
    ):
        return None
    return (
        "On SQLite, this plan may change the physical order of columns in"
        " rebuilt tables (harmless for Django); --trial checks it."
    )


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
