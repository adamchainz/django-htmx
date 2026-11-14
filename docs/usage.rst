Usage
=====

django-clean-migrations adds one management command, ``clean_migrations``.
It reads your whole migration history and removes operations that cancel each other out, such as a model that is created and later deleted, or a field that is added and later removed.
The migration files are edited in place: same files, same names, same dependencies, fewer operations.

Before you start
----------------

**Every database that will ever run your migrations must already be fully migrated.**
This is the same caveat as when squashing migrations.
The command edits migrations that other databases have already recorded as applied.
If a database is only partway through the history, removing operations from the middle can leave it with a schema that doesn’t match what the later migrations expect.

So, before running the command with ``--write``:

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

Run the command with no arguments to see what would change.
This is a dry run, and doesn’t touch any files:

.. code-block:: console

    $ python manage.py clean_migrations
    blog.Post.draft_notes: field added in 0001_initial, removed in 0002_tidy (2 operations)
    blog.Post: index 'post_title_idx' added in 0001_initial, removed in 0002_tidy (2 operations)
    shop.Coupon: model created in 0002_coupon_legacy_ref, deleted in 0004_cleanup (2 operations)
    shop.Product.legacy_ref: field added in 0002_coupon_legacy_ref, removed in 0004_cleanup (2 operations)
    shop.Product.sku: AlterField in 0003_widen_sku superseded by AlterField in 0004_cleanup (1 operation)
    Would remove 9 operations in 5 migration files. 2 migrations would be left with no operations.

Each line names the thing that would be removed, then where it appeared and disappeared in the history.
The summary counts operations removed outright, operations edited (for example a single field taken out of a ``CreateModel``), and migration files that end up with no operations at all.
Counts that are zero are left out.

When you are happy with the plan, apply it with ``--write``:

.. code-block:: console

    $ python manage.py clean_migrations --write
    ...
    Removed 9 operations in 5 migration files. 2 migrations now have no operations.

Running the command again then reports that there’s nothing left to do:

.. code-block:: console

    $ python manage.py clean_migrations
    No operations to remove.

By default the command considers every app that has at least one migration file inside your project.
Migrations installed from third-party packages are read, because their operations matter for working out what is safe, but they are never edited.

Targets
-------

Pass one or more targets to limit the cleaning to part of your project.
Targets are case-insensitive and can be an app label, a model, or a field:

.. code-block:: console

    $ python manage.py clean_migrations shop
    $ python manage.py clean_migrations shop.Coupon
    $ python manage.py clean_migrations shop.Product.legacy_ref

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

    $ python manage.py clean_migrations --exclude shop.Coupon --exclude blog

A target or exclusion naming a model or field that never existed in the migration history is an error, so typos don’t silently select nothing.
Old names count: a model or field matches any name it had.

.. note::

    ``--kind`` doesn’t apply to ``--elidable``, described below, but an excluded app’s migrations are never touched by it.

Kinds of removal
----------------

The command finds several kinds of removable operations.
Use ``--kind``, repeated as needed, to only consider some of them:

.. code-block:: console

    $ python manage.py clean_migrations --kind field --kind index

The default is all kinds.

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

Operations from ``django.contrib.postgres.operations`` are handled the same way as their standard equivalents.
For example, ``AddIndexConcurrently`` is treated as ``AddIndex``.

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

    $ python manage.py clean_migrations
    ...
    shop: elidable RunPython in 0003_widen_sku (mentions 'coupon', removed with shop.Coupon) (1 operation)
    shop.Coupon: model created in 0002_coupon_legacy_ref, deleted in 0004_cleanup (2 operations)
    ...

Second, with ``--elidable``, *every* elidable ``RunPython`` and ``RunSQL`` operation in the selected apps is removed, regardless of what it mentions:

.. code-block:: console

    $ python manage.py clean_migrations --elidable

This is the same as dropping them while squashing.
Only operations that don’t change the project state are removed.
``--elidable`` considers the apps you pass as targets (or all apps), minus apps passed to ``--exclude``, and ignores ``--kind``.

Using it in CI
--------------

``--check`` makes the command exit with status 1 if anything is removable, and never writes any files.
This lets you fail a CI job when new redundant operations creep in:

.. code-block:: console

    $ python manage.py clean_migrations --check
    $ echo $?
    1

It takes the same targets and options as a dry run.
``--check`` can’t be combined with ``--write``.

Seeing why things are kept
--------------------------

By default, only things that will be removed are listed.
Add ``--show-blocked`` to also list things that cancel out in the history but were kept, with reasons:

.. code-block:: console

    $ python manage.py clean_migrations --show-blocked
    ...
    Blocked:
      shop.Product.rank: field added in 0002_coupon_legacy_ref, removed in 0004_cleanup
        - RunPython in shop.0002_coupon_legacy_ref mentions 'rank'

The reasons say which operation, in which migration, is in the way.
You can then make that operation more specific, or accept it and leave the field in place.

The output also depends on the verbosity (``-v``):

* ``-v 0`` prints nothing.
  Combine it with ``--check`` when you only want the exit status.
* ``-v 1`` (the default) prints the summary shown above.
* ``-v 2`` also lists each operation to remove as ``path:line  OperationName``, and implies ``--show-blocked``.
  Paths are relative to the current directory.
  When a single entry is taken out of a larger operation, its detail is shown in parentheses:

  .. code-block:: console

      $ python manage.py clean_migrations -v 2 blog.Comment
      blog.Comment.spam_score: field added in 0003_comment, removed in 0004_drop_spam (2 operations)
          blog/migrations/0003_comment.py:13  CreateModel (field 'spam_score')
          blog/migrations/0004_drop_spam.py:8  RemoveField
      Would remove 1 operation and edit 1 operation in 2 migration files. 1 migration would be left with no operations.

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

Verifying your schema
---------------------

The command’s own checks work on Django’s in-memory model of your project, rather than a real database.
For extra confidence, compare the schema that migrations create before and after, on a fresh database of the same type you use in production.

For example, with PostgreSQL, before running ``--write``:

.. code-block:: console

    $ dropdb --if-exists clean_check && createdb clean_check
    $ DATABASE_URL=postgres:///clean_check python manage.py migrate
    $ pg_dump --schema-only --no-owner clean_check > /tmp/before.sql

Then run ``clean_migrations --write``, and repeat on a fresh database:

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
