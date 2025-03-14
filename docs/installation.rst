Installation
============

Requirements
------------

Python 3.9 to 3.13 supported.

Django 4.2 to 5.2 supported.

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

3. (Optional) Add the middleware:

   .. code-block:: python

       MIDDLEWARE = [
           ...,
           "django_htmx.middleware.HtmxMiddleware",
           ...,
       ]

   The middleware adds ``request.htmx``, as described in :doc:`middleware`.

4. (Optional) Add htmx and the django-htmx extension script to your pages with a :doc:`template tag <template_tags>`, available for Django templates and Jinja2.
   In the typical case, with Django templates:

   .. code-block:: django
      :emphasize-lines: 6

       {% load django_htmx %}
       <!doctype html>
       <html>
         <head>
           ...
           {% htmx_script %}
         </head>
         <body>
           ...
         </body>
       </html>
