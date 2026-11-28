Benchmarks
==========

On nine open source Django projects, compacting made migrating a fresh database 11% to 50% faster with the default options, and 27% to 71% faster with ``--noop-data --prune``.
In every case, the compacted history created the same database as the original, apart from :ref:`what may change <what-may-change>`.

Results
-------

Time for ``migrate`` to create a fresh database, in seconds:

.. list-table::
   :header-rows: 1
   :widths: 22 13 13 13 13 13 13

   * - Project
     - Database
     - Original
     - Default
     - Change
     - ``--noop-data --prune``
     - Change
   * - `Healthchecks <https://github.com/healthchecks/healthchecks>`__
     - SQLite
     - 3.1
     - 1.7
     - −45%
     - 0.9
     - −71%
   * - `pretalx <https://github.com/pretalx/pretalx>`__
     - PostgreSQL
     - 14.2
     - 7.9
     - −44%
     - 5.1
     - −64%
   * - `pretix <https://github.com/pretix/pretix>`__
     - PostgreSQL
     - 83.3
     - 55.4
     - −33%
     - 46.1
     - −45%
   * - `Label Studio <https://github.com/HumanSignal/label-studio>`__
     - PostgreSQL
     - 27.3
     - 13.7
     - −50%
     - 13.5
     - −51%
   * - `Saleor <https://github.com/saleor/saleor>`__
     - PostgreSQL
     - 173.0
     - 123.2
     - −29%
     - 92.8
     - −46%
   * - `Wagtail <https://github.com/wagtail/wagtail>`__
     - SQLite
     - 50.3
     - 44.6
     - −11%
     - 36.5
     - −27%
   * - `Weblate <https://github.com/WeblateOrg/weblate>`__
     - PostgreSQL
     - 41.0
     - 23.4
     - −43%
     - 19.8
     - −52%
   * - `Zulip <https://github.com/zulip/zulip>`__
     - PostgreSQL
     - 38.0
     - 22.2
     - −42%
     - 18.0
     - −53%
   * - `NetBox <https://github.com/netbox-community/netbox>`__
     - PostgreSQL
     - 288.4
     - 151.9
     - −47%
     - 147.2
     - −49%

What the command changed to get there, where *removed* and *folded* count operations, and *deleted* counts migration files (only ``--prune`` deletes them):

.. list-table::
   :header-rows: 2
   :widths: 22 13 13 13 13 13 13

   * -
     - Migration
     - Default
     -
     - ``--noop-data --prune``
     -
     -
   * - Project
     - files
     - Removed
     - Folded
     - Removed
     - Folded
     - Deleted
   * - Healthchecks
     - 187
     - 62
     - 133
     - 111
     - 119
     - 163
   * - pretalx
     - 263
     - 63
     - 269
     - 134
     - 269
     - 177
   * - pretix
     - 432
     - 98
     - 492
     - 197
     - 484
     - 120
   * - Label Studio
     - 245
     - 148
     - 338
     - 178
     - 330
     - 120
   * - Saleor
     - 1454
     - 567
     - 650
     - 906
     - 657
     - 697
   * - Wagtail
     - 240
     - 73
     - 141
     - 87
     - 139
     - 86
   * - Weblate
     - 238
     - 89
     - 284
     - 144
     - 281
     - 146
   * - Zulip
     - 956
     - 39
     - 213
     - 103
     - 213
     - 207
   * - NetBox
     - 299
     - 328
     - 677
     - 395
     - 672
     - 124

The file counts include migrations that a squashed migration replaces and that are still on disk, which the command never edits: 698 of Zulip’s files and 157 of pretix’s.

Making a plan takes several times as long as one ``migrate``, because the trial migrates scratch databases repeatedly: between 3 and 9 runs in these benchmarks.

Method
------

Each project was compacted twice from a clean checkout: once with the default options (``plan --output``, then ``apply --plan``), and once with ``--noop-data --prune``.

*Timing.*
Each time is the median of three runs of ``migrate`` on a new, empty database (two runs for Saleor and NetBox).
Runs of the original and the two compacted histories were interleaved, one at a time, on an otherwise idle machine.
Wagtail’s runs varied the most (47 to 56 seconds for the original history), so its figures are the least certain.

*Correctness.*
Each compacted history’s fresh database was compared with one made by the original history:

* the schema dump (``pg_dump --schema-only``, or a dump of ``sqlite_master`` for SQLite), ignoring the order of columns within a table,
* the content types and permissions, by app label, model and codename rather than by id,
* the output of ``makemigrations --check --dry-run``.

All were identical for all nine projects in both configurations, and ``migrate --check`` passed.
The order of columns changed in every project, and the ids of content types and permissions changed in one (Zulip): see :ref:`what-may-change`.

*Environment.*
4 virtual CPUs (Intel Xeon, 2.3GHz), 15GiB of memory, PostgreSQL 16.15 and SQLite 3.53.1 on the same machine, Python 3.13 (3.12 for Saleor).
The projects were at their main branches as of November 2026, on the Django versions they pin (5.2, 6.0 or 6.1).

Caveats
-------

* Times depend on the machine: compare the changes, not the seconds.
* These are large, long-lived projects.
  A younger history has less to remove.
* The gain is for databases migrated from empty, such as test databases and new environments.
  Fully migrated databases are unaffected.
* The Healthchecks and Wagtail figures come from SQLite, where a trial can’t compare everything: see :doc:`usage`.
* ``--noop-data`` and ``--prune`` carry risks that the defaults don’t: see the options table in :doc:`usage`.
