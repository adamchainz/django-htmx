django-compact-migrations documentation
=======================================

*Make* ``migrate`` *faster by removing operations from your Django migration history that cancel out or can be merged.*

----

**Improve your Django and Git skills** with `my books <https://adamj.eu/books/>`__.

----

django-compact-migrations provides a management command that rewrites your migration history, so ``migrate`` runs faster on fresh databases, such as your test database.
It removes operations that cancel out, merges operations into earlier ones, and can delete migrations left empty.

.. toctree::
   :maxdepth: 1
   :caption: Contents:

   installation
   usage
   how_it_works
   benchmarks
   changelog


Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
