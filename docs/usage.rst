Usage
=====

django-clean-migrations adds one management command, ``clean_migrations``, with subcommands:

* ``clean_migrations plan`` analyses the history and reports what can be removed, optionally saving a plan file.
* ``clean_migrations apply`` edits the migration files, either from a saved plan file or after analysing in one go.

Running ``clean_migrations`` without a subcommand prints the help.
It reads your whole migration history and removes operations that cancel each other out, such as a model that is created and later deleted, or a field that is added and later removed.
The migration files are edited in place: same files, same names, same dependencies, fewer operations.

Before you start
----------------

**Every database that will ever run your migrations must already be fully migrated.**
This is the same caveat as when squashing migrations.
The command edits migrations that other databases have already recorded as applied.
If a database is only partway through the history, removing operations from the middle can leave it with a schema that doesn’t match what the later migrations expect.

So, before running ``clean_migrations apply``:

* Make sure production, staging, and any shared databases are up to date.
* Commit your work, so you can review and revert the changes with Git.

And afterwards:

* Review the diff.
* Run your test suite, or at least create a fresh database with ``migrate`` and check it works.
  See :ref:`verifying-your-schema` for how to confirm the schema hasn’t changed.

The command runs its own checks before it writes anything, and again after (see :doc:`how_it_works`).
These checks make sure that the migration history still builds the same project state.
They can’t see your real database, so they are a safety net rather than a replacement for review.

Basic workflow
--------------

Run ``plan`` to see what would change.
It doesn’t touch any files:

.. code-block:: console

    $ python manage.py clean_migrations plan
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
    Would remove 11 operations and edit 1 operation in 5 migration files. 2 migrations would be left with no operations.
    Run 'clean_migrations apply' to apply.

Removals are grouped by app, then by model.
Each line names the thing that would be removed, its kind, the migrations where it appeared and disappeared, and how many operations are involved.
With colour enabled, kinds and names are highlighted and migration names are dimmed.
The kind column is one of the kinds listed below.
The summary counts operations removed outright, operations edited (for example a single field taken out of a ``CreateModel``), and migration files that end up with no operations at all.
Counts that are zero are left out.

When you are happy with the plan, apply it with ``apply``, which takes the same targets and options as ``plan``:

.. code-block:: console

    $ python manage.py clean_migrations apply
    ...
    Removed 11 operations and edited 1 operation in 5 migration files. 2 migrations now have no operations.

Running ``plan`` again then reports that there’s nothing left to do:

.. code-block:: console

    $ python manage.py clean_migrations plan
    No operations to remove.

Plan files
~~~~~~~~~~

``plan --output FILE`` saves the plan as a JSON file, and ``apply --plan FILE`` applies exactly that plan, without analysing the history again:

.. code-block:: console

    $ python manage.py clean_migrations plan --output plan.json
    $ python manage.py clean_migrations apply --plan plan.json

That lets you review the plan, or make it in CI and apply it elsewhere.
The file records the tool version, the options used, how the plan was verified, the full report (removals with their locations, blocked things with reasons, warnings), and, for each migration file to edit, its module and path (relative to the plan file), a SHA-256 hash of its current content, and its new content.
``apply --plan`` finds each file through its module, as Python imports it now, so it can be run from any directory; ``apply`` refuses to write anything if any of those files changed since the plan was made.
It then writes the files and verifies them exactly as ``apply`` without a plan does.
A plan file can’t be combined with targets or other analysis options.

Django’s own options (``-v``, ``--no-color``, ``--force-color``, ``--traceback``, ``--skip-checks``, ``--settings``, ``--pythonpath``) can go before or after the subcommand.

By default the command considers every app that has at least one migration file inside your project.
Migrations installed from third-party packages are read, because their operations matter for working out what is safe, but they are never edited.

Targets
-------

Pass one or more targets to limit the cleaning to part of your project.
Targets are case-insensitive and can be an app label, a model, or a field:

.. code-block:: console

    $ python manage.py clean_migrations plan shop
    $ python manage.py clean_migrations plan shop.Coupon
    $ python manage.py clean_migrations plan shop.Product.legacy_ref

* ``app`` selects everything in that app.
* ``app.Model`` selects the model and everything belonging to it: its fields, indexes, constraints, ``unique_together`` and ``index_together`` entries, and superseded operations.
* ``app.Model.field`` selects that field, including superseded ``AlterField`` operations on it.

A model or field matches if *any* name it had during its life matches, so renamed models and fields can be selected by their old or new names.

An unknown app label is an error.
An unknown model or field name is not: it just matches nothing, and the command reports that there is nothing to remove.

A target picks which things the command is trying to remove.
Other things may still go too, if removing the selected thing requires it.
For example, if you target ``shop.Coupon`` and a foreign key in another app points to it, that foreign key must be removed as well, in whatever app it lives.
If it can’t be removed, then ``shop.Coupon`` stays.

Use ``--exclude`` to protect something.
It takes the same kind of target, and can be repeated.
Excluded things are never removed, and anything that depends on them is kept too.
Excluding a field, for example ``shop.Coupon.code``, also keeps its model, since removing the model would remove the field:

.. code-block:: console

    $ python manage.py clean_migrations plan --exclude shop.Coupon --exclude blog

A target or exclusion naming a model or field that never existed in the migration history is an error, so typos don’t silently select nothing.
Old names count: a model or field matches any name it had.

.. note::

    ``--kind`` doesn’t apply to ``--elidable``, described below, but an excluded app’s migrations are never touched by it.

Kinds of removal
----------------

The command finds several kinds of removable operations.
Use ``--kind``, repeated as needed, to only consider some of them:

.. code-block:: console

    $ python manage.py clean_migrations plan --kind field --kind index

The default is all kinds.
``--skip-kind``, also repeatable, leaves some out instead, for example ``--skip-kind fold``.

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Kind
     - What it removes
   * - ``model``
     - A model created and later deleted, with all the operations in between that touch it (``CreateModel``, ``DeleteModel``, ``RenameModel``, and every field, index, constraint and options operation on it).
   * - ``field``
     - A field added and later removed, whether it was added by ``AddField`` or inside a ``CreateModel``.
       This includes ``AlterField`` and ``RenameField`` operations on it.
   * - ``index``
     - An index added and later removed, including any ``RenameIndex`` operations in between.
   * - ``constraint``
     - A constraint added and later removed, including any ``AlterConstraint`` operations in between.
   * - ``together``
     - An entry in ``unique_together`` or ``index_together`` that is added and later removed.
       This also removes ``AlterUniqueTogether`` and ``AlterIndexTogether`` operations that, after such removals, no longer change anything.
   * - ``superseded``
     - An ``AlterField``, ``AlterModelOptions``, ``AlterModelManagers`` or ``AlterModelTableComment`` operation that a later operation of the same type completely replaces.
       For fields, an ``AlterField`` can also be replaced by the ``RemoveField`` that follows it.
       Only a safe subset of such cases are removed, as described in :doc:`how_it_works`.
   * - ``fold``
     - A later operation moved into the one that created the thing it changes, and deleted: an ``AlterField`` into the ``AddField`` or ``CreateModel`` that added the field, an ``AddField``, ``AddIndex`` or ``AddConstraint`` into the model’s ``CreateModel``, and the model’s first ``AlterUniqueTogether``, ``AlterIndexTogether``, ``AlterModelOptions``, ``AlterModelManagers`` or ``AlterModelTableComment`` into its ``CreateModel`` options.
       The operation that absorbs it is rewritten.
       Only when nothing in between can tell the difference; see :doc:`how_it_works`.

Folded operations are reported against the thing they change, from the deleted operation’s migration to the migration of the operation that absorbs it, and counted separately in the summary:

.. code-block:: console

    $ python manage.py clean_migrations plan -v 2
    shop
      Product
        price  fold  0007_alter_price -> 0002_product_price  1 operation
            shop/migrations/0007_alter_price.py:12  AlterField
            shop/migrations/0002_product_price.py:14  AddField (regenerated)
    Would fold 1 operation and edit 1 operation in 2 migration files. 1 migration would be left with no operations.

.. note::

    An operation that absorbs a fold is rewritten as a whole, the way ``makemigrations`` writes operations (with Django’s migration writer), so its own formatting and comments are lost, and new imports may be added after the file’s last import.
    Run your code formatter afterwards.
    Operations that only lose parts, such as one field of a ``CreateModel``, keep their formatting.

Operations from ``django.contrib.postgres.operations`` are handled the same way as their standard equivalents.
For example, ``AddIndexConcurrently`` is treated as ``AddIndex``.

.. _opaque-operations:

Opaque operations
-----------------

Some operations can do anything: ``RunPython``, ``RunSQL``, ``SeparateDatabaseAndState``, and any custom operation.
The command calls these *opaque*.
An opaque operation that runs while a model or field exists might use it, so removing that model or field could make the migration crash on a fresh database.

The ``--opaque`` option sets how cautious the command is:

``--opaque scan`` (the default)
    Look through the opaque operation’s code for the names of the thing being removed.
    If the code mentions it, the removal is blocked.
    Otherwise it is allowed.

``--opaque block``
    Be strictly cautious: any opaque operation that might run while the thing exists blocks its removal, whether or not it mentions the names.
    This blocks a lot, since migrations in different apps that aren’t ordered relative to each other might run in either order.

``--opaque ignore``
    Never let opaque operations block anything.
    Only use this if you know your data migrations don’t touch the things being removed.

.. warning::

    With ``--opaque ignore``, a ``RunPython`` that refers to a removed field or model is left in place, and will fail when run on a fresh database.
    It also turns off the check for a model’s default ``ordering`` (or ``get_latest_by``) naming a removed field: any ``RunPython`` that queries that model in the meantime, even without naming the field, then fails with ``Cannot resolve keyword``.
    Run the whole history on a new database afterwards.

Regardless of the policy, an opaque operation that changes the project state (for example, a ``SeparateDatabaseAndState`` that adds a field) makes the models and fields it touches permanently off limits.
See :doc:`how_it_works` for details of how the scan works, and its limits.

Elidable operations
-------------------

Migration operations marked ``elidable=True`` are declared safe to drop when squashing.
The command respects that in two ways.

First, an elidable ``RunPython`` or ``RunSQL`` operation never blocks a removal.
If its code mentions something that is being removed, and it certainly runs while that thing exists (its migration comes after the creation and before the removal in every possible migration order), the elidable operation is removed too, since leaving it in place could crash on the missing model or field.
An elidable operation that mentions the name but could run in a different order, for example in another app with no dependency either way, blocks the removal like any other ``RunPython`` or ``RunSQL``: a matching word is not proof that it is about the same thing.
Elidable operations in excluded apps are never removed, so they block too.
In this example, the data fix in ``0003_widen_sku`` mentions ``Coupon``, so it is removed along with the model:

.. code-block:: console

    $ python manage.py clean_migrations plan
    ...
    shop
      RunPython         elidable    0003_widen_sku                           1 operation  (mentions 'coupon', removed with shop.Coupon)
      Coupon            model       0002_coupon_legacy_ref -> 0004_cleanup  2 operations
    ...

Second, with ``--elidable``, *every* elidable ``RunPython`` and ``RunSQL`` operation in the selected apps is removed, regardless of what it mentions:

.. code-block:: console

    $ python manage.py clean_migrations plan --elidable

This is the same as dropping them while squashing.
Only operations that don’t change the project state are removed.
``--elidable`` considers the apps you pass as targets (or all apps), minus apps passed to ``--exclude``, and ignores ``--kind``.

Using it in CI
--------------

``plan --check`` exits with status 1 if anything is removable.
This lets you fail a CI job when new redundant operations creep in:

.. code-block:: console

    $ python manage.py clean_migrations plan --check
    $ echo $?
    1

It takes the same targets and options as ``plan``.

Seeing why things are kept
--------------------------

By default, only things that will be removed are listed.
Add ``--show-blocked`` to also list things that cancel out in the history but were kept, with reasons:

.. code-block:: console

    $ python manage.py clean_migrations plan --show-blocked
    ...
    Blocked
      shop.Product.rank  field added in 0002_coupon_legacy_ref, removed in 0004_cleanup
        RunPython in shop.0002_coupon_legacy_ref mentions 'rank'

The reasons say which operation, in which migration, is in the way.
You can then make that operation more specific, or accept it and leave the field in place.

The output also depends on the verbosity (``-v``):

* ``-v 0`` prints nothing.
  Combine it with ``--check`` when you only want the exit status.
* ``-v 1`` (the default) prints the removals and the summary shown above.
* ``-v 2`` also lists each operation to remove as ``path:line  OperationName``, and implies ``--show-blocked``.
  Paths are relative to the current directory.
  When a single entry is taken out of a larger operation, its detail is shown in parentheses:

  .. code-block:: console

      $ python manage.py clean_migrations plan -v 2 blog.Post.spam_score
      blog
        Post
          spam_score  field  0001_initial -> 0002_tidy  2 operations
            blog/migrations/0001_initial.py:15  CreateModel (field 'spam_score')
            blog/migrations/0002_tidy.py:10  RemoveField
      Would remove 1 operation and edit 1 operation in 2 migration files.
      Run 'clean_migrations apply' to apply.

Warnings, such as migrations skipped because a squashed migration replaces them, go to stderr.

What is left behind
-------------------

The command only removes operations.
Some things remain for you to tidy up:

**Empty migrations.**
When all the operations in a migration are removed, the file stays, with ``operations = []``:

.. code-block:: python

    class Migration(migrations.Migration):
        dependencies = [("blog", "0001_initial")]

        operations = []

You must keep these files.
Other migrations depend on them, and databases have recorded them as applied.
They are fast to apply, and you can remove them later through squashing.

**Unused imports.**
The command never edits imports, so migrations may end up importing modules that they no longer use, such as ``models``.
Run a linter to remove them.
For example, with `Ruff <https://docs.astral.sh/ruff/>`__:

.. code-block:: console

    $ ruff check --select F401 --fix */migrations/

**Stale dependencies.**
Migration dependencies are left untouched, even if a migration no longer needs the dependency it declares.
This is harmless, but means the graph may order migrations more strictly than needed.

**Helper code.**
Functions that only an operation used, such as a ``RunPython`` callable, are left in place if the ``RunPython`` is not removed.

.. _verifying-your-schema:

Trial runs
----------

By default, the command decides whether a data migration (``RunPython``, ``RunSQL`` or a custom operation) might use something by scanning its source for names, which blocks a lot (see :ref:`opaque-operations`).
``--trial`` replaces that guess with evidence, by actually migrating scratch databases:

.. code-block:: console

    $ python manage.py clean_migrations plan --trial --output plan.json
    ...
    Trial: 5 migrate runs on postgresql in 141.7s; unlocked 212 removals of 221 that the static scan blocks, blocked 9 on evidence.
    $ python manage.py clean_migrations apply --plan plan.json

It works like this:

1. The original history is migrated on an empty scratch database (as for ``profile``), and a snapshot is taken of the resulting schema (in far more detail than Django’s model state: columns, defaults, constraints, indexes, sequences, views, functions, triggers and more) and all data, except date and time values.
2. A candidate plan is made in which the name scan doesn’t block anything, but each removal remembers which data migrations mentioned it.
   The command says how many removals a trial could unlock at most: if that is none, only the default plan is trialled.
   Then the original history is migrated a second time: anything that differs between the two runs (random or time-based values in rows that migrations create, such as UUIDs, tokens or password hashes) is left out of every comparison: just the differing columns where that is enough, else the table’s data.
   ``--trial-single-baseline`` skips that run; such differences are then only recognised when nothing else can be blamed, and may block removals.
3. The candidate is migrated on a fresh scratch database, with the edited operations in memory: no files are touched.
   If a data migration fails, its savepoint is rolled back, the failure is recorded, and the run goes on, so one run finds many failures.
   (That needs a backend that can roll back schema changes, such as PostgreSQL or SQLite, and an atomic migration; otherwise the run stops at the first failure.)
4. Failures are blamed as narrowly as possible:

   * An error that names what is missing (``Cannot resolve keyword 'x'``, ``has no field named 'x'``, ``object has no attribute 'x'``, ``column "x" does not exist``, ``doesn't have a 'X' model``, ``No installed app with label 'x'``, ``relation "t" does not exist`` and their SQLite and MySQL forms) blocks exactly the removals of that field, model, table or app, whichever data migration it occurs in.
   * Any other failure is blamed on the removals the failing operation mentioned (narrowed to those named in the error message, if any), or else on those whose lifetime includes the failing migration.
     Only half of them are blocked at first; if the same failure comes back in the next run, the other half is blocked too.
     Only the first such failure of a run is used: later ones may follow from it (the failed operation did nothing), and are tested again in the next run.
   * A snapshot that differs from the original blocks the removals relating to the differing table.

   A data migration stops at its first error, so its next missing field only shows up in the next run.
   This is why a project with many data migrations takes a few runs.
5. The candidate is solved again, and run again, until a run has no failures and a snapshot identical to the original.
   That final clean run is required.
   After ``--trial-max-runs`` runs (default 8), or when a run blames nothing new, the command falls back to the default plan, and still trials it.
   A trial never does worse than the default plan: candidates are solved on top of it (its removals and folds first, then what the trial unlocks), and a final check makes sure every operation the default plan removes is removed; if not, the default plan is used.

The command prints how long the original history took to migrate, and so roughly how long each run takes, before the first trial run, then what each run found and blocked, and shows its progress (run, migration, elapsed time) as it goes.
The summary counts *migrate runs*, the baseline runs included; so does ``runs`` in the plan file.
Long error messages are shortened in the output and in plan files (``FieldError``'s list of choices is left out).

``apply --trial`` does the same and applies the result.
The plan file records the trial: backend, number of runs, what it unlocked, and the failures it harvested (shown with ``-v 2``).
``--trial`` can’t be combined with ``--opaque``.

.. warning::

    The evidence holds for the database backend and settings of the scratch database (``--database``), and only for *empty* databases: data migrations are only shown to be harmless when they find no rows (other than rows seeded by earlier migrations, which are compared).
    That is the tool’s premise anyway, since every real database must already be fully migrated.
    But if your test suite tests migrations with data, for example with django-test-migrations, a migration that now finds a removed field may fail there.

If data from the original history itself differs between runs (random or time-based values in seeded rows), the command reports the table and stops comparing its data, rather than blaming removals for it.

.. warning::

    **A trial on SQLite verifies less than one on PostgreSQL.**
    SQLite constraints and inline unique indexes have no names worth comparing (Django creates them as part of ``CREATE TABLE`` and rebuilds the table on most changes), so a change of constraint or index name, which matters on PostgreSQL and MySQL, can’t be seen there.
    If your production database isn’t SQLite, run the trial against a scratch database of the same backend (``--database``).

SQLite column order needs care of its own.
Django rebuilds SQLite tables in the order fields have in its model state, and an ``AlterField`` or ``RenameField`` moves its field to the end of that order; so removing or folding such operations, or folding a field past one, can change the physical order of columns in a table that is rebuilt later.
Nothing in Django depends on that order, but a trial compares it anyway, so its result is exact on SQLite as on other backends (which never move columns): a difference blocks the removals and folds on that table, including ones the default plan makes, which are then left out of the plan.
``--trial-ignore-column-order`` turns that comparison off.
A plan made without a trial on SQLite says, in its summary, when it may change column order.

Removals the default plan makes on its own are never blamed for a failure: they are proven without a trial.
The exceptions are folds, no-op data migrations (see below), and SQLite column order: when the default plan’s own trial run fails or differs because of one of those, it is solved again without them.
Only removals that the name scan would have kept, and folds, are tried and blocked on evidence.

Pruning empty migrations
------------------------

Once operations are removed and folded, many migrations are left with no operations at all.
``--prune`` (on ``plan`` and ``apply``) deletes them, and makes every migration that depended on a deleted one depend on that one's dependencies instead:

.. code-block:: console

    $ python manage.py clean_migrations apply --prune
    ...
    shop
      Migration  prune  0002_add  (dependants rewired: shop.0004_qty)
    ...
    Removed 42 operations in 30 migration files. 12 migrations deleted.

Every remaining migration still runs after exactly the same migrations as before (minus the deleted ones): a dependency is only added where the migration didn't already have it through its other dependencies, and the result is checked, in memory and after writing the files.
Migrations that were already empty are pruned too.

A migration is kept, and listed with ``--show-blocked``, when:

* it is the first or the latest migration of its app, or marked ``initial`` (an app keeps its first and latest migrations, so merges, ``--fake-initial`` and ``__first__``/``__latest__`` dependencies behave as before);
* it is a squashed migration, or a squashed migration replaces it;
* it has ``run_before``, or another migration names it in ``run_before``;
* a migration depending on it can't be edited (an installed package, an excluded app), or doesn't list it as a plain ``("app", "name")`` tuple in a literal ``dependencies`` list;
* another migration file mentions its name other than in its dependencies, for example to import a function from it with ``import_module()``;
* its app isn't selected, or is excluded.

Whatever else the deleted file held (functions of the data migrations removed from it, ``atomic = False``...) goes with it.
Rewritten dependency lists keep their other entries as written, ``swappable_dependency()`` calls included.
``apply`` deletes the files (and their bytecode) and, if anything fails, writes them back; plan files record each deletion with the hash of the file.

.. warning::

    Pruning changes the set of migration names:

    * Existing databases keep a row in ``django_migrations`` for each deleted migration.
      It is harmless; ``python manage.py migrate --prune`` removes such rows.
    * Branches, or other projects, with migrations that depend on a deleted migration need that dependency updated (to the deleted migration's dependencies) before they can be merged.
    * Tooling that names migrations, such as ``migrate app 0042_x`` in scripts or documentation, needs updating.

With ``--trial``, the trial runs use the pruned graph too, and expect exactly the pruned migrations to be missing from ``django_migrations``.


No-op data migrations
---------------------

Many data migrations only do something to rows that already exist: they backfill a new column, fix up old values, or move data from one place to another.
On a fresh database there are no such rows, so they run a few ``SELECT`` queries and change nothing, yet they keep whatever they mention from being removed (see :ref:`opaque-operations`).
``--noop-data`` (which implies ``--trial``) removes them:

.. code-block:: console

    $ python manage.py clean_migrations plan --noop-data
    ...
    shop
      RunPython  noop  0042_backfill_x  1 operation  (no writes on a fresh database: 3 queries)
    ...
    Would remove 26 operations in 12 migration files. Of the operations removed, 4 are data migrations that did nothing on a fresh database.

While the original history runs on the trial's first scratch database, every statement each candidate executes, on every database connection, is recorded with the number of rows it affected.
A candidate is an exact ``RunPython`` or ``RunSQL`` operation (not a subclass) that doesn’t change the project state (no ``state_operations``), in an editable migration of a selected app that isn’t excluded.
It counts as a no-op only if every statement it ran was:

* a read: a ``SELECT`` (or ``WITH`` or ``VALUES``) that calls no function other than the usual aggregates, comparisons and conversions, and contains no ``INSERT``, ``UPDATE``, ``DELETE``, ``MERGE``, ``INTO``, ``nextval`` or ``setval``;
* transaction control (savepoints, ``BEGIN``, ``COMMIT``...), ``SET CONSTRAINTS`` or ``SET LOCAL``;
* or an ``UPDATE`` or ``DELETE`` that affected no rows.

Anything else counts as a write and keeps the operation: ``INSERT``, ``COPY``, any schema change, other ``SET`` statements, functions it doesn’t know (which might write), sequence functions, an unknown row count, and any statement on another database alias.
Each removal is then a trial unit like any other: removed from the start, so the name scan doesn’t see the operation and what it mentioned can go too, and blamed (and put back) if a trial run fails or differs without it.
The plan file lists them under ``verification.noop``, with what was observed.

.. warning::

    What a data migration does outside the database can’t be observed: files, caches, e-mails, calls to other services, or changes to the process.
    Nor can a database that isn’t configured in ``DATABASES``.
    Its reverse code is lost too: migrating backwards past the migration then does nothing instead of running ``reverse_code``/``reverse_sql``, which is usually harmless for a migration that had nothing to do, but not always.
    And the evidence holds for fresh databases only, as for any trial: a test suite that migrates databases with data through the history will see the difference.
    So this is opt-in: review the removed operations before applying.


Profiling migrations
--------------------

``clean_migrations profile`` shows where the time goes when migrating from scratch.
It creates an empty scratch database, runs the whole history on it with Django’s migration executor, prints timings, and drops the database:

.. code-block:: console

    $ python manage.py clean_migrations profile --top 5
    Migrated 412 migrations in 68.50s on postgresql: 14.85s in the database, 47.20s updating the project state, 6.45s other.
    Slowest 5 migrations
    ...
    By operation type
    ...
    By app
    ...
    Top 5 operations by project state cost
    ...
    The default plan removes 151 operations taking 9.204s (13.4%), and edits operations taking 0.412s (0.6%), counting database and project state time.

Time is split into:

* **database**: inside each operation’s ``database_forwards()``, the schema change or data migration itself, including any rendering of models it triggers;
* **project state**: updating Django’s in-memory model of the project for each operation, which ``migrate`` also has to do: the copy of the state Django takes before each operation, and the operation’s ``state_forwards()``, which re-renders the models it touches and every model related to them;
* **other**: the rest of the executor’s work, such as transactions, recording applied migrations, and deferred SQL.

On large projects the project state usually dominates: an operation on a model that many others relate to is expensive however small its SQL.
The “by project state cost” table shows which operations, and so which models, those are.

The tables show, per operation type, app and slowest migration, the count, database time, project state time and share of the total.
The removable estimate counts both database and project state time of the operations the default ``plan`` would remove or fold away, and of those it edits.
``pre_migrate`` is sent before migrating, as ``migrate`` does: handlers may prepare the database (a schema, an extension) or, like the content types app, add operations.
``post_migrate`` handlers, such as creating content types and permissions, are not run: they come after the history, so they can’t change how it migrates, and what they create depends only on the final models, which the plan keeps.
``--json`` prints the same report as JSON.

The scratch database uses the backend and settings of the ``default`` database, or of ``--database ALIAS``.
For SQLite it is a temporary file; for PostgreSQL and MySQL a new database named ``cleanmig_<random>_<name>`` is created next to the real one, so the database user needs permission to create databases, as for running tests.

While migrating, *every* configured database alias is pointed at a scratch database, as Django’s test runner does, so no project code can reach a real database:

* the chosen alias, the aliases configured for the same database (same engine, host, port and name, such as a read replica alias), and test mirrors (``"TEST": {"MIRROR": ...}``) share one scratch database, so a replica sees what the history wrote;
* every other database gets its own, empty scratch database: only the chosen alias is migrated, so a data migration that reads another database finds no tables there.
  Multi-database projects that migrate several aliases are trialled and profiled one alias at a time;
* aliases using the dummy backend are left alone; any alias on a backend that can’t have scratch databases (anything but SQLite, PostgreSQL and MySQL) makes the command refuse to run: remove it from ``DATABASES`` for the command.

Every scratch database is dropped afterwards, after closing every connection, also on errors, Ctrl-C and ``SIGTERM``.
The real databases are never touched.

Each migrate run happens in a child process of its own (``os.fork()``), which ends with the run, so nothing it leaves in the process (module-level caches of historical models, changed field objects, registered handlers) can affect the next run or the analysis.
Where ``os.fork()`` doesn’t exist (Windows), runs happen in-process, with a warning.

Only a hard kill (``SIGKILL``, a crash of the machine) can leave a scratch database behind.
``clean_migrations scratch`` lists any on the server (or, for SQLite, in the temporary directory), and ``--drop NAME`` or ``--drop-all`` drops them.
Other trial or profile runs against the same server use scratch databases too, so only drop those you know are left over.

Verifying your schema
---------------------

The command’s own checks work on Django’s in-memory model of your project, rather than a real database.
For extra confidence, compare the schema that migrations create before and after, on a fresh database of the same type you use in production.

For example, with PostgreSQL, before running ``apply``:

.. code-block:: console

    $ dropdb --if-exists clean_check && createdb clean_check
    $ DATABASE_URL=postgres:///clean_check python manage.py migrate
    $ pg_dump --schema-only --no-owner clean_check > /tmp/before.sql

Then run ``clean_migrations apply``, and repeat on a fresh database:

.. code-block:: console

    $ dropdb clean_check && createdb clean_check
    $ DATABASE_URL=postgres:///clean_check python manage.py migrate
    $ pg_dump --schema-only --no-owner clean_check > /tmp/after.sql
    $ diff /tmp/before.sql /tmp/after.sql

Adjust the commands to the way your project selects its database.
The diff should be empty.

Next steps
----------

With redundant operations gone, your migration history is shorter and easier to understand.
It also makes it simpler to squash, since the leftover operations are fewer and no longer entangled.
Try ``squashmigrations`` afterwards, if you want to reduce the number of files as well.
