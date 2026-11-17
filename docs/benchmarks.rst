Benchmarks
==========

How much faster does ``migrate`` get?
On nine open source Django projects, compacting made migrating a fresh database 14% to 46% faster with the default options, and 15% to 51% faster with ``--noop-data --prune``.
In every case, the compacted history created the same database as the original.

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
     - 4.7
     - 3.4
     - −28%
     - 2.5
     - −47%
   * - `pretalx <https://github.com/pretalx/pretalx>`__
     - PostgreSQL
     - 18.8
     - 15.1
     - −20%
     - 10.5
     - −44%
   * - `pretix <https://github.com/pretix/pretix>`__
     - PostgreSQL
     - 82.3
     - 64.6
     - −22%
     - 57.2
     - −30%
   * - `Label Studio <https://github.com/HumanSignal/label-studio>`__
     - PostgreSQL
     - 31.2
     - 16.8
     - −46%
     - 15.4
     - −51%
   * - `Saleor <https://github.com/saleor/saleor>`__
     - PostgreSQL
     - 167.8
     - 128.6
     - −23%
     - 102.3
     - −39%
   * - `Wagtail <https://github.com/wagtail/wagtail>`__
     - SQLite
     - 52.2
     - 45.0
     - −14%
     - 44.5
     - −15%
   * - `Weblate <https://github.com/WeblateOrg/weblate>`__
     - PostgreSQL
     - 43.8
     - 30.0
     - −32%
     - 24.4
     - −44%
   * - `Zulip <https://github.com/zulip/zulip>`__
     - PostgreSQL
     - 42.4
     - 29.6
     - −30%
     - 23.6
     - −44%
   * - `NetBox <https://github.com/netbox-community/netbox>`__
     - PostgreSQL
     - 321.1
     - 218.8
     - −32%
     - 196.9
     - −39%

What the command changed to get there.
*Removed* and *folded* count operations, and *deleted* counts migration files, which only ``--prune`` deletes:

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
     - 34
     - 70
     - 78
     - 54
     - 92
   * - pretalx
     - 263
     - 64
     - 149
     - 137
     - 156
     - 128
   * - pretix
     - 432
     - 115
     - 317
     - 218
     - 312
     - 83
   * - Label Studio
     - 245
     - 183
     - 342
     - 216
     - 337
     - 113
   * - Saleor
     - 1454
     - 586
     - 427
     - 927
     - 416
     - 618
   * - Wagtail
     - 240
     - 65
     - 110
     - 67
     - 104
     - 67
   * - Weblate
     - 238
     - 67
     - 229
     - 121
     - 227
     - 106
   * - Zulip
     - 956
     - 34
     - 155
     - 98
     - 155
     - 153
   * - NetBox
     - 299
     - 330
     - 463
     - 397
     - 464
     - 77

The file counts include migrations that a squashed migration replaces and that are still on disk, which the command never edits: 698 of Zulip’s files and 157 of pretix’s.

Making a plan takes several times as long as one ``migrate``, because the trial migrates scratch databases repeatedly: between 3 and 9 runs in these benchmarks.

Method
------

Each project was compacted twice from a clean checkout: once with the default options (``plan --output``, then ``apply --plan``), and once with ``--noop-data --prune``.

*Timing.*
Each time is the median of three runs of ``migrate`` on a new, empty database (two runs for Saleor and NetBox).
Runs of the original and the two compacted histories were interleaved, one at a time, on an otherwise idle machine.

*Correctness.*
For each compacted history, a fresh database was compared with one made by the original history:

* the schema dump (``pg_dump --schema-only``, or a sorted dump of ``sqlite_master`` for SQLite),
* the ids of all content types and permissions,
* the output of ``makemigrations --check --dry-run``.

All were identical for all nine projects in both configurations, and ``migrate --check`` passed.

*Environment.*
4 virtual CPUs (Intel Xeon, 2.1GHz), 15GiB of memory, PostgreSQL 16.15 and SQLite 3.53.1 on the same machine, Python 3.13 (3.12 for Saleor).
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
