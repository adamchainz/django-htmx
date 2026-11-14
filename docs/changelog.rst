=========
Changelog
=========

1.0.0 (unreleased)
------------------

* Initial release.

* Plan files are TOML, with each file edit as a unified diff, applied strictly and checked by hash.

* ``--prune`` deletes migrations left with no operations, rewiring the migrations that depend on them so every remaining migration keeps exactly the same ancestors.
