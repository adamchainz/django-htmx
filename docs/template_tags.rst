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

The ``htmx_script`` template tag renders script tags for:

1. A vendored version of htmx included in django-htmx.
   Two versions of htmx are vendored (`htmx release notes <https://github.com/bigskysoftware/htmx/releases>`__):

   * htmx 2, the default — currently version 2.0.7.
   * htmx 4, currently in beta — version 4.0.0-beta5.

   (There is no htmx 3—the project skipped from 2 to 4.)

2. Vendored htmx extensions, if requested with the ``extensions`` argument, :ref:`covered below <vendored-htmx-extensions>`.

3. django-htmx’s extension script, when |settings.DEBUG|__ is ``True``.
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

The default is to use htmx version 2.
Pass ``version=4`` to render htmx version 4, currently in beta:

.. code-block:: django

    {% htmx_script version=4 %}

Pass ``extensions`` with a comma-separated string of names to also render script tags for :ref:`vendored htmx extensions <vendored-htmx-extensions>`, matching the selected htmx version:

.. code-block:: django

    {% htmx_script version=4 extensions="sse,ws" %}

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

The default is to use htmx version 2.
Pass ``version=4`` to render htmx version 4, currently in beta:

.. code-block:: jinja

    {{ htmx_script(version=4) }}

Pass ``extensions`` with a comma-separated string or sequence of names to also render script tags for :ref:`vendored htmx extensions <vendored-htmx-extensions>`, matching the selected htmx version:

.. code-block:: jinja

    {{ htmx_script(version=4, extensions=["sse", "ws"]) }}

To use a CSP nonce, pass it to the function as ``nonce``:

.. code-block:: jinja

    {{ htmx_script(nonce=csp_nonce) }}

.. _vendored-htmx-extensions:

Vendored htmx extensions
^^^^^^^^^^^^^^^^^^^^^^^^

django-htmx vendors some htmx extensions, selected for being stable and available for both htmx versions 2 and 4.
The ``extensions`` argument renders a script tag for each named extension, using the file appropriate for the selected htmx version.

.. list-table::
   :header-rows: 1

   * - Name
     - Description
     - htmx 2
     - htmx 4

   * - ``head-support``
     - Merging of ``<head>`` tag content between pages.
     - `2.0.5 <https://htmx.org/extensions/head-support/>`__
     - `hx-head <https://four.htmx.org/extensions/hx-head>`__

   * - ``preload``
     - Preload responses for links and forms before they’re requested.
     - `2.1.2 <https://htmx.org/extensions/preload/>`__
     - `hx-preload <https://four.htmx.org/extensions/hx-preload>`__

   * - ``sse``
     - Server-sent events (SSE).
     - `2.2.4 <https://htmx.org/extensions/sse/>`__
     - `hx-sse <https://four.htmx.org/extensions/hx-sse>`__

   * - ``ws``
     - WebSockets.
     - `2.0.4 <https://htmx.org/extensions/ws/>`__
     - `hx-ws <https://four.htmx.org/extensions/hx-ws>`__

The htmx 2 extension files come from their standalone packages, with the linked versions.
The htmx 4 extension files are bundled with htmx itself, so they always match the vendored htmx 4 version.

Refer to each extension’s documentation for usage, which can differ between htmx versions.
Notably, htmx 2 extensions need activating with the `hx-ext attribute <https://htmx.org/attributes/hx-ext/>`__, whilst htmx 4 extensions are active as soon as their script is loaded.

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
