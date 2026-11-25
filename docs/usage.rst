Usage
=====

django-compact-migrations adds a management command, ``compact_migrations``, with two subcommands:

* ``compact_migrations plan`` reports what can be removed, and can save the plan to a file.
* ``compact_migrations apply`` edits the migration files, from a saved plan or after making one.

The command reads your whole migration history and removes operations that cancel each other out, such as a model that is created and later deleted.
It also *folds* operations: it merges an operation into the earlier one that created what it changes.
With ``--prune``, it also deletes migrations left empty.
A *trial* checks every plan: the command migrates scratch databases with the original history and with the plan, and compares the results.

.. _before-you-start:

Before you start
----------------

**Migrate every existing database (production, staging, shared development databases) to your latest migrations first.**
Django never runs a migration again once it has recorded it as applied, so fully migrated databases are unaffected.
A database that is partway through the history when you apply may end up with a different schema.
The same rule applies when you delete the migrations that a squashed migration replaces.
``apply`` checks the databases it can reach, and refuses if one is partway through an app’s history.

**Commit your work.**
``apply`` refuses to edit migration files with uncommitted changes, so you can review and revert its edit on its own.

**Run the command with development settings.**
A trial creates scratch databases on the server of the ``--database`` alias (``default`` by default).
It runs your migrations there, data migrations included.
Anything a data migration does outside the database, such as sending email, happens for real.
So point ``--database`` at a local server of the same backend as production, never at production.
The database user needs permission to create databases, as for running tests.

The command only edits your project’s migrations: those under the top of the Git work tree you run it in (or the current directory, outside Git), outside virtual environments.
It reads the migrations of installed packages, but never edits them.
See :ref:`project-migrations` for the exact rules.
If the migrations that a squashed migration replaces are still on disk, the command skips them with a warning, and compacts only the squashed migration.

.. _what-may-change:

What may change
---------------

A database migrated from scratch with the compacted history has the same tables, columns, indexes, constraints and rows as one migrated with the original history, all under the same names.
Three things may differ, as they may after squashing migrations:

* **The order of columns in a table.**
  A field folded into an earlier operation is created where that operation puts it.
  On SQLite, Django also rebuilds tables with their columns in the order of the model’s fields, which ``AlterField`` and ``RenameField`` change.
  The order of fields in the historical models that data migrations get may change in the same way.
  Django always names the columns it reads and writes, so this only matters to raw SQL that relies on column positions: ``SELECT *`` read by index, or ``INSERT`` or ``COPY`` without a column list.
* **The ids of content types and permissions.**
  They depend on which rows the history happened to create first.
  Rows that point at them with a foreign key still point at the same content type or permission.
* **The next id of a table**: the value of its sequence or ``AUTO_INCREMENT`` counter, when a removed data migration created rows and then deleted them.

If you need the order of columns preserved, please `open an issue <https://github.com/adamchainz/django-compact-migrations/issues>`__: an option for that would be welcome.

Quick start
-----------

1. Make a plan and save it:

   .. code-block:: console

       $ ./manage.py compact_migrations plan --output plan.toml

2. Review the report, and the diffs in ``plan.toml``.
3. Apply exactly that plan:

   .. code-block:: console

       $ ./manage.py compact_migrations apply --plan plan.toml

4. Run your code formatter, and remove the imports that edited migrations no longer use, for example with `Ruff <https://docs.astral.sh/ruff/>`__:

   .. code-block:: console

       $ ruff check --select F401 --fix */migrations/

5. Run your test suite, which migrates a fresh test database through the new history.
6. Commit and deploy.
   On every existing database, ``migrate`` should then report “No migrations to apply.”

To undo, revert the commit.
Fully migrated databases need nothing more, since every original migration is still recorded as applied there.
The one exception is pruning: see :ref:`pruning`.

Reading the report
------------------

Here is a report for a small project:

.. code-block:: console

    $ ./manage.py compact_migrations plan
    The trial creates scratch databases next to 'default' (SQLite, files in the temporary directory; settings example.settings) and runs every migration there, data migrations included: whatever they do outside the database (email, files, network calls) happens for real.
    The original history migrated in 0s, and each run takes about as long. A trial can unlock up to 0 removals that the name scan blocks, in at most 2 more runs (about 0s at most).
    Nothing to unlock: only the static plan is trialled.
    blog
      Post
        spam_score      field       0001_initial -> 0002_tidy               2 operations
        draft_notes     field       0001_initial -> 0002_tidy               2 operations
        post_title_idx  index       0001_initial -> 0002_tidy               2 operations
    shop
      RunPython         elidable    0003_widen_sku                           1 operation  (mentions 'coupon', removed with shop.Coupon)
      Coupon            model       0002_coupon_legacy_ref -> 0004_cleanup  2 operations
      Product
        legacy_ref      field       0002_coupon_legacy_ref -> 0004_cleanup  2 operations
        sku             superseded  0003_widen_sku -> 0004_cleanup           1 operation
        sku             fold        0004_cleanup -> 0001_initial             1 operation
    Trial: 3 migrate runs on SQLite (database 'default') in 0.6s; unlocked 0 removals of 0 that the name scan blocks.
    SQLite trials can't compare the names of unique and foreign key constraints, or what check constraints check; if production doesn't run on SQLite, trial on its backend with --database.
    Would remove 9 operations, fold 1 operation and edit 2 operations in 6 migration files. 4 migrations would be left with no operations.
    Would edit migration files of: blog, shop.
    No MySQL or MariaDB database is configured: if production runs on one, make the plan with it configured (some of its index names depend on the history).
    Run 'compact_migrations apply' to apply.

The notes before the report say what the trial will do.
Removals are grouped by app and model.
Each line shows what goes, its kind, the migrations where it appeared and where it ended, and how many operations it takes.
For a fold, the line shows the migration it moves into instead.
The *Trial* line says how the plan was checked.
The summary counts the operations removed outright, folded into earlier ones, and edited, and the migrations left with no operations.
An edited operation is one that lost part of itself, such as a field taken out of a ``CreateModel``, or one that absorbed a fold.
Progress goes to stderr.

``-v 2`` also lists each operation as ``path:line  Operation``.
It also lists what cancels out in the history but was kept, with the reason, for example “RunPython in shop.0002_coupon mentions 'rank'”.
``-v 0`` prints nothing.

Options
-------

The defaults are the safe choices.
These options change what is removed, or how it is checked:

.. list-table::
   :header-rows: 1
   :widths: 25 15 60

   * - Option
     - Risk
     - What you accept
   * - (none)
     - safe by default
     - The trial checks the plan on empty scratch databases of one backend.
   * - targets, ``--exclude``, ``--skip-kind``
     - safe
     - Less is removed.
   * - ``--static``
     - opt-in risk
     - No trial: the plan is checked against Django’s model state only, and data migrations are judged by the names they mention.
   * - ``--elidable``
     - opt-in risk
     - Every elidable data migration goes, seed data included.
   * - ``--noop-data``
     - opt-in risk
     - Data migrations that wrote nothing in the trial go: evidence from one backend, their reverse code and any effect outside the database are lost.
   * - ``--prune``
     - opt-in risk
     - Migration names change: branches, other projects and scripts that name migrations need updating.
   * - ``--allow-dirty``, ``--no-database-check``
     - opt-in risk
     - Files with uncommitted changes are edited; partly migrated databases aren’t looked for.

Targets and ``--exclude``
~~~~~~~~~~~~~~~~~~~~~~~~~

Pass targets to compact part of your project: an app label, ``app.Model``, or ``app.Model.field``.

.. code-block:: console

    $ ./manage.py compact_migrations plan shop.Coupon

A model or field matches if any name it had in the history matches.
A target that matches no app, model or field in the history is an error.
Removing a target may need other things to go too, such as a foreign key in another app that points at a removed model.
If they can’t go, the target stays.
A target also makes its app editable when its migrations are outside the project.

``--exclude`` takes the same targets, and can be repeated.
It keeps what matches, and everything that depends on it.

``--skip-kind``
~~~~~~~~~~~~~~~

The command looks for every kind of removal.
``--skip-kind``, repeatable, leaves one out:

* ``model``: a model created and later deleted, with every operation on it.
* ``field``: a field added and later removed, with its ``AlterField`` and ``RenameField`` operations.
* ``index`` and ``constraint``: an index or constraint added and later removed.
* ``together``: a ``unique_together`` or ``index_together`` entry added and later removed.
* ``superseded``: an ``AlterField``, ``AlterModelOptions``, ``AlterModelManagers`` or ``AlterModelTableComment`` that a later one replaces.
* ``fold``: an operation merged into the earlier operation that created what it changes, such as an ``AlterField`` into the field’s ``AddField``.
  Django’s migration writer rewrites the earlier operation, so it loses its own formatting and comments.

The report can also show ``elidable``, ``noop`` and ``prune``, described below.
:doc:`how_it_works` gives the rules for each kind.

``--static``
~~~~~~~~~~~~

Skips the trial.
The plan is then checked against Django’s model state only, not against a database, and the report says so.
Data migrations (``RunPython``, ``RunSQL`` and custom operations) are judged by the names in their code: anything they mention is kept.
Use it when no database server is available.

``--database``
~~~~~~~~~~~~~~

The alias whose server the trial uses for its scratch databases (``default`` by default).
Use your production backend: a trial on SQLite can’t compare constraint names.
The rules for MySQL and MariaDB apply when any configured database uses them, and the report says when none does.

``--output`` and ``apply --plan``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``plan --output FILE`` (or ``-o``) saves the plan as a TOML file.
It holds the summary, the options, how the plan was checked, each removal, and each file edit as a unified diff.
With ``-v 2`` it also lists what was kept and why.

``apply --plan FILE`` applies exactly that plan.
It writes nothing if a file changed since the plan was made, if a diff doesn’t apply exactly, or if the plan names anything but the project’s own migrations.
For a ``--static`` plan, making the plan again must also give the same edits.
For a trial plan, the report shows the trial’s results as “recorded in the plan”.
Only apply plans that you made, or whose diffs you reviewed.

``--check``
~~~~~~~~~~~

``plan --check`` exits with status 1 when anything can be removed, so CI can fail when redundant operations creep in.
As with ``makemigrations --check``, an error also exits with status 1.

``--elidable``
~~~~~~~~~~~~~~

An elidable ``RunPython`` or ``RunSQL`` operation never blocks a removal.
If it mentions what is removed, and always runs while that exists, it is removed too (kind ``elidable``).
With ``--elidable``, every elidable operation of the selected apps goes, as when squashing.

``--noop-data``
~~~~~~~~~~~~~~~

Many data migrations only change rows that already exist.
On a fresh database they do nothing, yet they keep what they mention.
``--noop-data`` removes the data migrations that wrote nothing during the trial (kind ``noop``).
It keeps those whose code shows effects that queries don’t, such as files, email or signal handlers.
It can’t be combined with ``--static``.

.. warning::

    The evidence comes from the trial’s backend only, and from fresh databases only.
    Effects outside the database can’t be seen, and the reverse code goes with the operation.
    Libraries that support several backends shouldn’t use it.
    Review the removed operations before applying.

.. _pruning:

``--prune``
~~~~~~~~~~~

Deletes migrations left with no operations (kind ``prune``).
The migrations that depended on them then depend on their dependencies instead.
Every remaining migration still runs after the same migrations as before, in the same order, and every app keeps its first and latest migrations.
Some empty migrations are kept, with the reason shown by ``-v 2``.

.. warning::

    Pruning changes the set of migration names.
    Branches or other projects with migrations that depend on a deleted migration need that dependency updated.
    Scripts and documentation that name migrations need updating too.

Existing databases keep a ``django_migrations`` row for each deleted migration, which is harmless.
``migrate --prune`` removes those rows.
But if you then revert the change, ``migrate`` would run the restored migrations again.
So only run ``migrate --prune`` once the change is deployed everywhere and you won’t revert it.
Otherwise, mark the restored migrations as applied with ``./manage.py migrate --fake <app> <migration>``.

``--allow-dirty`` and ``--no-database-check``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``apply`` refuses to edit files with uncommitted changes, and says when they aren’t in Git at all.
``--allow-dirty`` edits them anyway.

``apply`` also refuses when a configured database it can reach has applied some of an app’s migrations, but not one that the plan edits or deletes.
``--no-database-check`` skips that check, for example when the databases can’t be reached from where you run the command.

``apply`` checks that it can write every file before it writes any.
It keeps the originals until the result is verified, and restores them on failure.
If it is killed in between, restore the files with Git.

Scratch databases
~~~~~~~~~~~~~~~~~

Trial runs drop their scratch databases when they end, even on Ctrl+C.
A scratch database is named like Django’s test databases, but with its own prefix: ``compactmig_<name>``.
Only a hard kill can leave one behind, and the next trial replaces it.
So don’t run two trials for the same database at the same time.
For SQLite, a scratch database is a file in a ``compactmig_…`` directory in the temporary directory.

Full help
---------

The full help for each subcommand:

``plan``
~~~~~~~~

.. [[[cog
.. import cog
.. import os
.. import subprocess
.. import sys
.. def show_help(subcommand):
..     result = subprocess.run(
..         [
..             sys.executable,
..             "-c",
..             "from django.core.management import execute_from_command_line as e;"
..             f"e(['manage.py', 'compact_migrations', '{subcommand}', '--help'])",
..         ],
..         capture_output=True,
..         text=True,
..         check=True,
..         env={**os.environ, "COLUMNS": "80", "DJANGO_SETTINGS_MODULE": "tests.settings"},
..     )
..     cog.outl("")
..     cog.outl(".. code-block:: console")
..     cog.outl("")
..     for line in result.stdout.splitlines():
..         if line.strip() == "":
..             cog.outl("")
..         else:
..             cog.outl("    " + line.rstrip())
..     cog.outl("")
.. show_help("plan")
.. ]]]

.. code-block:: console

    usage: manage.py compact_migrations plan [-h] [--exclude TARGET]
                                             [--skip-kind {model,field,index,constraint,together,superseded,fold}]
                                             [--elidable] [--prune] [--static]
                                             [--database DATABASE] [--noop-data]
                                             [-o FILE] [--check] [-v {0,1,2,3}]
                                             [--traceback] [--no-color]
                                             [--force-color] [--skip-checks]
                                             [--settings SETTINGS]
                                             [--pythonpath PYTHONPATH]
                                             [target ...]

    Analyse the migration history and report what can be removed. Nothing is
    written, except the plan file with --output.

    positional arguments:
      target                app_label, app_label.Model or app_label.Model.field to
                            compact.

    options:
      -h, --help            show this help message and exit
      --exclude TARGET      Never remove anything matching this target
                            (repeatable).
      --skip-kind {model,field,index,constraint,together,superseded,fold}
                            Don't consider this kind of removal, e.g. fold
                            (repeatable).
      --elidable            Also remove every elidable RunPython/RunSQL operation.
      --prune               Delete migrations left with no operations, and make
                            the migrations depending on them depend on their
                            dependencies instead. Changes the set of migration
                            names.
      --static              Don't run a trial: check the plan against Django's
                            model state only, without migrating scratch databases
                            (fewer removals, no database needed).
      --database DATABASE   Database alias whose backend and settings trial runs
                            use (default: 'default').
      --noop-data           Also remove RunPython/RunSQL operations that write
                            nothing on a fresh database, as a trial run observes
                            them (not with --static). Their reverse code and any
                            effect outside the database are lost.
      -o, --output FILE     Save the plan as a TOML file, to review and then
                            apply.
      --check               Exit with status 1 if anything is removable.
      -v, --verbosity {0,1,2,3}
                            Verbosity level; 0=minimal output, 1=normal output,
                            2=verbose output, 3=very verbose output
      --traceback           Display a full stack trace on CommandError exceptions.
      --no-color            Don't colorize the command output.
      --force-color         Force colorization of the command output.
      --skip-checks         Skip system checks.
      --settings SETTINGS   The Python path to a settings module, e.g.
                            "myproject.settings.main". If this isn't provided, the
                            DJANGO_SETTINGS_MODULE environment variable will be
                            used.
      --pythonpath PYTHONPATH
                            A directory to add to the Python path, e.g.
                            "/home/djangoprojects/myproject".

.. [[[end]]]

``apply``
~~~~~~~~~

.. [[[cog
.. show_help("apply")
.. ]]]

.. code-block:: console

    usage: manage.py compact_migrations apply [-h] [--plan FILE]
                                              [--no-database-check]
                                              [--allow-dirty] [--exclude TARGET]
                                              [--skip-kind {model,field,index,constraint,together,superseded,fold}]
                                              [--elidable] [--prune] [--static]
                                              [--database DATABASE] [--noop-data]
                                              [-v {0,1,2,3}] [--traceback]
                                              [--no-color] [--force-color]
                                              [--skip-checks]
                                              [--settings SETTINGS]
                                              [--pythonpath PYTHONPATH]
                                              [target ...]

    Apply a saved plan (--plan FILE), or analyse and apply in one go. The result
    is verified on disk and rolled back on failure.

    positional arguments:
      target                app_label, app_label.Model or app_label.Model.field to
                            compact.

    options:
      -h, --help            show this help message and exit
      --plan FILE           A plan file saved by 'plan --output'. Refused if any
                            migration file changed since.
      --no-database-check   Don't check that the configured databases have applied
                            every migration this edits or deletes.
      --allow-dirty         Edit migration files that have changes Git hasn't
                            committed (refused by default, so the edit can be
                            reviewed and reverted on its own).
      --exclude TARGET      Never remove anything matching this target
                            (repeatable).
      --skip-kind {model,field,index,constraint,together,superseded,fold}
                            Don't consider this kind of removal, e.g. fold
                            (repeatable).
      --elidable            Also remove every elidable RunPython/RunSQL operation.
      --prune               Delete migrations left with no operations, and make
                            the migrations depending on them depend on their
                            dependencies instead. Changes the set of migration
                            names.
      --static              Don't run a trial: check the plan against Django's
                            model state only, without migrating scratch databases
                            (fewer removals, no database needed).
      --database DATABASE   Database alias whose backend and settings trial runs
                            use (default: 'default').
      --noop-data           Also remove RunPython/RunSQL operations that write
                            nothing on a fresh database, as a trial run observes
                            them (not with --static). Their reverse code and any
                            effect outside the database are lost.
      -v, --verbosity {0,1,2,3}
                            Verbosity level; 0=minimal output, 1=normal output,
                            2=verbose output, 3=very verbose output
      --traceback           Display a full stack trace on CommandError exceptions.
      --no-color            Don't colorize the command output.
      --force-color         Force colorization of the command output.
      --skip-checks         Skip system checks.
      --settings SETTINGS   The Python path to a settings module, e.g.
                            "myproject.settings.main". If this isn't provided, the
                            DJANGO_SETTINGS_MODULE environment variable will be
                            used.
      --pythonpath PYTHONPATH
                            A directory to add to the Python path, e.g.
                            "/home/djangoprojects/myproject".

.. [[[end]]]

.. _verifying-your-schema:

Verifying your schema
---------------------

For more confidence, compare the schema that migrations create before and after, on fresh databases of your production backend.
For example, with PostgreSQL, before applying:

.. code-block:: console

    $ createdb compact_check
    $ DATABASE_URL=postgres:///compact_check ./manage.py migrate
    $ pg_dump --schema-only --no-owner compact_check > before.sql

Then apply, and repeat on a new database to get ``after.sql``.
``diff before.sql after.sql`` should show nothing.
