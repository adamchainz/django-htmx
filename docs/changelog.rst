=========
Changelog
=========

1.0.0 (unreleased)
------------------

* Initial release.

* ``--prune`` deletes migrations left with no operations, rewiring the migrations that depend on them so every remaining migration keeps exactly the same ancestors.
