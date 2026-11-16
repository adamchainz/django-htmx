How it works
============

This page explains how django-compact-migrations decides what is safe to remove.
You don’t need to read it to use the command, but it helps to understand why something was kept, or what the tool can’t know.

The goal is simple: a fresh database migrated with the compacted history must end up with exactly the same schema as one migrated with the original.
Everything below serves that goal, and when the command is unsure, it keeps the operation.

Reading the history
-------------------

The command loads your migrations with Django’s ``MigrationLoader``, without touching any database.
It then replays every migration in the same order that ``migrate`` would run them, tracking the state of every model as it goes.

Squashed migrations are applied as Django does: the squashed migration is used, and the migrations it replaces are skipped.
If replaced migrations are still on disk, the command prints a warning saying how many it skipped.
They are neither analysed nor edited: only the squashed migration is compacted.
That is fine as long as every database has the squashed migration recorded as applied, which is the usual precondition for deleting the replaced files anyway; delete them before or after compacting.

Which migrations are edited
~~~~~~~~~~~~~~~~~~~~~~~~~~~

A migration is a *project migration*, and may be edited, when its file is under the project root (the top of the Git work tree holding the current directory, else the current directory), not inside a virtual environment (a directory with ``pyvenv.cfg``), and not part of an installed package (a ``site-packages``, ``dist-packages``, ``.dist-info`` or ``.egg`` path component).
A migration outside the project is also editable when its app is named as a target; one in an installed package never is.
It’s the file that counts, not the app, so if you point ``MIGRATION_MODULES`` at a package inside your project, those migrations are editable.
Other migrations are replayed, because their operations affect which references exist, but never edited.

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

Django also skips the schema changes of some models: unmanaged ones (``managed=False``), proxy models, and models swapped out by a setting (``swappable``).
A table or column it left behind, or never created, must stay that way.
So a model whose ``managed`` option changes during its life, a proxy model, and a swapped-out model are poisoned, with everything they hold: nothing of them is removed, superseded or folded.
A model that is unmanaged throughout its life can still be removed, since neither its creation nor its deletion touches the database.

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
  A constraint or ``unique_together`` entry is enforced, so a data migration writing the table may rely on it without naming it (``bulk_create(..., ignore_conflicts=True)``, a caught ``IntegrityError``): its model’s names and the names of its fields are looked for too.

An operation that calls ``create_permissions``, ``create_contenttypes``, ``update_contenttypes``, ``emit_post_migrate_signal``, ``get_for_model`` or ``get_for_models`` acts on every model that exists when it runs, without naming them: content type and permission rows, whose ids follow the models that existed.
It counts as mentioning every name, and an elidable one is never removed alongside something it mentions.

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
* If the ``AlterField`` rewrites existing rows (it makes the field ``NOT NULL``, so Django fills ``NULL`` with its default, or it changes the field class), no data migration mentioning the model may run between the model’s creation and the ``AlterField`` (or, with ``--trial``, the trial runs pass): rows written by one would keep their old values.
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
* Every definition in the chain agrees on ``unique`` and on whether, and how, the field has a check constraint, and no foreign key points at the field: otherwise the database objects would get different names (see `Folding`_).

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
That is a bug in django-compact-migrations, and the message asks you to report it.
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
6. A ``RenameField`` into the ``AddField`` or ``CreateModel`` entry that created the field, which then gets the new name.
   Django’s ``RenameField`` renames the column only: indexes (including PostgreSQL’s ``_like`` ones), unique and check constraints, foreign key constraints and many-to-many tables keep names made from the old column or field name, while a field created under the new name gets names made from the new one.
   So only a field without any of these is folded: not unique, not indexed, not a relation, no check constraint (``PositiveIntegerField`` and the like), not generated, never a primary key; or one whose column is fixed by ``db_column``, which the rename doesn’t change.
   Nothing may refer to the field between the two (indexes, constraints, ``unique_together``, other fields’ ``to_field``), the ``CreateModel``’s options may not name it, no other field may ever have had the new name, and the scan for both names must be clean.
   And since a rename moves the field to the end of the model’s fields in the project state, which is the column order SQLite rebuilds tables in, the field must already be the last one: no other field of the model is created or renamed in between.
7. An ``AlterModelTable`` of an unmanaged model (``managed=False`` in its ``CreateModel``, with nothing else happening to the model in between) into the ``CreateModel``’s ``db_table``: there is no table to rename.

``RenameModel`` is never folded, nor is ``AlterModelTable`` of a managed model: Django renames the table only, so its primary key constraint, sequence or identity, indexes, foreign keys and many-to-many tables keep names made from the old table name, which a table created under the new name wouldn’t have.
``AlterOrderWithRespectTo`` isn’t folded either: it adds a column, last.

Conditions for every fold:

* Both operations are in editable migrations, the things involved aren’t poisoned or being removed, and P doesn’t lose anything else in the same step.
* Everything the folded definition needs exists when P runs: a referenced model or field must be created in a migration P depends on, or earlier in P’s migration, or be in the ``CreateModel`` itself.
  No dependency is ever added.
* Nothing is occupied earlier than before in a way that could collide: a field folded into a ``CreateModel`` must not share a name or column with any other field of the model that is kept, an index or constraint name must not be used by anything else, and an ``AlterField`` may not change the column, ``to_field``, ``through``, table or tablespace.
* Nothing is released later than before: whatever the old definition referred to and the new one doesn’t must not be needed meanwhile.
  The exception is a foreign key that just points at a different model, when the new model already exists at P; the old one then becomes unreferenced, and may in turn become removable.
* Nothing that stays between P and E refers to the field being changed.
* Every field and model the folded definition names has the same name at P as at E: no ``RenameField``, ``RenameModel`` or ``AlterModelTable`` of them in between.
* The final project state lists every model’s fields in the same order (SQLite rebuilds tables in that order), which the verification checks.
* Every database object gets the name it got in the original history.
  Django’s schema editor names some objects differently when it creates them with the table than when it adds them later, so these folds are not made:

  - a field that gains ``unique=True`` from an ``AlterField`` gets a hashed ``..._uniq`` constraint, while a unique column created with the table (``CREATE TABLE`` or ``AddField``) gets the database’s own name (``..._key`` on PostgreSQL);
  - likewise, a check constraint added by an ``AlterField`` (``PositiveIntegerField``, ``JSONField`` on some backends, custom fields with ``db_check()``) is named ``..._check`` by Django, while an inline one is named by the database: so an ``AlterField`` is only folded or superseded when the definitions involved agree on ``unique`` and on the kind of field that decides the check;
  - when an ``AlterField`` changes a field’s type, Django drops and recreates the foreign keys pointing at it with a ``..._fk`` suffix, instead of the usual ``..._fk_<table>_<column>``: so no foreign key or many-to-many field is folded, or created by a fold, before an ``AlterField`` of the field it points at, a field that foreign keys point at is never superseded or folded, and an ``AddField`` is only folded into a ``CreateModel`` when no such ``AlterField`` falls between;
  - a field whose class doesn’t come from ``django.db.models`` (``HStoreField``, a custom field) may need something a ``RunSQL`` or custom operation sets up, such as an extension or a type: it isn’t folded across any opaque operation other than ``RunPython``, since its column would then be created first.
    The same goes for a field with a ``db_collation``, since such an operation may create the collation (NetBox creates ``natural_sort`` that way).

  Index, foreign key and ``unique_together`` names come from the same functions either way, from the table and column names; those must not change in the window, which the rule above ensures.
  These rules were derived from Django’s ``BaseDatabaseSchemaEditor`` (``_iter_column_sql``, ``_alter_field``) and checked by comparing ``pg_dump --schema-only`` of databases migrated with the original and the folded histories of large projects.
* Fields, indexes and constraints keep their order: they are folded in the order they were added, stopping at the first that can’t be.
  A model with ``order_with_respect_to`` is left alone.
* The opaque operations in the window pass the name scan (for the field and its model, the model, the index or constraint name and, for anything enforced such as a constraint, the model), or, with ``--trial``, the trial runs.
* An ``AlterField`` folded into an earlier ``AddField`` gives rows that already exist when the column is added the folded definition’s default instead of the ``AddField``’s.
  Unless the type and defaults are the same, no data migration mentioning the model may run between the model’s creation and the ``AddField`` (or, with ``--trial``, the trial runs pass).
* P can be rewritten: Django’s migration writer must be able to serialize it, every import it needs must be a normal module path that doesn’t clash with a name the file already binds to something else, and the written source must load back to an operation with the same effect.
  For example, an ``AlterModelManagers`` setting just the default manager can’t be folded, since a ``CreateModel`` can’t state that.

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
On MySQL and MariaDB it covers what ``mysqldump --no-data`` shows, from ``information_schema``: tables (engine, collation, comment, options), columns in their order (type, nullability, default, extra, collation, comment, generation), indexes and their names (columns or expressions, order, prefix length, uniqueness, type, comment, visibility), constraints, foreign keys (columns, targets, ``ON UPDATE``/``ON DELETE``), check constraints by table, triggers, views and routines; the tests compare it on both servers.
Other backends fall back to Django’s introspection, which is weaker.
Data is compared as a multiset of rows per table, with date and time values masked, and ``django_migrations`` compared by app and name only.

A candidate run substitutes the edited operation lists on a private ``MigrationLoader`` and runs Django’s ``MigrationExecutor``, so the files on disk are untouched.
Opaque operations that don’t change the project state are wrapped: an exception inside one rolls back to a savepoint and is recorded, and the run continues as if it did nothing.
Any other failure ends the run.
Failures are attributed as described in the usage guide: by the field, model, table or app the error says is missing, else by the opaque operation’s mentions or the failing migration’s window, half at a time.
A run with failures isn’t compared to the baseline, since the skipped operations explain any difference.

Each round blocks more removals, then solves again (blocking can cascade through references), until a run is clean and identical to the baseline.
That final run is what the plan reports.

Scratch databases are created with the backend’s own machinery and named ``compactmig_<random>_<name>`` (a temporary file on SQLite); every alias is pointed at one for the run, as the test runner does, and they are always dropped.
Each run happens in a forked child process; the parent creates and drops the databases, and gets back the failures and the snapshot.

The baseline is migrated twice: values that differ between the two runs (per column, else per table, and any schema object) are masked in every later comparison, so they are never blamed on a removal.
A data difference is first blamed on removals that took away an opaque operation (elidable ones removed with what they mention, no-op data migrations), half at a time, then on the removals relating to the table.
Data is compared by column name, not position; on SQLite, the physical column order of each table is compared as a separate schema item, which blames every removal on that table.
With ``--noop-data``, the first baseline run also records, through ``execute_wrapper`` on every connection, the statements each candidate data migration runs and their row counts.
Those that wrote nothing are removed in the first round of every solve, the static one included, and the name scan ignores them, so what they mentioned can be removed or folded in the same plan.
Like folds, they can be blamed: one that is blamed is put back, and the static plan is solved again without it.

Candidates are solved starting from the history as the default plan leaves it, so they contain all of it; at the end, every operation the default plan removes must be removed, or the default plan, trialled too, is used.

Pruning
-------

With ``--prune``, after everything else, migrations left with no operations are deleted (the rules for what is kept are in the usage guide).
For a deleted migration P, every surviving migration that depended on it gets P’s dependencies instead, following chains of deleted migrations, except those it already runs after through its other dependencies.
The exception is a dependency within the same app: Django finds an app’s first and latest migrations, and conflicting branches, through same-app dependencies only, so a migration that depended on a deleted migration of its own app always gets that migration’s nearest surviving same-app dependencies (both branches of a deleted merge migration), even when it already runs after them through another app.
The roots and leaves of every app (as ``migrate`` sees them when it checks for conflicts) must be the original ones minus the deleted migrations: this is checked on the planned graph, in trial runs, and on the graph loaded from the rewritten files.
Keeping the ancestors isn’t enough to keep the order ``migrate`` runs migrations in: Django walks the graph depth first, following each migration’s dependencies sorted by name, so a dependency moved from a deleted migration to its dependant can make two unordered migrations (say, two that add columns to the same table) swap.
So Django’s plan for the pruned graph must be the original plan minus the deleted migrations, exactly; if it isn’t, migrations are pruned one at a time, in plan order, keeping each only if the order still holds.
This is checked on disk too.
So the set of migrations every surviving migration runs after (its ancestors in the graph) is exactly the original set minus the deleted migrations: no ordering constraint between the migrations that do something is lost or added.
This invariant is checked on the planned graph, and again on the graph loaded from the rewritten files, which must also contain exactly the surviving migrations and be consistent.

Keeping the first and latest migration of every app means ``__first__`` and ``__latest__`` dependencies, ``swappable_dependency()`` (which means ``__first__``), Django’s inference of ``initial`` and ``--fake-initial`` all resolve as before.
Only literal ``("app", "name")`` entries are rewritten, in the dependants’ ``dependencies`` lists, which are regenerated keeping their other entries’ source text.

MySQL and MariaDB
-----------------

The analysis reasons about Django’s project state, which is the same on every backend.
On MySQL and MariaDB, two parts of the schema also depend on how the database got there, so a removal or fold the state calls harmless can change the schema:

* **The index backing a foreign key.**
  InnoDB needs an index starting with a foreign key’s column.
  Creating the foreign key creates one implicitly, named like the constraint (``…_fk_<table>_<column>``), unless an index starting with that column exists already; the server drops it when another index starting with the column is created; and Django, before dropping such an index, creates its own index on the column, named ``<table>_<column>_<hash>``.
  So, when any configured database is MySQL or MariaDB, the command keeps:

  - an index, unique constraint or together-entry whose first column is a foreign key (unless that column is dropped later anyway): removing it removes those renames;
  - a fold of an ``AlterUniqueTogether`` or ``AlterIndexTogether`` that drops such an entry from the ``CreateModel`` (the same thing), and a fold that would create such an index earlier, while another one on the same foreign key is dropped in between;
  - folds of ``AlterField``, and superseded ``AlterField`` operations, on a field that is or becomes a foreign key, when they change whether it is one, its ``db_constraint``, ``unique`` or ``db_index``.

  Changing a foreign key’s target or ``on_delete``, and folding indexes into the ``CreateModel``, keep the same names: those still go.
* **Defaults of TEXT, BLOB and JSON columns, on MySQL only.**
  ``AddField`` of a ``TextField``, ``BinaryField`` or ``JSONField`` with a (non-empty) default adds the column with ``DEFAULT (…)`` and never drops it, as MySQL can’t, while ``CreateModel`` creates the column without one.
  So, when a configured database is MySQL (not MariaDB, which drops the default as other backends do), such an ``AddField`` isn’t folded into its ``CreateModel``, and ``AlterField`` operations on such fields are neither folded nor dropped as superseded.

A plan doesn’t know which database production runs, so the rules apply when any configured database uses the MySQL backend; telling MySQL from MariaDB needs the server, which is asked for its version (a server that can’t be reached counts as MySQL, whose rules are a superset).
Plans for projects without MySQL or MariaDB are unchanged.
The plan file records the servers the rules were applied for, under ``options.backends``.
A trial run on a MySQL or MariaDB database (``--trial --database …``) compares index names, defaults and column order, so it checks what these rules can’t foresee.

Limitations
-----------

* **Squashed migrations that are still on disk are skipped.**
  Django uses the squashed migration, so that is the one the command processes.
  The migrations it replaces are neither scanned nor edited.
  Delete them first, once the squash is fully applied everywhere, if you want them compacted.
* **Folds rewrite operations.**
  An operation that absorbs a fold is regenerated with Django’s migration writer, so it loses its formatting and comments.
  Use ``--skip-kind fold`` to keep every operation as written.
* **Conservative about a few things.**
  A field that was ever a primary key is never removed on its own, and ``AlterField`` operations on it, or on many-to-many fields, are never treated as superseded.
  ``order_with_respect_to`` blocks removing the field it names, unless the model goes too.
  A custom operation or ``SeparateDatabaseAndState`` that changes state keeps everything it touches.
* **The name scan is a heuristic**, as described above.
  It can miss a use that is hidden in helpers, dynamic names, or raw SQL built at run time.
  Review ``RunPython`` and ``RunSQL`` operations that survive near removed things, and test the compacted history on a fresh database.
* **Equivalence is checked at project-state level.**
  Verification compares Django’s model of your schema, not the schema itself.
  Two histories with the same project state normally build the same database, but it’s possible for them to differ in ways that Django’s state can’t express.
  For example, a ``RunSQL`` that you rely on for its side effect, or database-specific details.
  Diff the schema before and after on a fresh database, as shown in :ref:`verifying-your-schema`.
* **Existing databases must already be migrated.**
  The compacted history is for fresh databases.
  A database that is only part way through the original history may not end up with the same schema.
