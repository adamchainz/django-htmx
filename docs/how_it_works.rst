How it works
============

This page explains how django-clean-migrations decides what is safe to remove.
You don’t need to read it to use the command, but it helps to understand why something was kept, or what the tool can’t know.

The goal is simple: a fresh database migrated with the cleaned history must end up with exactly the same schema as one migrated with the original.
Everything below serves that goal, and when the command is unsure, it keeps the operation.

Reading the history
-------------------

The command loads your migrations with Django’s ``MigrationLoader``, without touching any database.
It then replays every migration in the same order that ``migrate`` would run them, tracking the state of every model as it goes.

Squashed migrations are applied as Django does: the squashed migration is used, and the migrations it replaces are skipped.
If replaced migrations are still on disk, the command prints a warning saying how many it skipped.
They are neither analysed nor edited: only the squashed migration is cleaned.
That is fine as long as every database has the squashed migration recorded as applied, which is the usual precondition for deleting the replaced files anyway; delete them before or after cleaning.

Which migrations are edited
~~~~~~~~~~~~~~~~~~~~~~~~~~~

A migration is a *project migration*, and may be edited, when the path of its file has no ``site-packages`` or ``dist-packages`` component.
It’s the file that counts, not the app, so if you point ``MIGRATION_MODULES`` at a package inside your project, those migrations are editable.
Migrations from installed packages are replayed, because their operations affect which references exist, but never edited.

To edit a file, the command also needs to find each operation in the source.
It parses the file, and looks for a single ``operations = [...]`` assignment in ``class Migration``.
The migration is only editable if that is a plain list or tuple literal, with no ``*`` unpacking, whose entries are calls matching the real operations one-to-one.
If a migration builds its operations dynamically, or has any other unusual shape, it can’t be edited, and anything that would need an edit there is kept.
With ``--show-blocked``, you’ll see the reason, such as “operations list in app.0004_x could not be parsed”.

Files that start with a UTF-8 byte order mark (BOM) are never edited either; the reason given says so.

Lives
-----

The command follows the *life* of each thing, rather than its name.
Names are unreliable: models and fields get renamed, and a name can be reused by a different model or field later.

A life begins when something is created, and ends when it is deleted:

* A model life runs from ``CreateModel`` to ``DeleteModel``.
* A field life runs from ``AddField`` (or an entry in a ``CreateModel``) to ``RemoveField``.
* Index and constraint lives run from their creation, in ``AddIndex``/``AddConstraint`` or in a ``CreateModel``, to ``RemoveIndex``/``RemoveConstraint``.
* A together life is one entry in ``unique_together`` or ``index_together``.
  It lasts for as long as later values of the option still contain the entry.

Operations in between, like ``AlterField``, ``RenameField``, ``RenameIndex`` and ``AlterConstraint``, belong to the life they change.
A life that begins and ends is a candidate for removal: if all its operations go, nothing it did remains.

Some lives are never candidates:

* A life with no end, since it still exists in the final state.
* A life touched by an opaque operation that changes the project state (see below).
* A field that was ever a primary key, even briefly.
  Other tables’ columns are derived from a primary key, so the field is never removed on its own.
  (A whole model can still be removed, primary key included.)
* A model or field whose name was reused in a way that can’t be tracked, for example, a ``CreateModel`` for a name that already exists.

Parts of operations
~~~~~~~~~~~~~~~~~~~

Operations are not always the unit of removal.
A ``CreateModel`` can define fields, indexes, constraints and together-entries that have different lives, so the command can take just one of them out, leaving the rest of the ``CreateModel`` in place.
These count as “edited” operations in the summary.

For this, the command needs to find the entry in the source.
It must be a literal list or tuple (``fields``) or dict (``options``) of the expected shape.
If it can’t, the thing it belongs to is kept.

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

If a reference exists from an operation that won’t be removed, the thing it points to is kept.
The reason shows as, for example, “referenced by AddField in blog.0005_x”.

This is solved together, not one at a time.
The command starts by assuming everything selected can go, then repeatedly puts back anything with a remaining blocker, until nothing changes.
That is how a model and the foreign keys that point to it, possibly in other apps, are removed together, or kept together.

*Soft references* are a weaker kind.
A model’s ``ordering`` and ``get_latest_by`` options can name a field: directly, with a ``-`` prefix, through a lookup such as ``fk__name`` (which names the local ``fk``), or in an ``F()``/``OrderBy`` expression.
The option may even name the field before it is added, as happens in squashed migrations, so the command tracks the periods during which an option names an existing field.
That doesn’t block removing the field by itself.
But if an opaque operation might query the model during such a period, a stale default ordering could crash the query, so the field is kept.
The scan for that looks for the model’s names (including its tables and the related names of its relations), the names of models that inherit from it (they inherit ``Meta.ordering``), and the names of relation fields on any model that point at it, since related managers and ``prefetch_related`` use the default ordering too.

Opaque operations
-----------------

Operations the command knows precisely are the ones built into ``django.db.migrations`` for models, fields, indexes and constraints, along with the PostgreSQL equivalents of some of them.
They are recognised by their exact class, not by ``isinstance``, because a subclass may do anything.

Everything else is *opaque*: ``RunPython``, ``RunSQL``, ``SeparateDatabaseAndState``, and any custom, third-party or subclassed operation.
(The PostgreSQL operations ``CreateExtension``, ``CreateCollation`` and ``RemoveCollation`` are the exception.
They don’t touch model state, so they are ignored.)

State changes
~~~~~~~~~~~~~

While replaying, the command applies each operation to a real Django ``ProjectState``.
For an opaque operation, it compares the state before and after.
Any model, field, index, constraint or ``unique_together``/``index_together`` entry it created, deleted or altered becomes *poisoned*: never removable, and never the subject of a superseded removal.
This covers a ``SeparateDatabaseAndState`` that adds a field, or a custom operation that adds an index.
A poisoned field or index also keeps its model, since removing the model would remove it, but it doesn’t affect the model’s other fields, indexes or options.
A common case is a ``SeparateDatabaseAndState`` that removes a field from the state only, leaving the column to be dropped later: that field is reported as “ended by” the operation.

The command then resynchronises its picture of the history with the state, so later operations still resolve correctly.

The name scan
~~~~~~~~~~~~~

An opaque operation that doesn’t change state can still *use* things, such as a ``RunPython`` that queries a model.
If a model or field is removed while this runs, the migration would fail.

So under ``--opaque scan``, the command checks every opaque operation inside the window during which a thing exists.
If the operation’s text mentions one of the thing’s names, the removal is blocked.

The *window* runs from the operation that creates the thing to the one that ends it.
An opaque operation is outside the window if its migration is an ancestor of the creating migration, a descendant of the ending migration, or earlier or later in the same migration.
Migrations that aren’t ordered relative to the thing, for example in an unrelated app with no dependency path, count as inside, since they might run at any point.

The text that gets scanned is:

* The migration file, minus the operations the command understands, and minus the ``dependencies``, ``replaces``, ``run_before``, ``initial`` and ``atomic`` attributes.
  So module-level helper functions, and the arguments of the opaque operations themselves, are included, but migration names such as ``0002_item_owner`` are not.
* The source of ``RunPython`` ``code`` and ``reverse_code`` callables that are defined outside the migration file, with ``functools.partial`` unwrapped.
* The ``sql`` and ``reverse_sql`` of ``RunSQL``, as raw text.
* For custom operations, the source of their classes (outside Django).
* Operations nested in ``SeparateDatabaseAndState`` or ``RunSQL(state_operations=...)``.

Python source is parsed, and only the parts that can name a model, field, relation, table or index when the code runs are kept: string and bytes constants (including the literal parts of f-strings), attribute names (``obj.name``), keyword argument names (``filter(name=...)``), and imported modules and names.
Local variables, parameters, function and class names, and comments are left out, so a loop variable called ``event`` doesn’t block a field called ``event``.
The app label argument of ``apps.get_model("app_label", "Model")`` is left out too.
Names built dynamically, with ``getattr()`` or ``**kwargs``, come from string constants in practice, which are kept.
If the source can’t be parsed, the raw text is used instead.

Migrations from installed packages are treated a little differently.
Such a migration can only act on a project model or field if it comes after its creation in the dependency graph, so it only counts if it is a descendant of the creating migration.
For example Django’s own ``auth.0011_update_proxy_permissions`` doesn’t block anything in your apps.
Project migrations that aren’t ordered relative to a thing still count as inside its window, since missing dependencies between project apps are common.

The names that are looked for are:

* For a model: every name it had, any explicit ``db_table``, and the ``related_name`` and ``related_query_name`` of its relations.
* For a field: every name it had, ``db_column``, ``related_name``, ``related_query_name``, and a many-to-many field’s ``db_table``.
  A relation field also matches the default reverse names: every name its own model had (the default related query name, and ``<model>_set``) and that model’s ``default_related_name``.
* For an index or constraint: every name it had.

Matching ignores case.
A name matches where it is not touching other letters or digits (in any script, so non-ASCII names work), so underscores count as boundaries.
That means ``coupon`` matches ``Coupon``, ``coupon_set``, ``shop_coupon`` and ``coupon__code``, but not ``coupons``.

This is a heuristic, and has limits:

* It can’t follow code that it can’t see.
  If a data migration imports a helper from another module, only the helper’s own function is scanned when it is passed to ``RunPython`` directly.
  Anything the helper calls in turn is not.
  Neither are names built dynamically, such as ``apps.get_model("shop", name)`` where ``name`` comes from elsewhere.
* It matches words, not meaning.
  A field called ``name`` will match many things, and blocks often.
  A field called ``rank`` is blocked by any mention of the word ``rank``.
  False matches only keep things, which is the safe direction.
* If the source of a ``RunPython`` callable can’t be read at all, such as a C-implemented or dynamically created function, it counts as matching every name.
* Names assembled by string concatenation or formatting are missed.

The cases where it is unsafe to rely on the scan are exactly the ones where ``--opaque block`` is useful.
It is stricter, since any opaque operation that might run in the window blocks.
``--opaque ignore`` turns the checks off.
State changes by opaque operations still poison things, and verification still runs.

Elidable operations
~~~~~~~~~~~~~~~~~~~

A ``RunPython`` or ``RunSQL`` operation (the exact classes) with ``elidable=True``, that changes no state, never blocks.
If a removal would leave it mentioning a name that no longer exists, the command removes it too, as long as:

* its migration is certainly within the removed thing’s lifetime: a descendant of (or later in) the creating migration and an ancestor of (or earlier in) the ending one,
* it is in an editable migration with a locatable ``operations`` list, and
* its app isn’t excluded.

Otherwise it blocks like any other opaque operation.
Each such removal is reported with the name that matched and the thing it was removed with.

The whole operation is removed, so an elidable ``RunSQL`` that also does unrelated things, such as creating an index on another table, loses those too.
That is the same contract as ``squashmigrations``, which drops elidable operations wholesale: only mark operations elidable if all of their effects can go.

This only removes the elidable operation when it mentions a removed model, field, index or constraint.
Unrelated elidable operations stay, unless you pass ``--elidable``.

Superseded operations
---------------------

After removing whole lives, the command looks for operations that a later one makes pointless.
These are limited to cases where it can show the intermediate state is never seen.

**AlterField.**
An ``AlterField`` is removed when the next ``AlterField`` or ``RemoveField`` on the same field replaces it, and:

* Nothing between them refers to the field, and no ``RenameField`` falls between.
* The scan of opaque operations between them, for both the field’s and its model’s names, finds nothing.
  Models are scanned too, since a data migration may use the field through the model.
* The field is not many-to-many, and was never a primary key.
  The primary key flag can’t change between the definitions involved.
* The type change is safe.
  Take the definitions before the ``AlterField``, the one it makes, and the one that replaces it.
  Either the first two, or the last two, must be the same kind: same field class, same internal type, and for relations the same target model.
  The aim is to avoid turning two possible casts into a single impossible one, for example, ``IntegerField`` to ``JSONField`` to ``TextField``, which some databases reject even on empty tables.
  When replaced by a ``RemoveField``, this check isn’t needed.
* The definitions before and after the ``AlterField`` hold the same things.
  Without the ``AlterField``, the earlier definition stays in force until the replacing operation, so anything it occupies or points to that the newer one gave up could be reused or removed meanwhile, which the project state can’t show.
  So both must agree on the column (``db_column``, and whether it is a foreign key column), ``to_fields``, ``from_fields``, ``through``, ``db_table``, ``db_tablespace``, primary key status, and everything the definition refers to: the target model and field, and fields named in expressions.

**AlterModelOptions.**
Removed if a later ``AlterModelOptions`` on the same model replaces it, if the earlier one doesn’t change ``managed``, and if the scan is clean.
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
If any of these fail, the command stops with an “Internal error” message, and nothing is written.
That is a bug in django-clean-migrations, and the message asks you to report it.
In the meantime you can work around it by excluding the thing involved with ``--exclude``.

When applying, there is a second round after the files are written.
The command builds a fresh ``MigrationLoader`` that re-reads the files from disk, and checks that the graph has not changed, that every migration has the operations it expected, and that the final project state is the same as before.
If anything is off, it puts the original text back in every file it wrote, and fails with an error.

Folding
-------

Most of a long history is not “create, then delete”, but “create, then alter many times”.
Folding moves a later operation into the operation that created the thing it changes, and deletes it.
It is like Django’s squash optimizer, across the whole graph, but without changing migration files, names or dependencies.

Below, *P* is the operation that creates (and keeps carrying) the definition, and *E* the later operation folded into it.
The window is everything that may run between them, with the same rules as for the name scan.

What can be folded:

1. An ``AlterField`` into the ``AddField``, or ``CreateModel`` field entry, that added the field.
   If the ``AddField`` had a default and the new definition has none, the default is kept as a one-off (``preserve_default=False``), since the table may already hold rows when the column is added.
2. An ``AddField`` into the model’s ``CreateModel``, appended to its fields.
3. An ``AddIndex`` or ``AddConstraint`` (or their PostgreSQL variants) into the ``CreateModel``’s ``indexes`` or ``constraints``.
4. The model’s first ``AlterUniqueTogether`` or ``AlterIndexTogether`` into the ``CreateModel`` options.
5. The model’s first ``AlterModelOptions`` (unless it changes ``managed``), ``AlterModelManagers`` or ``AlterModelTableComment`` into the ``CreateModel``.

Conditions for every fold:

* Both operations are in editable migrations, the things involved aren’t poisoned or being removed, and P doesn’t lose anything else in the same step.
* Everything the folded definition needs exists when P runs: a referenced model or field must be created in a migration P depends on, or earlier in P’s migration, or be in the ``CreateModel`` itself.
  No dependency is ever added.
* Nothing is occupied earlier than before in a way that could collide: a field folded into a ``CreateModel`` must not share a name or column with any other field of the model that is kept, an index or constraint name must not be used by anything else, and an ``AlterField`` may not change the column, ``to_field``, ``through``, table or tablespace.
* Nothing is released later than before: whatever the old definition referred to and the new one doesn’t must not be needed meanwhile.
  The exception is a foreign key that just points at a different model, when the new model already exists at P; the old one then becomes unreferenced, and may in turn become removable.
* Nothing that stays between P and E refers to the field being changed.
* Every field and model the folded definition names has the same name at P as at E: no ``RenameField`` or ``RenameModel`` of them in between.
* Fields, indexes and constraints keep their order: they are folded in the order they were added, stopping at the first that can’t be.
  A model with ``order_with_respect_to`` is left alone.
* The opaque operations in the window pass the name scan (for the field and its model, the model, the index or constraint name and, for anything enforced such as a constraint, the model), or, with ``--trial``, the trial runs.
* P can be rewritten: Django’s migration writer must be able to serialize it, every import it needs must be a normal module path that doesn’t clash with a name the file already binds to something else, and the written source must load back to an operation with the same effect.
  For example, an ``AlterModelManagers`` setting just the default manager can’t be folded, since a ``CreateModel`` can’t state that.
  ``AlterModelTable`` is never folded: tables, sequences and indexes created under the new name from the start would get different names than in the original history.

Folds enable each other, and can make more things removable (a foreign key retargeted at P leaves its old model unreferenced).
So the command analyses the edited history again, removing, superseding and folding, until nothing changes.

The edited history is replayed in memory and on disk as for any other change.
Model state replays use copies of the field objects, since Django’s ``RenameField`` rewrites ``to_field`` and similar attributes of fields in the state in place.

Trial mode
----------

With ``--trial``, the name scan and the soft-reference rule stop being blockers.
Their hits are recorded instead: each removal (a whole life, or a superseded operation) keeps the list of opaque operations that mentioned it.
Every other rule stays: references, poisoning, editability, the primary key rule, exclusions and the superseded guards.

The original history is migrated on a scratch database first.
If that fails, the command stops: the history doesn’t migrate from scratch, so nothing can be learnt from trials.
The resulting snapshot covers, on SQLite, every table’s columns, foreign keys, indexes and check constraints (through ``PRAGMA`` statements, since the stored ``CREATE TABLE`` text depends on how a table was built), plus index, trigger and view definitions.
On PostgreSQL it covers columns (type, length, precision, nullability, default, identity, collation, generation), constraints (``pg_get_constraintdef``), indexes (``pg_get_indexdef``), sequences and their owners, views, materialized views, functions and procedures, triggers, enum types, extensions and comments.
On MySQL and MariaDB a best-effort set from ``information_schema`` is used; this is untested.
Other backends fall back to Django’s introspection, which is weaker.
Data is compared as a multiset of rows per table, with date and time values masked, and ``django_migrations`` compared by app and name only.

A candidate run substitutes the edited operation lists on a private ``MigrationLoader`` and runs Django’s ``MigrationExecutor``, so the files on disk are untouched.
Opaque operations that don’t change the project state are wrapped: an exception inside one rolls back to a savepoint and is recorded, and the run continues as if it did nothing.
Any other failure ends the run, and is blamed on the removals whose lifetime includes that migration.
A run with failures isn’t compared to the baseline, since the skipped operations explain any difference.

Each round blocks more removals, then solves again (blocking can cascade through references), until a run is clean and identical to the baseline.
That final run is what the plan reports.

Scratch databases are created with the backend’s own machinery and named ``cleanmig_<random>_<name>`` (a temporary file on SQLite); the alias is pointed at them for the run, as the test runner does, and they are always dropped.

Limitations
-----------

* **Squashed migrations that are still on disk are skipped.**
  Django uses the squashed migration, so that is the one the command processes.
  The migrations it replaces are neither scanned nor edited.
  Delete them first, once the squash is fully applied everywhere, if you want them cleaned.
* **No folding of operations.**
  The command removes operations, but doesn’t merge them.
  An ``AddField`` followed by an ``AlterField`` stays as two operations.
  It will not rewrite them to a single ``AddField`` with the final definition.
* **Conservative about a few things.**
  A field that was ever a primary key is never removed on its own, and ``AlterField`` operations on it, or on many-to-many fields, are never treated as superseded.
  ``order_with_respect_to`` blocks removing the field it names, unless the model goes too.
  A custom operation or ``SeparateDatabaseAndState`` that changes state keeps everything it touches.
* **The name scan is a heuristic**, as described above.
  It can miss a use that is hidden in helpers, dynamic names, or raw SQL built at run time.
  Review ``RunPython`` and ``RunSQL`` operations that survive near removed things, and test the cleaned history on a fresh database.
* **Equivalence is checked at project-state level.**
  Verification compares Django’s model of your schema, not the schema itself.
  Two histories with the same project state normally build the same database, but it’s possible for them to differ in ways that Django’s state can’t express.
  For example, a ``RunSQL`` that you rely on for its side effect, or database-specific details.
  Diff the schema before and after on a fresh database, as shown in :ref:`verifying-your-schema`.
* **Existing databases must already be migrated.**
  The cleaned history is for fresh databases.
  A database that is only part way through the original history may not end up with the same schema.
