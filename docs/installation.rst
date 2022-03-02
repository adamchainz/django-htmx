Installation
============

Requirements
------------

Python 3.7 to 3.10 supported.

Django 2.2 to 3.2 supported.

Installation
------------

1. Install with **pip**:

   .. code-block:: sh

       python -m pip install django-htmx

2. Add django-htmx to your ``INSTALLED_APPS``:

   .. code-block:: python

       INSTALLED_APPS = [
           ...,
           "django_htmx",
           ...,
       ]

3. Add the middleware:

   .. code-block:: python

       MIDDLEWARE = [
           ...,
           "django_htmx.middleware.HtmxMiddleware",
           ...,
       ]

   The middleware adds ``request.htmx``, as described in :doc:`middleware`.

4. (Optional) Add the extension script to your base template, as documented in :doc:`extension_script`.

It’s up to you to add htmx (and extensions) to your project, via a ``<script>`` tag in your base template.
For resilience, download it into your project’s static files, rather than rely on the ``unpkg.com`` hosted version.
