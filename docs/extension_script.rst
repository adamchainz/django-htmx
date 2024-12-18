Extension Script
================

django-htmx comes with a small JavaScript extension for htmx’s behaviour.
Currently the extension only includes a debug error handler, documented below.

Installation
------------

The script is served as a static file called ``django-htmx.js``, but you shouldn't reference it directly.
Instead, use the included template tags, for both Django and Jinja templates.

Django Templates
^^^^^^^^^^^^^^^^

Load and use the template tag in your base template, after your htmx ``<script>`` tag:

.. code-block:: django

    {% load django_htmx %}
    <!doctype html>
    <html>
      ...
      <script src="{% static 'js/htmx.min.js' %}" defer></script>{# or however you include htmx #}
      {% django_htmx_script %}
      </body>
    </html>


Jinja Templates
^^^^^^^^^^^^^^^

First, load the tag function into the globals of your `custom environment
<https://docs.djangoproject.com/en/stable/topics/templates/#django.template.backends.jinja2.Jinja2>`__:

.. code-block:: python

    # myproject/jinja2.py
    from jinja2 import Environment
    from django_htmx.jinja import django_htmx_script


    def environment(**options):
        env = Environment(**options)
        env.globals.update(
            {
                # ...
                "django_htmx_script": django_htmx_script,
            }
        )
        return env

Second, call the function in your base template, after your htmx ``<script>`` tag:

.. code-block:: jinja

    {{ django_htmx_script() }}

Debug Error Handler
-------------------

htmx’s default behaviour when encountering an HTTP error is to discard the response content.
This can make it hard to debug errors in development.

The django-htmx script includes an error handler that’s active when Django’s debug mode is on (``settings.DEBUG`` is ``True``).
The handler detects responses with 404 and 500 status codes and replaces the page with their content.
This change allows you to debug with Django’s default error responses as you would for a non-htmx request.

See this in action in the “Error Demo” section of the :doc:`example project <example_project>`.

.. hint::

   This extension script should not be confused with htmx’s `debug extension <https://github.com/bigskysoftware/htmx-extensions/blob/main/src/debug/README.md>`__, which logs DOM events in the browser console.
