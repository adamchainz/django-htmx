How it works
============

This page explains how django-compact-migrations decides what is safe to remove.
You don’t need it to use the command, but it helps you understand why something was kept, and what the command can’t know.

The goal: a fresh database migrated with the compacted history must end up with the same schema as one migrated with the original, apart from :ref:`what may change <what-may-change>`.
When the command is unsure, it keeps the operation.

This page gives the principles, with an example each.
The detailed rules live in the code and its tests: ``src/django_compact_migrations/solver.py`` decides removals, ``fold.py`` decides folds, and ``tests/`` holds a case for each rule.

Reading the history
-------------------

The command loads your migrations with Django’s ``MigrationLoader``, and replays them in the order ``migrate`` would run them, tracking the state of every model.
It connects to the configured databases only for system checks, to tell MySQL from MariaDB, and, in ``apply``, to read which migrations each database has applied.

Squashed migrations are handled as Django handles them: the squashed migration is used, and the migrations it replaces are skipped, with a warning if they are still on disk.

.. _project-migrations:

Which migrations are edited
~~~~~~~~~~~~~~~~~~~~~~~~~~~

A migration is a *project migration*, and may be edited, when its file is:

* under the project root: the top of the Git work tree holding the current directory, else the current directory;
* not inside a virtual environment (a directory with ``pyvenv.cfg``);
* not part of an installed package (a ``site-packages``, ``dist-packages``, ``.dist-info`` or ``.egg`` path component).

A migration outside the project is also editable when its app is named as a target, unless it is in an installed package.
The file counts, not the app: if ``MIGRATION_MODULES`` points at a package inside your project, those migrations are editable.
Other migrations are never edited, but they are replayed, because their operations affect which references exist.

To edit a file, the command must also find each operation in the source: a single literal ``operations = [...]`` list in ``class Migration``, with one call per operation.
If a migration has any other shape, anything that would need an edit there is kept, and ``-v 2`` says why.

.. _lives:

Lives
-----

The command follows the *life* of each thing, not its name, since names get renamed and reused.
A life runs from the operation that creates a model, field, index, constraint or together-entry to the one that deletes it.
Operations in between, such as ``AlterField`` or ``RenameField``, belong to the life they change.
A life that begins and ends is a candidate for removal: if all its operations go, nothing it did remains.

For example, a ``Coupon`` model created in ``0002`` and deleted in ``0004`` can go, with every operation on it.

The unit of removal is not always a whole operation.
A ``CreateModel`` can define fields, indexes and constraints with different lives, and the command can take one entry out and leave the rest.
The report counts these as “edited” operations.

Some lives are never candidates: one that still exists at the end, a field that was ever a primary key (on its own; the whole model can still go), and a name reused in a way the command can’t track.

What blocks a removal
---------------------

**References.**
A life can only be removed if everything that refers to it is removed too.
Foreign keys refer to their target model and field, models to their bases, indexes and constraints to the fields they mention, and so on.
The command solves this for all lives together: it assumes everything can go, then puts back anything still referred to, until nothing changes.
That is how a model and the foreign keys pointing at it, possibly from other apps, go together or stay together.

**Opaque operations.**
The command reads the operations built into Django for models, fields, indexes and constraints, recognised by their exact class.
Everything else is *opaque*: ``RunPython``, ``RunSQL``, ``SeparateDatabaseAndState``, and any custom or subclassed operation.
When an opaque operation changes the project state, say a ``SeparateDatabaseAndState`` that adds a field, everything it touches is *locked*: never removed, superseded or folded.
Proxy models, swapped-out models, and models whose ``managed`` option changes are locked too, since Django skips their schema changes.

**The name scan.**
An opaque operation that changes no state can still use things, such as a ``RunPython`` that queries a model.
So the command scans the text of every opaque operation that may run while a thing exists, its *window*.
If the text mentions one of the thing’s names, the removal is blocked.
The scanned text includes the migration file’s helper functions, the source of ``RunPython`` callables defined elsewhere, and the SQL of ``RunSQL``.
Only strings, attribute names, keyword names and imports count, so a loop variable called ``event`` doesn’t block a field called ``event``.
Matching ignores case and treats underscores as boundaries: ``coupon`` matches ``Coupon``, ``coupon_set`` and ``shop_coupon``, but not ``coupons``.
A false match only keeps something.
A missed use, such as a name built at run time, is what the trial is for.

**Elidable operations.**
An elidable ``RunPython`` or ``RunSQL`` never blocks.
If it mentions a removed thing and certainly runs only while that thing exists, it is removed with it (kind ``elidable``).
The whole operation goes, as when squashing, so only mark operations elidable if all of their effects can go.

Superseded operations
---------------------

After removing whole lives, the command removes operations that a later one makes pointless, where it can show the intermediate state is never seen.
For example, an ``AlterField`` followed by another ``AlterField`` on the same field, with nothing in between that refers to the field or mentions it or its model.
The same applies to ``AlterModelOptions``, ``AlterModelManagers`` and ``AlterModelTableComment``.
The guards cover type changes that a database could reject, rows rewritten by a ``NOT NULL`` change, and database objects that would get different names.
``AlterModelTable`` is never superseded, since skipping one would keep the old table name taken.

An ``AlterUniqueTogether`` or ``AlterIndexTogether`` left setting the same value as before is removed too.

Folding
-------

Most of a long history is not “create, then delete”, but “create, then alter many times”.
Folding moves a later operation into the operation that created the thing it changes, and deletes the later one.
It is like Django’s squash optimizer, across the whole graph, but without changing migration files, names or dependencies.

What can be folded:

1. An ``AlterField`` into the ``AddField``, or ``CreateModel`` field entry, that added the field.
2. An ``AddField`` into the model’s ``CreateModel``.
3. An ``AddIndex`` or ``AddConstraint`` into the ``CreateModel``’s ``indexes`` or ``constraints``.
4. The model’s first ``AlterUniqueTogether`` or ``AlterIndexTogether`` into the ``CreateModel`` options.
5. The model’s first ``AlterModelOptions`` (unless it changes ``managed``), ``AlterModelManagers`` or ``AlterModelTableComment`` into the ``CreateModel``.
6. A ``RenameField`` into the operation that created the field, for a field with no indexes, constraints or relations whose names come from the field name, or with a fixed ``db_column``.
7. An ``AlterModelTable`` of an unmanaged model into the ``CreateModel``’s ``db_table``.
8. A ``RenameIndex`` into the operation that defines the index.
9. An ``AlterConstraint`` into the operation that defines the constraint, when only the validation message and code differ.
10. The model’s first ``AlterOrderWithRespectTo`` into the ``CreateModel`` options.

``RenameModel`` is never folded, nor is ``AlterModelTable`` of a managed model.
Django renames the table only, so its constraints, sequences, indexes and many-to-many tables keep names made from the old table name, which a table created under the new name wouldn’t have.

Every fold must keep each database object’s name, and must not make anything exist earlier, or go later, in a way that could collide or break a reference.
For example, a field that gains ``unique=True`` from an ``AlterField`` gets a ``..._uniq`` constraint named by Django, while a unique column created with the table gets a name chosen by the database, so that ``AlterField`` isn’t folded.
Timing matters too.
Django creates the indexes, foreign keys and ``unique_together`` constraints of a new table or column at the end of its migration, named after what the table and columns are called by then.
So an ``AlterField`` that drops or retargets them in that same migration leaves them in place, and isn’t folded or superseded.
Nor is anything folded when a rename follows it in its migration.
PostgreSQL keeps only one of several unique constraints on the same columns in one ``CREATE TABLE``, so a ``UniqueConstraint`` isn’t folded into a ``CreateModel`` that already makes those fields unique.
Opaque operations in between must pass the name scan, or the trial.
The folded operation must also be one Django’s migration writer can write.
A folded field, index or constraint goes at the end of the ``CreateModel``’s list, so columns may come out in another order (see :ref:`what-may-change`).

Folds can make more things removable: a foreign key retargeted in a fold leaves its old model unreferenced.
So the command analyses the edited history again, until nothing changes, for at most ten rounds.

Verification
------------

The command doesn’t trust its own analysis.
Before reporting anything, it replays the edited operations in memory against Django’s ``ProjectState``.
The replay must not raise, no migration may leave more relations pointing at missing models than the original did, and the final state of every model must equal the original’s.
The new source of each file must compile.
If any check fails, the command stops with an “Internal error” and writes nothing: please report it, and use ``--exclude`` meanwhile.

When applying, it re-reads the written files with a fresh ``MigrationLoader``, checks the graph, operations and final state again, and puts the original files back if anything is off.

.. _trial-mode:

Trial mode
----------

By default, every plan is checked by a *trial* on scratch databases (``--static`` skips it).
In a trial, the name scan stops being a blocker: each removal it would have blocked remembers which opaque operations mentioned it.
Every other rule stays.

1. The original history is migrated on an empty scratch database, and a snapshot is taken.
   If that fails, the command stops.
2. The original history is migrated a second time.
   Values that differ between these two baseline runs, such as times and random values, are left out of every comparison.
3. The candidate plan is migrated on a fresh scratch database, without touching the files on disk.
   Where the backend can roll back schema changes, a failing opaque operation is rolled back and recorded, and the run goes on, so one run can find many failures.
4. Failures and differences are blamed as narrowly as possible.
   An error that names what is missing, such as ``Cannot resolve keyword 'x'``, blocks just the removals of that thing.
   Other failures are blamed on the removals the failing operation mentioned, or on those whose window includes it.
   A snapshot that differs from the baseline blocks the removals relating to the differing table.
5. The plan is solved again and run again, until a run has no failures and matches the baseline.
   After 8 runs, or when a run blames nothing new, the command falls back to the static plan, and trials that.

Removals that the name scan would have kept, folds and no-op data migrations are the first suspects.
If nothing is left to blame, ``--prune`` and then ``--noop-data`` are given up, with a note.
If even that plan fails, ``plan`` exits with an error and ``apply`` writes nothing.

A trial migrates the ``--database`` alias only.
A data migration that ``DATABASE_ROUTERS`` keep from running there can’t be checked, so what it mentions stays blocked.

What a snapshot compares
~~~~~~~~~~~~~~~~~~~~~~~~

A snapshot holds the schema and the data:

* the tables, columns, indexes, constraints, foreign keys and their names, plus triggers, views and, on PostgreSQL, sequences, functions, types, extensions and comments;
* every table’s rows, compared as a multiset, with ``django_migrations`` compared by app and name only.

Columns are compared by name, not position, and the next id of each table isn’t compared.
Content types and permissions are compared by app label, model and codename, not by id, and so are foreign keys to them.
SQLite keeps no usable names for unique and foreign key constraints, and the trial there compares only which columns a check constraint covers.

Scratch databases
~~~~~~~~~~~~~~~~~

A scratch database uses the backend and settings of the ``--database`` alias.
For SQLite it is a temporary file.
For PostgreSQL and MySQL it is a new database on the same server, named like Django’s test databases but with its own prefix: ``compactmig_<name>``.
While migrating, every configured alias is pointed at a scratch database, as Django’s test runner does, so code that uses Django’s connections can’t reach a real database.
Each run happens in its own child process where ``os.fork()`` exists.
Every scratch database is dropped when the run ends, also on errors, Ctrl+C and ``SIGTERM``.
If a killed process leaves one behind, the next trial replaces it.
So don’t run two trials for the same database at once.

No-op data migrations
---------------------

With ``--noop-data``, the command records every statement each data migration runs in the first baseline run.
An exact ``RunPython`` or ``RunSQL`` that changes no state counts as a no-op if it only read, controlled transactions, or ran ``UPDATE`` or ``DELETE`` statements that affected no rows.
A ``RunPython`` is kept anyway when its source shows effects that queries don’t, such as signal handlers, files, email, network calls or ``call_command()``.
These are listed as kept in the output and the plan file.

Pruning
-------

With ``--prune``, migrations left with no operations are deleted (see :ref:`pruning`).
Some are kept: the first and latest migration of each app, squashed migrations, migrations involved in ``run_before``, and migrations named by something the command can’t rewrite.
Each migration that depended on a deleted one gets its dependencies instead.

The pruned graph must keep three invariants, checked on the plan and again on the rewritten files:

* every surviving migration runs after the same surviving migrations as before;
* every app has the same first and latest migrations, minus the deleted ones;
* Django’s plan is the original plan minus the deleted migrations, in the same order.

``apply`` deletes the files and writes them back if anything fails.
A trial runs the pruned graph too.

MySQL and MariaDB
-----------------

The analysis reasons about Django’s project state, which is the same on every backend.
On MySQL and MariaDB, the name of the index backing a foreign key depends on the order things were created and dropped, and on MySQL, a ``TextField``, ``BinaryField`` or ``JSONField`` added with a default keeps a ``DEFAULT`` that ``CreateModel`` wouldn’t give it.
So when any configured database uses the MySQL backend, the command keeps the removals and folds that would change these.
A server that can’t be reached counts as MySQL, whose rules are a superset of MariaDB’s.
The plan file records the servers the rules were applied for, under ``options.backends``.

Limitations
-----------

* **Squashed migrations that are still on disk are skipped.**
  The migrations a squashed migration replaces are neither scanned nor edited.
  Delete them once the squash is applied everywhere.
* **Folds rewrite operations.**
  An operation that absorbs a fold loses its formatting and comments.
  Use ``--skip-kind fold`` to keep every operation as written.
* **Conservative about a few things.**
  A field that was ever a primary key is never removed on its own, and ``AlterField`` operations on it, or on many-to-many fields, are never superseded.
  ``order_with_respect_to`` blocks removing the field it names, unless the model goes too.
  An opaque operation that changes state keeps everything it touches.
* **The name scan is a heuristic.**
  It can miss a use hidden in helpers, dynamic names, or SQL built at run time.
  Review data migrations that survive near removed things, and test the compacted history on a fresh database.
* **Without a trial, equivalence is checked at project-state level.**
  With ``--static``, verification compares Django’s model of your schema, not the schema itself, which can miss a ``RunSQL`` side effect or database-specific details.
  A trial compares the schema itself, but only on one backend.
  Diff the schema before and after, as shown in :ref:`verifying-your-schema`.
* **On SQLite, a static plan can’t see what table rebuilds change.**
  Django rebuilds SQLite tables for most changes.
  A rebuild drops indexes or triggers that a ``RunSQL`` created, so removing the operation that caused it leaves them in place.
  A rebuild also renames the indexes Django named after a field or model that was since renamed, so removing it leaves the old index name.
  A trial on SQLite sees both; ``--static`` doesn’t.
* **A model whose table name another, renamed table once had is left alone.**
  The renamed table keeps the names of its primary key and sequence, so PostgreSQL numbers the new table’s (``..._pkey1``).
  Whether it does depends on everything both models go through.
* **A model whose** ``unique_together`` **or** ``index_together`` **names** ``_order`` **is left alone.**
  The ``_order`` column isn’t tracked as a field, so nothing of such a model is removed or folded.
* **Existing databases must already be migrated.**
  The compacted history is for fresh databases (see :ref:`before-you-start`).
