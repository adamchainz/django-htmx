Installation
============

Requirements
------------

Python 3.11 to 3.15 supported.

Django 5.2 to 6.1 supported.

Trials run on SQLite, PostgreSQL, MySQL or MariaDB.

Installation
------------

1. Install with **pip**:

   .. code-block:: sh

       python -m pip install django-compact-migrations

2. Add django-compact-migrations to your ``INSTALLED_APPS``.
   It’s a development tool, so add it in your development settings only:

   .. code-block:: python

       INSTALLED_APPS = [
           ...,
           "django_compact_migrations",
           ...,
       ]

Trials create scratch databases, so the database user needs permission to create databases, as for running tests.
Use development settings, never production ones: see :ref:`before-you-start`.
