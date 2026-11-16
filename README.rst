=========================
django-compact-migrations
=========================

.. image:: https://img.shields.io/readthedocs/django-compact-migrations?style=for-the-badge
   :target: https://django-compact-migrations.readthedocs.io/en/latest/

.. image:: https://img.shields.io/github/actions/workflow/status/adamchainz/django-compact-migrations/main.yml.svg?branch=main&style=for-the-badge
   :target: https://github.com/adamchainz/django-compact-migrations/actions?workflow=CI

.. image:: https://img.shields.io/badge/Coverage-100%25-success?style=for-the-badge
   :target: https://github.com/adamchainz/django-compact-migrations/actions?workflow=CI

.. image:: https://img.shields.io/pypi/v/django-compact-migrations.svg?style=for-the-badge
   :target: https://pypi.org/project/django-compact-migrations/

.. image:: https://img.shields.io/badge/code%20style-black-000000.svg?style=for-the-badge
   :target: https://github.com/psf/black

.. image:: https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white&style=for-the-badge
   :target: https://github.com/pre-commit/pre-commit
   :alt: pre-commit

----

Compact your Django migration history by removing operations that cancel out.

----

**Improve your Django and Git skills** with `my books <https://adamj.eu/books/>`__.

----

Over time, migration histories accumulate operations that undo each other: models created then deleted, fields added then removed, indexes and constraints added then dropped, and ``Alter*`` operations that later ones supersede.
django-compact-migrations scans your project’s migrations and removes such operations, to speed up running migrations from scratch.

Preview the changes with ``plan``, then apply them with ``apply``:

.. code-block:: console

    $ python manage.py compact_migrations plan
    $ python manage.py compact_migrations apply

Or save the plan to a file, review it, and apply exactly that:

.. code-block:: console

    $ python manage.py compact_migrations plan --output plan.toml
    $ python manage.py compact_migrations apply --plan plan.toml

Documentation
-------------

Please see https://django-compact-migrations.readthedocs.io/.
