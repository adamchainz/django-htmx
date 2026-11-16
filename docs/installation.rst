Installation
============

Requirements
------------

Python 3.11 to 3.15 supported.

Django 5.2 to 6.1 supported.

Installation
------------

1. Install with **pip**:

   .. code-block:: sh

       python -m pip install django-compact-migrations

2. Add django-compact-migrations to your ``INSTALLED_APPS``.
   Since it’s a development tool, you should only do so in your development settings:

   .. code-block:: python

       INSTALLED_APPS = [
           ...,
           "django_compact_migrations",
           ...,
       ]
