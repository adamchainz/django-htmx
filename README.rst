=======================
django-clean-migrations
=======================

.. image:: https://img.shields.io/readthedocs/django-clean-migrations?style=for-the-badge
   :target: https://django-clean-migrations.readthedocs.io/en/latest/

.. image:: https://img.shields.io/github/actions/workflow/status/adamchainz/django-clean-migrations/main.yml.svg?branch=main&style=for-the-badge
   :target: https://github.com/adamchainz/django-clean-migrations/actions?workflow=CI

.. image:: https://img.shields.io/badge/Coverage-100%25-success?style=for-the-badge
   :target: https://github.com/adamchainz/django-clean-migrations/actions?workflow=CI

.. image:: https://img.shields.io/pypi/v/django-clean-migrations.svg?style=for-the-badge
   :target: https://pypi.org/project/django-clean-migrations/

.. image:: https://img.shields.io/badge/code%20style-black-000000.svg?style=for-the-badge
   :target: https://github.com/psf/black

.. image:: https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white&style=for-the-badge
   :target: https://github.com/pre-commit/pre-commit
   :alt: pre-commit

----

Clean up your Django migration history by removing operations that cancel out.

----

**Improve your Django and Git skills** with `my books <https://adamj.eu/books/>`__.

----

Over time, migration histories accumulate operations that undo each other: models created then deleted, fields added then removed, indexes and constraints added then dropped, and ``Alter*`` operations that later ones supersede.
django-clean-migrations scans your project’s migrations and removes such operations, to speed up running migrations from scratch.

Preview the changes with ``plan``, then apply them with ``apply``:

.. code-block:: console

    $ python manage.py clean_migrations plan
    $ python manage.py clean_migrations apply

Or save the plan to a file, review it, and apply exactly that:

.. code-block:: console

    $ python manage.py clean_migrations plan --output plan.json
    $ python manage.py clean_migrations apply --plan plan.json

Documentation
-------------

Please see https://django-clean-migrations.readthedocs.io/.
