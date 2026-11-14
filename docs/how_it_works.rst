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
They are neither analysed nor edited.

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
A model’s ``ordering`` and ``get_latest_by`` options can name a field.
That doesn’t block removing the field by itself.
But if an opaque operation might query the model while the field exists, then it’s an issue, since a stale default ordering could crash the query.
So in that case the field is kept.

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
Any model or field it created, deleted or altered becomes *poisoned*: never removable.
This covers a ``SeparateDatabaseAndState`` that adds a field, or a custom operation that adds an index.

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

* The source of the migration file, minus the operations the command understands, and minus the ``dependencies``, ``replaces``, ``run_before``, ``initial`` and ``atomic`` attributes.
  So module-level helper functions, and the arguments of the opaque operations themselves, are included, but migration names such as ``0002_item_owner`` are not.
* The source of ``RunPython`` ``code`` and ``reverse_code`` callables that are defined outside the migration file, with ``functools.partial`` unwrapped.
* The ``sql`` and ``reverse_sql`` of ``RunSQL``.
* For custom operations, the source of their class.
* Operations nested in ``SeparateDatabaseAndState`` or ``RunSQL(state_operations=...)``.

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

With ``--write``, there is a second round after the files are written.
The command builds a fresh ``MigrationLoader`` that re-reads the files from disk, and checks that the graph has not changed, that every migration has the operations it expected, and that the final project state is the same as before.
If anything is off, it puts the original text back in every file it wrote, and fails with an error.

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
