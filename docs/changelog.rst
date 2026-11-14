=========
Changelog
=========

1.0.0 (unreleased)
------------------

* Initial release.

* Plan files are TOML, with each file edit as a unified diff, applied strictly and checked by hash.

* ``--prune`` deletes migrations left with no operations, rewiring the migrations that depend on them so every remaining migration keeps exactly the same ancestors.

* An operation referring to a model or field that doesn't exist at that point (a broken history) stops the analysis with a message naming it.

* A trial blames the default plan's own removals too when nothing else explains a difference, until a plan passes its run; when none does, ``plan --trial`` fails and ``apply --trial`` writes nothing.
