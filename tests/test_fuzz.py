"""
Random histories of one to three apps (see tests.fuzz): compacting one must never fail, and the
compacted history must build the same schema as the original.

Each seed is one history. ``COMPACT_FUZZ_SEEDS=5000`` runs more of them
(``COMPACT_FUZZ_FIRST=5000`` starts further on), and ``COMPACT_FUZZ_SEED=1234``
just that one.
"""

from __future__ import annotations

import os
from typing import Any

from django.db import DatabaseError, OperationalError
from django.db.migrations.executor import MigrationExecutor
from unittest_parametrize import parametrize

from django_compact_migrations.scratch import scratch_database
from django_compact_migrations.snapshot import Snapshot, take_snapshot
from tests.fuzz import Generator
from tests.project import ProjectTestCase

if "COMPACT_FUZZ_SEED" in os.environ:  # pragma: no cover
    SEEDS = [int(os.environ["COMPACT_FUZZ_SEED"])]
else:
    FIRST = int(os.environ.get("COMPACT_FUZZ_FIRST", "0"))
    SEEDS = list(range(FIRST, FIRST + int(os.environ.get("COMPACT_FUZZ_SEEDS", "100"))))


class FuzzTests(ProjectTestCase):
    @parametrize("seed", [(seed,) for seed in SEEDS])
    def test_static(self, seed):
        Generator(seed, renames_keep_names=True).write(self.project)
        self.project.check_equivalence()

    @parametrize("seed", [(seed,) for seed in SEEDS[::5]])
    def test_static_postgres(self, seed):
        # SQLite hides most differences in the names of indexes and
        # constraints, since Django rebuilds its tables: compare whole
        # snapshots on PostgreSQL too.
        pg = self.server("pg")
        Generator(seed).write(self.project)
        try:
            before = self.snapshot(pg)
        except DatabaseError as exc:  # pragma: no cover
            # Such as a row written and its table altered in one migration.
            self.skipTest(f"The history doesn't migrate on PostgreSQL: {exc}")
        self.project.compact().write()
        assert before.differences(self.snapshot(pg)) == []

    def snapshot(self, alias: str) -> Snapshot:
        self.project.activate()
        self.project.purge()
        with scratch_database(alias) as connection:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
            return take_snapshot(connection)

    def trial(self, seed: int, **options: Any) -> None:
        Generator(seed).write(self.project)
        try:
            self.project.migrate_schema()
        except OperationalError as exc:  # pragma: no cover
            # With every kind of rename, a few histories trip over SQLite
            # itself: an index that kept a renamed column's old name gets in
            # the way of a later change.
            self.skipTest(f"The history doesn't migrate on SQLite: {exc}")
        self.project.check_equivalence(static=False, **options)

    @parametrize("seed", [(seed,) for seed in SEEDS[::5]])
    def test_trial(self, seed):
        self.trial(seed)

    @parametrize("seed", [(seed,) for seed in SEEDS[::5]])
    def test_trial_noop_data_and_prune(self, seed):
        self.trial(seed, noop_data=True, prune=True)
