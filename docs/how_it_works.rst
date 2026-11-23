How it works
============

This page explains how django-compact-migrations decides what is safe to remove.
You don’t need it to use the command, but it helps you understand why something was kept, and what the command can’t know.

The goal: a fresh database migrated with the compacted history must end up with the same schema as one migrated with the original, apart from :ref:`what may change <what-may-change>`.
When the command is unsure, it keeps the operation.

Terms
-----

These terms are used throughout:

* A *life* is the span of one model, field, index, constraint or together-entry, from the operation that creates it to the one that ends it (see :ref:`lives`).
* Its *window* is the part of the history during which it exists (see `The name scan`_).
* An *opaque operation* is one whose effect the command can’t read: ``RunPython``, ``RunSQL``, ``SeparateDatabaseAndState`` and custom operations.
* The *name scan* looks for the names of what would be removed in the code of the opaque operations in its window.
* The *static plan* is what the analysis alone allows, with the name scan.
  It is what ``--static`` gives.
* A *trial* checks a plan by migrating scratch databases with the original history and with the plan (see :ref:`trial-mode`).
  Its *baseline runs* migrate the original history.
* A *locked* life is never removed, superseded or folded, whatever else happens.
* A *fold* merges an operation into the earlier one that created what it changes.
* A *superseded* operation is one that a later one replaces.

Reading the history
-------------------

The command loads your migrations with Django’s ``MigrationLoader``, without reading any table.
It does connect to the configured databases: Django’s system checks do, telling MySQL from MariaDB needs the server’s version, and ``apply`` reads which migrations each database has applied.
It then replays every migration in the order ``migrate`` would run them, tracking the state of every model as it goes.

Squashed migrations are handled as Django handles them: the squashed migration is used, and the migrations it replaces are skipped.
If replaced migrations are still on disk, the command warns, saying how many it skipped.
It neither analyses nor edits them: only the squashed migration is compacted.
That is fine as long as every database has the squashed migration recorded as applied.
That is the usual precondition for deleting the replaced files anyway; delete them before or after compacting.

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

To edit a file, the command must also find each operation in the source.
It parses the file, and looks for a single ``operations = [...]`` assignment in ``class Migration``.
That must be a plain list or tuple literal, with no ``*`` unpacking, whose entries are calls matching the real operations one-to-one.
If a migration builds its operations dynamically, or has any other unusual shape, it can’t be edited, and anything that would need an edit there is kept.
``-v 2`` shows the reason, such as “operations list in app.0004_x could not be parsed”.

Files that start with a UTF-8 byte order mark (BOM) are edited like others, and keep it.

.. _lives:

Lives
-----

The command follows the *life* of each thing, not its name.
Names are unreliable: models and fields get renamed, and a different model or field can reuse a name later.

A life begins when something is created, and ends when it is deleted:

* A model life runs from ``CreateModel`` to ``DeleteModel``.
* A field life runs from ``AddField`` (or an entry in a ``CreateModel``) to ``RemoveField``.
* Index and constraint lives run from their creation, in ``AddIndex``/``AddConstraint`` or in a ``CreateModel``, to ``RemoveIndex``/``RemoveConstraint``.
* A together life is one entry in ``unique_together`` or ``index_together``.
  It lasts as long as later values of the option still contain the entry.

Operations in between, like ``AlterField``, ``RenameField``, ``RenameIndex`` and ``AlterConstraint``, belong to the life they change.
A life that begins and ends is a candidate for removal: if all its operations go, nothing it did remains.

Some lives are never candidates:

* A life with no end, since it still exists in the final state.
* A life touched by an opaque operation that changes the project state (see `State changes`_).
* A field that was ever a primary key, even briefly.
  Other tables’ columns are derived from a primary key, so the field is never removed on its own.
  (A whole model can still be removed, primary key included.)
* A model or field whose name was reused in a way that can’t be tracked, for example, a ``CreateModel`` for a name that already exists.

Parts of operations
~~~~~~~~~~~~~~~~~~~

The unit of removal is not always a whole operation.
A ``CreateModel`` can define fields, indexes, constraints and together-entries with different lives.
The command can take just one of them out, and leave the rest of the ``CreateModel`` in place.
The summary counts these as “edited” operations.

For this, the command must find the entry in the source: a literal list or tuple (``fields``) or dict (``options``) of the expected shape.
If it can’t, the thing the entry belongs to is kept.

References and blockers
-----------------------

A life can only be removed if everything that refers to it is also removed.
The command records these references while replaying:

* A foreign key or one-to-one field refers to the model it points to, and to the field it points to (the target model’s primary key, unless ``to_field`` says otherwise).
  A many-to-many field refers to its target and to its ``through`` model.
  The reference is held by the operation that defines the field.
* A model with ``bases`` refers to its base models.
* An index, constraint, or together-entry refers to every field it mentions.
  For indexes and constraints, the command walks through the definition (including ``F()`` and ``Q()`` objects, expressions, and ``include``) and treats any string it finds as a possible field name.
  This casts a wide net on purpose: a false match only keeps something.
* A field defined by an expression, such as ``GeneratedField``, refers to the fields in the expression.
* ``order_with_respect_to`` refers to the field it names.
  Because that operation belongs to the model, it blocks removing the field unless the whole model goes.

If an operation that won’t be removed holds a reference, the thing it points to is kept.
The reason shows as, for example, “referenced by AddField in blog.0005_x”.

The command solves this for all lives together, not one at a time.
It starts by assuming everything selected can go, then repeatedly puts back anything that still has a blocker, until nothing changes.
That is how a model and the foreign keys that point to it, possibly from other apps, are removed together, or kept together.

*Soft references* are a weaker kind.
A model’s ``ordering`` and ``get_latest_by`` options can name a field:

* directly, or with a ``-`` prefix;
* through a lookup such as ``fk__name``, which names the local ``fk``;
* in an ``F()`` or ``OrderBy`` expression.

The option may even name the field before it is added, as happens in squashed migrations.
So the command tracks the periods during which an option names an existing field.
A soft reference doesn’t block removing the field by itself.
But if an opaque operation might query the model during such a period, a stale default ordering could crash the query, so the field is kept.
The scan for that looks for:

* the model’s names, including its tables and the related names of its relations;
* the names of models that inherit from it, since they inherit ``Meta.ordering``;
* the names of relation fields on any model that point at it, since related managers and ``prefetch_related`` use the default ordering too.

Opaque operations
-----------------

The command knows precisely the operations built into ``django.db.migrations`` for models, fields, indexes and constraints, and the PostgreSQL equivalents of some of them.
It recognises them by their exact class, not by ``isinstance``, because a subclass may do anything.

Everything else is *opaque*: ``RunPython``, ``RunSQL``, ``SeparateDatabaseAndState``, and any custom, third-party or subclassed operation.
The exceptions are the PostgreSQL operations ``CreateExtension``, ``CreateCollation`` and ``RemoveCollation``.
They don’t touch model state, so they are ignored.

State changes
~~~~~~~~~~~~~

While replaying, the command applies each operation to a real Django ``ProjectState``.
For an opaque operation, it compares the state before and after.
Any model, field, index, constraint or ``unique_together``/``index_together`` entry that the operation created, deleted or altered becomes *locked*.
This covers a ``SeparateDatabaseAndState`` that adds a field, or a custom operation that adds an index.
A locked field or index also keeps its model, since removing the model would remove it.
It doesn’t affect the model’s other fields, indexes or options.
A common case is a ``SeparateDatabaseAndState`` that removes a field from the state only, leaving the column to be dropped later: that field is reported as “ended by” the operation.

The command then resynchronises its picture of the history with the state, so later operations still resolve correctly.

Django also skips the schema changes of some models: unmanaged ones (``managed=False``), proxy models, and models swapped out by a setting (``swappable``).
A table or column it left behind, or never created, must stay that way.
So these models are locked, with everything they hold:

* a model whose ``managed`` option changes during its life;
* a proxy model;
* a swapped-out model.

A model that is unmanaged for its whole life can still be removed, since neither its creation nor its deletion touches the database.

The name scan
~~~~~~~~~~~~~

An opaque operation that doesn’t change state can still *use* things, such as a ``RunPython`` that queries a model.
If a model or field is removed while this runs, the migration would fail.

So the command checks every opaque operation inside a thing’s window.
If the operation’s text mentions one of the thing’s names, the removal is blocked.

The window runs from the operation that creates the thing to the one that ends it.
An opaque operation is outside the window if its migration is an ancestor of the creating migration, a descendant of the ending migration, or earlier or later in the same migration.
Migrations that aren’t ordered relative to the thing count as inside, since they might run at any point.
An example is a migration in an unrelated app with no dependency path.

Migrations from installed packages are treated a little differently.
Such a migration can only act on a project model or field if it comes after its creation in the dependency graph.
So it only counts if it is a descendant of the creating migration.
For example, Django’s own ``auth.0011_update_proxy_permissions`` doesn’t block anything in your apps.
Project migrations that aren’t ordered relative to a thing still count as inside its window, since missing dependencies between project apps are common.

The text that gets scanned is:

* The migration file, minus the operations the command understands, and minus the ``dependencies``, ``replaces``, ``run_before``, ``initial`` and ``atomic`` attributes.
  So module-level helper functions, and the arguments of the opaque operations themselves, are included, but migration names such as ``0002_item_owner`` are not.
* The source of ``RunPython`` ``code`` and ``reverse_code`` callables that are defined outside the migration file, with ``functools.partial`` unwrapped.
* The ``sql`` and ``reverse_sql`` of ``RunSQL``, as raw text.
* For custom operations, the source of their classes (outside Django).
* Operations nested in ``SeparateDatabaseAndState`` or ``RunSQL(state_operations=...)``.

Python source is parsed, and only the parts that can name a model, field, relation, table or index when the code runs are kept:

* string and bytes constants, including the literal parts of f-strings;
* attribute names (``obj.name``);
* keyword argument names (``filter(name=...)``);
* imported modules and names.

Local variables, parameters, function and class names, and comments are left out, so a loop variable called ``event`` doesn’t block a field called ``event``.
The app label argument of ``apps.get_model("app_label", "Model")`` is left out too.
Names built dynamically, with ``getattr()`` or ``**kwargs``, come from string constants in practice, and those are kept.
If the source can’t be parsed, the raw text is used instead.

The names looked for are:

* For a model: every name it had, any explicit ``db_table``, and the ``related_name`` and ``related_query_name`` of its relations.
* For a field: every name it had, ``db_column``, ``related_name``, ``related_query_name``, and a many-to-many field’s ``db_table``.
  A relation field also matches the default reverse names: every name its own model had (the default related query name, and ``<model>_set``) and that model’s ``default_related_name``.
* For an index or constraint: every name it had.
  A constraint or ``unique_together`` entry is enforced, so a data migration writing the table may rely on it without naming it (``bulk_create(..., ignore_conflicts=True)``, a caught ``IntegrityError``).
  So its model’s names and the names of its fields are looked for too.

Some operations act on every model that exists when they run, without naming them: those that call ``create_permissions``, ``create_contenttypes``, ``update_contenttypes``, ``emit_post_migrate_signal``, or ``get_for_model`` (which may be called for every model in a loop).
They create content type and permission rows for the models that existed.
Such an operation counts as mentioning every name, and an elidable one is never removed alongside something it mentions.

Matching ignores case.
A name matches where it doesn’t touch other letters or digits, in any script, so non-ASCII names work.
Underscores count as boundaries.
So ``coupon`` matches ``Coupon``, ``coupon_set``, ``shop_coupon`` and ``coupon__code``, but not ``coupons``.

This is a heuristic, with limits:

* It can’t follow code that it can’t see.
  If a data migration imports a helper from another module, only the helper’s own function is scanned, when it is passed to ``RunPython`` directly.
  Anything the helper calls in turn is not.
  Neither are names built dynamically, such as ``apps.get_model("shop", name)`` where ``name`` comes from elsewhere.
* It matches words, not meaning.
  A field called ``name`` will match many things, and is blocked often.
  A field called ``rank`` is blocked by any mention of the word ``rank``.
  False matches only keep things, which is the safe direction.
* If the source of a ``RunPython`` callable can’t be read at all, such as a C-implemented or dynamically created function, it counts as matching every name.
* Names assembled by string concatenation or formatting are missed.

Where the scan can’t be relied on, the trial (the default) checks by running the operations instead.

Elidable operations
~~~~~~~~~~~~~~~~~~~

An elidable operation is a ``RunPython`` or ``RunSQL`` operation (the exact classes) with ``elidable=True`` that changes no state.
It never blocks.
If a removal would leave it mentioning a name that no longer exists, the command removes it too, as long as:

* its migration is certainly within the removed thing’s lifetime: a descendant of (or later in) the creating migration and an ancestor of (or earlier in) the ending one,
* it is in an editable migration with a locatable ``operations`` list, and
* its app isn’t excluded.

Otherwise it blocks like any other opaque operation.
Each such removal is reported with the name that matched and the thing it was removed with.

The whole operation is removed, so an elidable ``RunSQL`` that also does unrelated things, such as creating an index on another table, loses those too.
``squashmigrations`` has the same contract, as it drops elidable operations wholesale: only mark operations elidable if all of their effects can go.

An elidable operation is only removed this way when it mentions a removed model, field, index or constraint.
Unrelated elidable operations stay, unless you pass ``--elidable``.

Superseded operations
---------------------

After removing whole lives, the command looks for operations that a later one makes pointless.
It only removes them where it can show that the intermediate state is never seen.

**AlterField.**
An ``AlterField`` is removed when the next ``AlterField`` or ``RemoveField`` on the same field replaces it, and:

* Nothing between them refers to the field, and no ``RenameField`` falls between.
* The scan of opaque operations between them finds neither the field’s names nor its model’s.
  Models are scanned too, since a data migration may use the field through the model.
* The ``AlterField`` may rewrite existing rows: when it makes the field ``NOT NULL`` (Django fills ``NULL`` values with its default), or when it changes the field class.
  If it does, no data migration mentioning the model may run between the model’s creation and the ``AlterField`` (or the trial runs must pass): rows written by one would keep their old values.
* The field is not many-to-many, and was never a primary key.
  The primary key flag can’t change between the definitions involved.
* The type change is safe.
  Take the definitions before the ``AlterField``, the one it makes, and the one that replaces it.
  Either the first two, or the last two, must be the same kind: same field class, same internal type, and for relations the same target model.
  This avoids turning two possible casts into a single impossible one, for example, ``IntegerField`` to ``JSONField`` to ``TextField``, which some databases reject even on empty tables.
  When a ``RemoveField`` replaces it, this check isn’t needed.
* The definitions before and after the ``AlterField`` hold the same things.
  Without the ``AlterField``, the earlier definition stays in force until the replacing operation.
  Anything it occupies or points to that the newer one gave up could be reused or removed meanwhile, which the project state can’t show.
  So both must agree on the column (``db_column``, and whether it is a foreign key column), ``to_fields``, ``from_fields``, ``through``, ``db_table``, ``db_tablespace``, primary key status, and everything the definition refers to: the target model and field, and fields named in expressions.
* Every definition in the chain agrees on ``unique`` and on whether, and how, the field has a check constraint, and no foreign key points at the field.
  Otherwise the database objects would get different names (see `Folding`_).

**AlterModelOptions.**
Removed if a later ``AlterModelOptions`` on the same model replaces it, the earlier one doesn’t change ``managed``, and the scan is clean.
Only the standard options Django manages through this operation are considered.

**AlterModelManagers.**
Removed if a later ``AlterModelManagers`` replaces it, and the scan is clean.

**AlterModelTableComment.**
Removed if a later ``AlterModelTableComment`` replaces it, nothing else on the model falls between them (including children, and anything referring to it), and the scan is clean.

``AlterModelTable`` is never treated as superseded: skipping one would keep the old table name taken while another model may claim it.

Chains collapse as you would expect, with each decision made against the operations that survive.
The two migrations must also be ordered relative to each other in the graph.
If they could run in either order, nothing is removed.

Redundant together operations
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

After together-entries are removed, an ``AlterUniqueTogether`` or ``AlterIndexTogether`` might set the value to the same thing as before.
The command removes these operations too, if they are in an editable migration.
This counts as part of the ``together`` kind.

Verification
------------

The command doesn’t trust its own analysis.
Before reporting anything, it checks the plan:

1. It builds the edited operation lists, and replays them in memory against Django’s ``ProjectState``.
   The replay must not raise.
2. At the end of every migration, the set of relations pointing at models that don’t exist must be no larger than in the original history.
   This catches a removed model that something still relies on.
3. The final state of every model, including fields, options, bases and managers, must equal the original’s.
   Together options are compared ignoring their formatting.

It then generates the new source of each file, and checks that it compiles.
If any of these checks fail, the command stops with an “Internal error” message, and nothing is written.
That is a bug in django-compact-migrations, and the message asks you to report it.
Meanwhile, you can work around it by excluding the thing involved with ``--exclude``.

When applying, there is a second round after the files are written.
The command builds a fresh ``MigrationLoader`` that re-reads the files from disk.
It checks that the graph has not changed, that every migration has the operations it expected, and that the final project state is the same as before.
If anything is off, it puts the original text back in every file it wrote, and fails with an error.

Folding
-------

Most of a long history is not “create, then delete”, but “create, then alter many times”.
Folding moves a later operation into the operation that created the thing it changes, and deletes the later one.
It is like Django’s squash optimizer, across the whole graph, but without changing migration files, names or dependencies.

Below, *P* is the operation that creates (and keeps carrying) the definition, and *E* the later operation folded into it.
The window is everything that may run between them, with the same rules as for `The name scan`_.

What can be folded:

1. An ``AlterField`` into the ``AddField``, or ``CreateModel`` field entry, that added the field.
   If the ``AddField`` had a default and the new definition has none, the default is kept as a one-off (``preserve_default=False``), since the table may already hold rows when the column is added.
2. An ``AddField`` into the model’s ``CreateModel``, appended to its fields.
3. An ``AddIndex`` or ``AddConstraint`` (or their PostgreSQL variants) into the ``CreateModel``’s ``indexes`` or ``constraints``.
4. The model’s first ``AlterUniqueTogether`` or ``AlterIndexTogether`` into the ``CreateModel`` options.
5. The model’s first ``AlterModelOptions`` (unless it changes ``managed``), ``AlterModelManagers`` or ``AlterModelTableComment`` into the ``CreateModel``.
6. A ``RenameField`` into the ``AddField`` or ``CreateModel`` entry that created the field, which then gets the new name.
   Django’s ``RenameField`` renames the column only.
   Indexes (including PostgreSQL’s ``_like`` ones), unique and check constraints, foreign key constraints and many-to-many tables keep names made from the old column or field name.
   A field created under the new name gets names made from the new one.
   So only these fields are folded:

   - a field without any of these: not unique, not indexed, not a relation, no check constraint (``PositiveIntegerField`` and the like), not generated, never a primary key;
   - or a field whose column is fixed by ``db_column``, which the rename doesn’t change.

   Also, nothing may refer to the field between the two (indexes, constraints, ``unique_together``, other fields’ ``to_field``), the ``CreateModel``’s options may not name it, no other field may ever have had the new name, and the scan for both names must be clean.
7. An ``AlterModelTable`` of an unmanaged model into the ``CreateModel``’s ``db_table``: there is no table to rename.
   The model must have ``managed=False`` in its ``CreateModel``, with nothing else happening to it in between.
8. A ``RenameIndex`` (by ``old_name``) into the ``AddIndex`` or ``CreateModel`` entry that defines the index, which then gets the new name.
   No other index or constraint in the history may ever have had any of the index’s names, and the scan for those names must be clean.
9. An ``AlterConstraint`` into the ``AddConstraint`` or ``CreateModel`` entry that defines the constraint.
   ``AlterConstraint`` changes nothing in the database, so it is only folded if the two definitions differ in nothing but the validation message and code.
10. The model’s first ``AlterOrderWithRespectTo`` into the ``CreateModel`` options.
    The table then has its ``_order`` column from the start.
    So the model’s scan must be clean (rows written in between would be numbered differently), the field must be in the ``CreateModel`` under the same name, and the model may have no field of its own called ``_order``.
    One that removes or changes an ordering the ``CreateModel`` set is only folded if nothing else happens to the model in between.

``RenameModel`` is never folded, nor is ``AlterModelTable`` of a managed model.
Django renames the table only, so its primary key constraint, sequence or identity, indexes, foreign keys and many-to-many tables keep names made from the old table name, which a table created under the new name wouldn’t have.

Conditions for every fold:

* Both operations are in editable migrations, the things involved aren’t locked or being removed, and P doesn’t lose anything else in the same step.
* Everything the folded definition needs exists when P runs.
  A referenced model or field must be created in a migration P depends on, or earlier in P’s migration, or be in the ``CreateModel`` itself.
  No dependency is ever added.
* Nothing is occupied earlier than before in a way that could collide:

  - a field folded into a ``CreateModel`` must not share a name or column with any other field of the model that is kept;
  - an index or constraint name must not be used by anything else;
  - an ``AlterField`` may not change the column, ``to_field``, ``through``, table or tablespace.

* Nothing is released later than before: whatever the old definition referred to and the new one doesn’t must not be needed meanwhile.
  The exception is a foreign key that just points at a different model, when the new model already exists at P.
  The old model then becomes unreferenced, and may in turn become removable.
* Nothing that stays between P and E refers to the field being changed.
* Every field and model the folded definition names has the same name at P as at E: no ``RenameField``, ``RenameModel`` or ``AlterModelTable`` of them in between.
* Every database object gets the name it got in the original history.
  Django’s schema editor names some objects differently when it creates them with the table than when it adds them later, so these folds are not made:

  - A field that gains ``unique=True`` from an ``AlterField`` gets a hashed ``..._uniq`` constraint.
    A unique column created with the table (``CREATE TABLE`` or ``AddField``) gets the database’s own name (``..._key`` on PostgreSQL).
  - Likewise, a check constraint added by an ``AlterField`` (``PositiveIntegerField``, ``JSONField`` on some backends, custom fields with ``db_check()``) is named ``..._check`` by Django, while an inline one is named by the database.
    So an ``AlterField`` is only folded or superseded when the definitions involved agree on ``unique`` and on the kind of field that decides the check.
  - When an ``AlterField`` changes a field’s type, Django drops and recreates the foreign keys pointing at it with a ``..._fk`` suffix, instead of the usual ``..._fk_<table>_<column>``.
    So no foreign key or many-to-many field is folded, or created by a fold, before an ``AlterField`` of the field it points at.
    A field that foreign keys point at is never superseded or folded.
    And an ``AddField`` is only folded into a ``CreateModel`` when no such ``AlterField`` falls between.
  - A field whose class doesn’t come from ``django.db.models`` (``HStoreField``, a custom field) may need something that a ``RunSQL`` or custom operation sets up, such as an extension or a type.
    Its column would be created earlier after a fold, so it isn’t folded across any opaque operation other than ``RunPython``.
    The same goes for a field with a ``db_collation``, since such an operation may create the collation (NetBox creates ``natural_sort`` that way).

  Index, foreign key and ``unique_together`` names come from the same functions either way, from the table and column names.
  Those must not change in the window, which the rule above ensures.
  These rules were derived from Django’s ``BaseDatabaseSchemaEditor`` (``_iter_column_sql``, ``_alter_field``).
  They were checked by comparing ``pg_dump --schema-only`` of databases migrated with the original and the folded histories of large projects.
* A folded field, index or constraint goes at the end of the ``CreateModel``’s list, so a table’s columns may come out in another order: see :ref:`what-may-change`.
* The opaque operations in the window pass the name scan, or the trial runs pass.
  The scan looks for the field and its model, the model, or the index or constraint name, and also for the model of anything enforced, such as a constraint.
* An ``AlterField`` folded into an earlier ``AddField`` gives rows that already exist when the column is added the folded definition’s default, instead of the ``AddField``’s.
  Unless the type and defaults are the same, no data migration mentioning the model may run between the model’s creation and the ``AddField`` (or the trial runs must pass).
* P can be rewritten:

  - Django’s migration writer must be able to serialize it;
  - every import it needs must be a normal module path that doesn’t clash with a name the file already binds to something else;
  - the written source must load back to an operation with the same effect.

  For example, an ``AlterModelManagers`` that sets just the default manager can’t be folded, since a ``CreateModel`` can’t state that.

Folds enable each other, and can make more things removable: a foreign key retargeted at P leaves its old model unreferenced.
So the command analyses the edited history again, removing, superseding and folding, until nothing changes, for at most ten rounds.

The edited history is replayed in memory and on disk, as for any other change.
Model state replays use copies of the field objects, since Django’s ``RenameField`` rewrites ``to_field`` and similar attributes of fields in the state in place.

.. _trial-mode:

Trial mode
----------

In a trial (unless ``--static``), the name scan and the soft-reference rule stop being blockers.
Their hits are recorded instead: each removal (a whole life, or a superseded operation) keeps the list of opaque operations that mentioned it.
Every other rule stays: references, locking, editability, the primary key rule, exclusions and the superseded guards.

A trial goes like this:

1. The original history is migrated on an empty scratch database, and a snapshot is taken of the schema and data (see `Snapshots`_).
   If that fails, the command stops: the history doesn’t migrate from scratch, so nothing can be learnt from trials.
2. A candidate plan is made in which the name scan doesn’t block anything, but each removal remembers which data migrations mentioned it.
   The command says how many removals a trial could unlock at most: if none, only the static plan is trialled.
   Then the original history is migrated a second time.
   Values that differ between the two baseline runs (times, random values) are left out of every comparison.
3. The candidate is migrated on a fresh scratch database.
   The run substitutes the edited operation lists on a private ``MigrationLoader`` and runs Django’s ``MigrationExecutor``, so the files on disk are untouched.
   Opaque operations that don’t change the project state are wrapped.
   If one fails, it rolls back to a savepoint, the failure is recorded, and the run goes on as if the operation did nothing, so one run finds many failures.
   That needs a backend that can roll back schema changes, such as PostgreSQL or SQLite, and an atomic migration; otherwise the run stops at the first failure.
   Any other failure ends the run.
   A run with failures isn’t compared to the baseline, since the skipped operations explain any difference.
4. Failures are blamed as narrowly as possible:

   * An error that names what is missing (``Cannot resolve keyword 'x'``, ``has no field named 'x'``, ``object has no attribute 'x'``, ``column "x" does not exist``, ``doesn't have a 'X' model``, ``No installed app with label 'x'``, ``relation "t" does not exist``, and their SQLite and MySQL forms) blocks exactly the removals of that field, model, table or app.
   * Any other failure is blamed on the removals the failing operation mentioned (narrowed to those named in the error message, if any), or else on those whose window includes the failing migration.
     Only half of them are blocked at first; if the same failure comes back in the next run, the other half is blocked too.
     Only the first such failure of a run is used, since later ones may follow from it.
   * A snapshot that differs from the baseline blocks the removals relating to the differing table.
5. The candidate is solved again and run again, until a run has no failures and a snapshot identical to the baseline.
   After 8 runs, or when a run blames nothing new, the command falls back to the static plan, and still trials it.

Removals that the name scan would have kept, folds and no-op data migrations are the first suspects.
The static plan’s own removals are blamed only when nothing else explains a failure.
The analysis can’t see everything (a model a data migration names by a computed string, database objects whose names depend on the backend’s history), so none is taken as proven once a trial run differs.
Only when nothing is left to blame are ``--prune`` and then ``--noop-data`` given up, with a note (``verification.dropped`` in the plan file).
If even that plan fails, ``plan`` exits with an error and ``apply`` writes nothing.

A trial migrates the ``--database`` alias only.
A ``RunPython`` or ``RunSQL`` operation that ``DATABASE_ROUTERS`` keep from running on that alias can’t be checked.
What it mentions stays blocked as in the static plan, and it is never taken for a no-op.

Snapshots
~~~~~~~~~

A snapshot covers:

* On SQLite: every table’s columns, foreign keys, indexes and check constraints, plus index, trigger and view definitions.
  These are read through ``PRAGMA`` statements, since the stored ``CREATE TABLE`` text depends on how a table was built.
* On PostgreSQL: columns (type, length, precision, nullability, default, identity, collation, generation), constraints (``pg_get_constraintdef``), indexes (``pg_get_indexdef``), sequences and their owners, views, materialized views, functions and procedures, triggers, enum types, extensions and comments.
* On MySQL and MariaDB: what ``mysqldump --no-data`` shows, from ``information_schema``.
  That is tables (engine, collation, comment, options), columns (type, nullability, default, extra, collation, comment, generation), indexes and their names (columns or expressions, order, prefix length, uniqueness, type, comment, visibility), constraints, foreign keys (columns, targets, ``ON UPDATE``/``ON DELETE``), check constraints by table, triggers, views and routines.
  The tests compare it on both servers.

Columns are compared by name, not in their order.
The next id each table gets (a PostgreSQL sequence’s last value, MySQL’s ``AUTO_INCREMENT``, SQLite’s ``sqlite_sequence``) isn’t compared: see :ref:`what-may-change`.
Data is compared as a multiset of rows per table, with ``django_migrations`` compared by app and name only.

Every run sends ``pre_migrate`` first and ``post_migrate`` at the end, as ``migrate`` does, so the content types and permissions it creates are compared too.
Their ids are not compared, since they may change (see :ref:`what-may-change`).
Content types and permissions are compared by app label, model and codename, and so are foreign keys to them, so a row that points at one must still point at the same one.

SQLite keeps no usable names for unique and foreign key constraints, and the trial there compares only which columns a check constraint covers, not its condition.

No-op data migrations
~~~~~~~~~~~~~~~~~~~~~

With ``--noop-data``, the command records every statement each candidate data migration executes while the original history runs on the first scratch database.
It records statements on every connection, each with the number of rows it affected.
A candidate is an exact ``RunPython`` or ``RunSQL`` operation (not a subclass) that doesn’t change the project state, in an editable migration of a selected app that isn’t excluded.
It counts as a no-op only if every statement it ran was:

* a read: a ``SELECT`` (or ``WITH`` or ``VALUES``) that calls no function other than the usual aggregates, comparisons and conversions, and contains no ``INSERT``, ``UPDATE``, ``DELETE``, ``MERGE``, ``INTO``, ``nextval`` or ``setval``;
* transaction control (savepoints, ``BEGIN``, ``COMMIT``...), ``SET CONSTRAINTS`` or ``SET LOCAL``;
* or an ``UPDATE`` or ``DELETE`` that affected no rows.

Anything else counts as a write: ``INSERT``, ``COPY``, any schema change, other ``SET`` statements, unknown functions, sequence functions, an unknown row count, and any statement on another database alias.

A ``RunPython`` operation is also kept when its function’s source (and that of the functions of the same module it calls) shows effects that queries don’t:

* connecting signal handlers (``.connect()``, ``receiver``, ``pre_migrate``/``post_migrate``);
* looking at the backend (``.vendor``, ``.features``, ``connection.ops``, sequence resets);
* files, the network, caches, email, ``call_command()`` or subprocesses;
* a source that can’t be read;
* no query at all, yet calls to anything other than ``apps.get_model()`` and the like.

These are listed as kept, in the trial’s output and under ``verification.noop_kept`` in the plan file.

Pruning
-------

With ``--prune``, after everything else, migrations left with no operations are deleted.
A migration is kept when:

* it is the first or the latest migration of its app, or marked ``initial`` (so merges, ``--fake-initial`` and ``__first__``/``__latest__`` dependencies behave as before);
* it is a squashed migration, or a squashed migration replaces it;
* it has ``run_before``, or another migration names it in ``run_before``;
* a migration depending on it can’t be edited (an installed package, an excluded app), or doesn’t list it as a plain ``("app", "name")`` tuple in a literal ``dependencies`` list;
* another migration file mentions its name other than in its dependencies, for example to import a function from it with ``import_module()``;
* its app isn’t selected, or is excluded.

Keeping the first and latest migration of every app also means ``swappable_dependency()`` (which means ``__first__``) and Django’s inference of ``initial`` resolve as before.

Whatever else the deleted file held (functions of the data migrations removed from it, ``atomic = False``...) goes with it.
``apply`` deletes the files (and their bytecode), and writes them back if anything fails.
Plan files record each deletion with the hash of the file.
A trial runs the pruned graph too, and expects exactly the pruned migrations to be missing from ``django_migrations``.

For a deleted migration P, every surviving migration that depended on it gets P’s dependencies instead, following chains of deleted migrations.
It skips those it already runs after through its other dependencies.
Dependencies within the same app are the exception.
Django finds an app’s first and latest migrations, and conflicting branches, through same-app dependencies only.
So a migration that depended on a deleted migration of its own app always gets that migration’s nearest surviving same-app dependencies (both branches of a deleted merge migration), even when it already runs after them through another app.
Only literal ``("app", "name")`` entries are rewritten, in the dependants’ ``dependencies`` lists.
Those lists are regenerated, keeping their other entries’ source text.

The pruned graph must keep these invariants:

* The set of migrations every surviving migration runs after (its ancestors in the graph) is exactly the original set minus the deleted migrations.
  No ordering constraint between the migrations that do something is lost or added.
* The roots and leaves of every app, as ``migrate`` sees them when it checks for conflicts, are the original ones minus the deleted migrations.
* Django’s plan for the pruned graph is exactly the original plan minus the deleted migrations.
  Keeping the ancestors isn’t enough for that.
  Django walks the graph depth first, following each migration’s dependencies sorted by name.
  So a dependency moved from a deleted migration to its dependant can make two unordered migrations swap, say two that add columns to the same table.
  If the plan isn’t kept, migrations are pruned one at a time, in plan order, keeping each only if the order still holds.

Each invariant is checked on the planned graph, and again on the graph loaded from the rewritten files.
That graph must also contain exactly the surviving migrations and be consistent.
The roots and leaves are also checked in trial runs.

MySQL and MariaDB
-----------------

The analysis reasons about Django’s project state, which is the same on every backend.
On MySQL and MariaDB, two parts of the schema also depend on how the database got there.
So a removal or fold that the state calls harmless can change the schema:

* **The index backing a foreign key.**
  InnoDB needs an index starting with a foreign key’s column:

  - Creating the foreign key creates one implicitly, named like the constraint (``…_fk_<table>_<column>``), unless an index starting with that column exists already.
  - The server drops it when another index starting with the column is created.
  - Before dropping such an index, Django creates its own index on the column, named ``<table>_<column>_<hash>``.

  So, when any configured database is MySQL or MariaDB, the command keeps:

  - an index, unique constraint or together-entry whose first column is a foreign key (unless that column is dropped later anyway): removing it removes those renames;
  - a fold of an ``AlterUniqueTogether`` or ``AlterIndexTogether`` that drops such an entry from the ``CreateModel`` (the same thing), and a fold that would create such an index earlier, while another one on the same foreign key is dropped in between;
  - folds of ``AlterField``, and superseded ``AlterField`` operations, on a field that is or becomes a foreign key, when they change whether it is one, its ``db_constraint``, ``unique`` or ``db_index``.

  Changing a foreign key’s target or ``on_delete``, and folding indexes into the ``CreateModel``, keep the same names: those still go.
* **Defaults of TEXT, BLOB and JSON columns, on MySQL only.**
  ``AddField`` of a ``TextField``, ``BinaryField`` or ``JSONField`` with a (non-empty) default adds the column with ``DEFAULT (…)``.
  It never drops that default, as MySQL can’t, while ``CreateModel`` creates the column without one.
  So, when a configured database is MySQL, such an ``AddField`` isn’t folded into its ``CreateModel``, and ``AlterField`` operations on such fields are neither folded nor dropped as superseded.
  MariaDB drops the default as other backends do, so this rule doesn’t apply to it.

A plan doesn’t know which database production runs, so these rules apply when any configured database uses the MySQL backend.
Telling MySQL from MariaDB needs the server, which is asked for its version.
A server that can’t be reached counts as MySQL, whose rules are a superset.
Plans for projects without MySQL or MariaDB are unchanged.
The plan file records the servers the rules were applied for, under ``options.backends``.
A trial run on a MySQL or MariaDB database (``--database …``) compares index names and defaults, so it checks what these rules can’t foresee.

Scratch databases
-----------------

A scratch database uses the backend and settings of the ``--database`` alias.
For SQLite it is a temporary file.
For PostgreSQL and MySQL it is a new database named ``compactmig_<random>_<name>``, created next to the real one.

While migrating, every configured alias is pointed at a scratch database, as Django’s test runner does.
So project code that uses Django’s connections can’t reach a real database.
Code that opens its own connections, or an alias whose ``OPTIONS`` name another database, still can.

* The chosen alias, aliases for the same database (same engine, host, port and name, such as a read replica), and test mirrors share one scratch database.
* Every other database gets its own, empty scratch database: only the chosen alias is migrated.
* Aliases using the dummy backend are left alone, and any alias on another backend makes the command refuse to run.

Every scratch database is dropped afterwards, after closing every connection, also on errors, Ctrl+C and ``SIGTERM``.
Ctrl+C ends a run with exit status 130, with no migration file changed.
Each run happens in its own child process (``os.fork()``), so nothing it leaves in the process (module-level caches, changed field objects, registered handlers) can affect the next run.
Where ``os.fork()`` doesn’t exist (Windows), runs happen in-process, with a warning.

Limitations
-----------

* **Squashed migrations that are still on disk are skipped.**
  Django uses the squashed migration, so that is the one the command processes.
  The migrations it replaces are neither scanned nor edited.
  Delete them once the squash is applied everywhere.
* **Folds rewrite operations.**
  An operation that absorbs a fold is regenerated with Django’s migration writer, so it loses its formatting and comments.
  Use ``--skip-kind fold`` to keep every operation as written.
* **Conservative about a few things.**
  A field that was ever a primary key is never removed on its own.
  ``AlterField`` operations on it, or on many-to-many fields, are never treated as superseded.
  ``order_with_respect_to`` blocks removing the field it names, unless the model goes too.
  A custom operation or ``SeparateDatabaseAndState`` that changes state keeps everything it touches.
* **The name scan is a heuristic** (see `The name scan`_).
  It can miss a use that is hidden in helpers, dynamic names, or raw SQL built at run time.
  Review ``RunPython`` and ``RunSQL`` operations that survive near removed things, and test the compacted history on a fresh database.
* **Without a trial, equivalence is checked at project-state level.**
  With ``--static``, verification compares Django’s model of your schema, not the schema itself.
  Two histories with the same project state normally build the same database, but they can differ in ways that Django’s state can’t express.
  For example, a ``RunSQL`` that you rely on for its side effect, or database-specific details.
  A trial compares the schema itself, but only on one backend.
  Diff the schema before and after on a fresh database, as shown in :ref:`verifying-your-schema`.
* **On SQLite, a static plan can keep what a table rebuild used to drop.**
  Django changes most things on SQLite by rebuilding the table, which drops any index or trigger that a ``RunSQL`` created on it.
  Removing or folding the operation that caused a rebuild leaves such an object in place.
  A trial on SQLite sees the difference; ``--static`` doesn’t look for it.
* **A model whose** ``unique_together`` **or** ``index_together`` **names** ``_order`` **is left alone.**
  The ``_order`` column of ``order_with_respect_to`` isn’t tracked as a field, so nothing of such a model is removed or folded.
* **Existing databases must already be migrated.**
  The compacted history is for fresh databases.
  A database that is only partway through the original history may not end up with the same schema (see :ref:`before-you-start`).
