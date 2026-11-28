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

from django.db import OperationalError
from unittest_parametrize import parametrize

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
