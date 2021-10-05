===========
django-htmx
===========

.. image:: https://img.shields.io/github/workflow/status/adamchainz/django-htmx/CI/main?style=for-the-badge
   :target: https://github.com/adamchainz/django-htmx/actions?workflow=CI

.. image:: https://img.shields.io/codecov/c/github/adamchainz/django-htmx/main?style=for-the-badge
  :target: https://app.codecov.io/gh/adamchainz/django-htmx

.. image:: https://img.shields.io/pypi/v/django-htmx.svg?style=for-the-badge
   :target: https://pypi.org/project/django-htmx/

.. image:: https://img.shields.io/badge/code%20style-black-000000.svg?style=for-the-badge
   :target: https://github.com/psf/black

.. image:: https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white&style=for-the-badge
   :target: https://github.com/pre-commit/pre-commit
   :alt: pre-commit

Extensions for using Django with `htmx <https://htmx.org/>`__.

Requirements
------------

Python 3.6 to 3.10 supported.

Django 2.2 to 4.0 supported.

----

**Are your tests slow?**
Check out my book `Speed Up Your Django Tests <https://gumroad.com/l/suydt>`__ which covers loads of best practices so you can write faster, more accurate tests.

----

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

4. (Optional) Add the extension script to your template, as documented below.


Example app
-----------

See the `example app <https://github.com/adamchainz/django-htmx/tree/main/example>`__ in the ``example/`` directory of the GitHub repository for usage of django-htmx.

Reference
---------

Extension Script
^^^^^^^^^^^^^^^^

django-htmx comes with a small JavaScript extension for htmx’s behaviour.
Currently the extension only includes a debug error handler, documented below.

The script is served as a static file called `django-htmx.js`, but you shouldn’t reference it directly.
Instead, use the included template tags, for both Django and Jinja templates.
Include the relevant template tag after your htmx script tag.

For **Django Templates**, load and use the template tag:

.. code-block:: django

    {% load django_htmx %}
    {% django_htmx_script %}

For **Jinja Templates**, you need to perform two steps.
First, load the tag function into the globals of your `custom environment <https://docs.djangoproject.com/en/stable/topics/templates/#django.template.backends.jinja2.Jinja2>`__:

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

Second, call the function in your template:

.. code-block:: jinja

    {{ django_htmx_script() }}

Debug Error Handler
~~~~~~~~~~~~~~~~~~~

htmx’s default behaviour when encountering an HTTP error is to discard the response.
This can make it hard to debug errors in development.

The django-htmx script includes an error handler that’s active when debug mode is on.
The handler detects responses with 404 and 500 status codes and replaces the page with their content.
This change allows you to debug with Django’s default error responses as you would for a non-htmx request.

See this in action in the “Error Demo” section of the example app.

``django_htmx.middleware.HtmxMiddleware``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

This middleware attaches ``request.htmx``, an instance of ``HtmxDetails``.

See it action in the “Middleware Tester” section of the example app.

``django_htmx.middleware.HtmxDetails``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

This class provides shortcuts for reading the htmx-specific `request headers <https://htmx.org/reference/#request_headers>`__.

``__bool__(): bool``
~~~~~~~~~~~~~~~~~~~~

``True`` if the request was made with htmx, otherwise ``False``.
This is based on the presence of the ``HX-Request`` header.

This allows you to switch behaviour for requests made with htmx like so:

.. code-block:: python

    def my_view(request):
        if request.htmx:
            template_name = "partial.html"
        else:
            template_name = "complete.html"
        return render(template_name, ...)

``boosted: bool``
~~~~~~~~~~~~~~~~~

``True`` if the request request came from an element with the ``hx-boost`` attribute.
Based on the ``HX-Boosted`` header.

``current_url: str | None``
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The current URL of the browser, or ``None`` for non-htmx requests.
Based on the ``HX-Current-URL`` header.

``history_restore_request: bool``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``True`` if the request is for history restoration after a miss in the local history cache.
Based on the ``HX-History-Restore-Request`` header.

``prompt: str | None``
~~~~~~~~~~~~~~~~~~~~~~

The user response to `hx-prompt <https://htmx.org/attributes/hx-prompt/>`__ if it was used, or ``None``.

``target: str | None``
~~~~~~~~~~~~~~~~~~~~~~

The ``id`` of the target element if it exists, or ``None``.
Based on the ``HX-Target`` header.

``trigger: str | None``
~~~~~~~~~~~~~~~~~~~~~~~

The ``id`` of the triggered element if it exists, or ``None``.
Based on the ``HX-Trigger`` header.

``trigger_name: str | None``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``name`` of the triggered element if it exists, or ``None``.
Based on the ``HX-Trigger-Name`` header.

``triggering_event: Any | None``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The deserialized JSON representtation of the event that triggered the request if it exists, or ``None``.
This header is set by the `event-header htmx extension <https://htmx.org/extensions/event-header/>`__, and contains details of the DOM event that triggered the request.

``django_htmx.http.HttpResponseStopPolling: type[HttpResponse]``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

When using a `polling trigger <https://htmx.org/docs/#polling>`__, htmx will stop polling when it encounters a response with the special HTTP status code 286.
``HttpResponseStopPolling`` is a `custom response class <https://docs.djangoproject.com/en/stable/ref/request-response/#custom-response-classes>`__ with that status code.

For example:

.. code-block:: python

    from django_htmx.http import HttpResponseStopPolling


    def my_pollable_view(request):
        if event_finished():
            return HttpResponseStopPolling()
        ...

``django_htmx.http.HTMX_STOP_POLLING: int``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

A constant for the HTTP status code 286, for use with e.g. `Django’s render shortcut <https://docs.djangoproject.com/en/stable/topics/http/shortcuts/#django.shortcuts.render>`__.

.. code-block:: python

    from django_htmx.http import HTMX_STOP_POLLING


    def my_pollable_view(request):
        if event_finished():
            return render("event-finished.html", status=HTMX_STOP_POLLING)
        ...
