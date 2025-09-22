Template tags
=============

django-htmx comes with two template tags for rendering ``<script>`` tags, the first of which includes a vendored version of htmx.
The tags are available for both of Django’s built-in template engines:

* For Django templates, use the ``django_htmx`` template library with ``{% load django_htmx %}``.

* For Jinja, import the functions from ``django_htmx.jinja`` and add them to the environment.

All ``<script>`` tags are rendered with |the defer attribute|__ to avoid blocking page rendering.

.. |the defer attribute| replace:: ``defer`` attribute
__ https://developer.mozilla.org/en-US/docs/Web/HTML/Element/script#defer

``htmx_script``
---------------

The ``htmx_script`` template tag renders two script tags for:

1. The vendored version of htmx included in django-htmx.
   The current vendored version of htmx is 2.0.7.
   (`htmx release notes <https://github.com/bigskysoftware/htmx/releases>`__.)

2. django-htmx’s extension script, when |settings.DEBUG|__ is ``True``.
   This script adds an error handler for debugging HTTP errors, :ref:`explained below <django-htmx-extension-script>`.

   .. |settings.DEBUG| replace:: ``settings.DEBUG``
   __ https://docs.djangoproject.com/en/stable/ref/settings/#debug

Django templates
^^^^^^^^^^^^^^^^

Load the library and use ``{% htmx_script %}`` in your ``<head>`` tag, typically in a base template:

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

The default is to use a minified version of htmx.
Pass ``minified=False`` to render the non-minified version:

.. code-block:: django

    {% htmx_script minified=False %}

This may be useful when debugging htmx behaviour.

On Django 6.0+, the ``<script>`` tags will include `the Content Security Policy (CSP) nonce <https://docs.djangoproject.com/en/6.0/howto/csp/#nonce-config>`__, if it’s present in the context.

Jinja
^^^^^

First, load the tag function into the globals of your `custom environment
<https://docs.djangoproject.com/en/stable/topics/templates/#django.template.backends.jinja2.Jinja2>`__:

.. code-block:: python
   :emphasize-lines: 10

   from jinja2 import Environment
   from django_htmx.jinja import htmx_script


   def environment(**options):
       env = Environment(**options)
       env.globals.update(
           {
               # ...
               "htmx_script": htmx_script,
           }
       )
       return env

Second, call the function in a variable in your ``<head>`` tag, typically in a base template:

.. code-block:: jinja
   :emphasize-lines: 6

    {% load django_htmx %}
    <!doctype html>
    <html>
      <head>
        ...
        {{ htmx_script() }}
      </head>
      <body>
        ...
      </body>
    </html>

The default is to use a minified version of htmx.
Pass ``minified=False`` to render the non-minified version:

.. code-block:: jinja

    {{ htmx_script(minified=False) }}

This may be useful when debugging htmx behaviour.

To use a CSP nonce, pass it to the function as ``nonce``:

.. code-block:: jinja

    {{ htmx_script(nonce=csp_nonce) }}

``django_htmx_script``
----------------------

The ``django_htmx_script`` template tag renders a script tag only for the django-htmx extension script (:ref:`explained below <django-htmx-extension-script>`), when ``settings.DEBUG`` is ``True``.
Use it when you’re sourcing htmx from outside django-htmx.

Django templates
^^^^^^^^^^^^^^^^

Load and use the template tag after your htmx ``<script>`` tag:

.. code-block:: django
   :emphasize-lines: 7

    {% load django_htmx %}
    <!doctype html>
    <html>
      <head>
        ...
        <script src="{% static 'custom/htmx.min.js' %}" defer></script>
        {% django_htmx_script %}
      </head>
      <body>
        ...
      </body>
    </html>

On Django 6.0+, the ``<script>`` tag will include `the Content Security Policy (CSP) nonce <https://docs.djangoproject.com/en/6.0/howto/csp/#nonce-config>`__, if it’s present in the context.

Jinja
^^^^^

First, load the tag function into the globals of your `custom environment
<https://docs.djangoproject.com/en/stable/topics/templates/#django.template.backends.jinja2.Jinja2>`__:

.. code-block:: python
   :emphasize-lines: 10

   from jinja2 import Environment
   from django_htmx.jinja import django_htmx_script, htmx_script


   def environment(**options):
       env = Environment(**options)
       env.globals.update(
           {
               # ...
               "django_htmx_script": django_htmx_script,
           }
       )
       return env

Second, call the function in a variable in your ``<head>`` tag, typically in a base template:

.. code-block:: jinja
   :emphasize-lines: 7

    {% load django_htmx %}
    <!doctype html>
    <html>
      <head>
        ...
        <script src="{{ static('custom/htmx.min.js') }}" defer></script>
        {{ django_htmx_script() }}
      </head>
      <body>
        ...
      </body>
    </html>

To use a CSP nonce, pass it to the function as ``nonce``:

.. code-block:: jinja

    {{ django_htmx_script(nonce=csp_nonce) }}

.. _django-htmx-extension-script:

django-htmx extension script
----------------------------

This script, rendered by either of the above template tags when ``settings.DEBUG`` is ``True``, extends htmx with an error handler.
htmx’s default behaviour when encountering an HTTP error is to discard the response content, which can make it hard to debug errors.

This script adds an error handler that detects responses with 400, 403, 404, and 500 status codes and replaces the page with their content.
This change exposes Django’s default error responses, allowing you to debug as you would for a non-htmx request.

See the script in action in the “Error Demo” section of the :doc:`example project <example_project>`.

See its source `on GitHub <https://github.com/adamchainz/django-htmx/blob/main/src/django_htmx/static/django_htmx/django-htmx.js>`__.
