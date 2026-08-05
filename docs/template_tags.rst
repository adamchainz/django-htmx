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

   * htmx 2, the default — currently version 2.0.10.
   * htmx 4, currently in beta — version 4.0.0-beta6.

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

    {% htmx_script version=4 extensions="hx-sse,hx-ws" %}

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

    {{ htmx_script(version=4, extensions=["hx-sse", "hx-ws"]) }}

To use a CSP nonce, pass it to the function as ``nonce``:

.. code-block:: jinja

    {{ htmx_script(nonce=csp_nonce) }}

.. _vendored-htmx-extensions:

Vendored htmx extensions
^^^^^^^^^^^^^^^^^^^^^^^^

django-htmx vendors some stable htmx extensions.
Extensions are named per htmx 4, where they’re bundled with htmx itself.
Some extensions are only available for htmx 4, as shown in the below table.
The ``extensions`` argument renders a script tag for each named extension, using the file appropriate for the selected htmx version.

.. list-table::
   :header-rows: 1

   * - Name
     - Description
     - htmx 2
     - htmx 4

   * - ``htmx-2-compat``
     - Compatibility behaviours from htmx 2, such as old event names, easing migration.
     - —
     - `docs <https://four.htmx.org/extensions/htmx-2-compat>`__

   * - ``hx-browser-indicator``
     - Show the browser’s native loading indicator during requests.
     - —
     - `docs <https://four.htmx.org/extensions/hx-browser-indicator>`__

   * - ``hx-download``
     - Trigger file downloads, with progress events, instead of swaps.
     - —
     - `docs <https://four.htmx.org/extensions/hx-download>`__

   * - ``hx-head``
     - Merging of ``<head>`` tag content between pages.
     - `head-support 2.0.5 <https://htmx.org/extensions/head-support/>`__
     - `docs <https://four.htmx.org/extensions/hx-head>`__

   * - ``hx-optimistic``
     - Optimistic UI updates before the response arrives.
     - —
     - `docs <https://four.htmx.org/extensions/hx-optimistic>`__

   * - ``hx-preload``
     - Preload responses for links and forms before they’re requested.
     - `preload 2.1.2 <https://htmx.org/extensions/preload/>`__
     - `docs <https://four.htmx.org/extensions/hx-preload>`__

   * - ``hx-prompt``
     - Restores htmx 2’s ``hx-prompt`` attribute and ``HX-Prompt`` header, read by :attr:`HtmxDetails.prompt <django_htmx.middleware.HtmxDetails.prompt>`.
     - Included in htmx 2 itself.
     - `docs <https://four.htmx.org/extensions/hx-prompt>`__

   * - ``hx-ptag``
     - Polling tags, letting servers skip swaps when content hasn’t changed.
       Supported by the :func:`django_htmx.http.ptag` decorator.
     - —
     - `docs <https://four.htmx.org/extensions/hx-ptag>`__

   * - ``hx-sse``
     - Server-sent events (SSE).
     - `sse 2.2.4 <https://htmx.org/extensions/sse/>`__
     - `docs <https://four.htmx.org/extensions/hx-sse>`__

   * - ``hx-targets``
     - Target multiple elements with the same swap content.
     - —
     - `docs <https://four.htmx.org/extensions/hx-targets>`__

   * - ``hx-upsert``
     - ``upsert`` swap style that updates existing elements by ID and inserts new ones.
     - —
     - `docs <https://four.htmx.org/extensions/hx-upsert>`__

   * - ``hx-ws``
     - WebSockets.
     - `ws 2.0.4 <https://htmx.org/extensions/ws/>`__
     - `docs <https://four.htmx.org/extensions/hx-ws>`__

The htmx 2 extension files come from their standalone packages, with the linked names and versions.
The htmx 4 extension files are bundled with htmx itself, so they always match the vendored htmx 4 version.

Refer to each extension’s documentation for usage, which can differ between htmx versions.
Notably, htmx 2 extensions need activating with the `hx-ext attribute <https://htmx.org/attributes/hx-ext/>`__ using their htmx 2 names, like ``hx-ext="sse"``, whilst htmx 4 extensions are active as soon as their script is loaded.

htmax
"""""

htmx 4 also ships `htmax <https://four.htmx.org/docs#htmax>`__, a bundle of htmx plus its most popular extensions in a single file.
Pass ``extensions="htmax"`` to render the bundle in place of the plain htmx script:

.. code-block:: django

    {% htmx_script version=4 extensions="htmax" %}

Since htmax already bundles its extensions, the ``htmax`` name cannot be combined with other extension names, and it’s only available with htmx version 4.

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
