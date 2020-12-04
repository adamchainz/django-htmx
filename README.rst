===========
django-htmx
===========

.. image:: https://img.shields.io/github/workflow/status/adamchainz/django-htmx/CI/master?style=for-the-badge
   :target: https://github.com/adamchainz/django-htmx/actions?workflow=CI

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

Python 3.6 to 3.9 supported.

Django 2.2 to 3.1 supported.

----

**Are your tests slow?**
Check out my book `Speed Up Your Django Tests <https://gumroad.com/l/suydt>`__ which covers loads of best practices so you can write faster, more accurate tests.

----

Installation
------------

1. Install with **pip**:

   .. code-block:: sh

       python -m pip install django-htmx

2. Add the middleware:

   .. code-block:: python

       MIDDLEWARE = [
           ...,
           "django_htmx.HtmxMiddleware",
           ...,
       ]

API
---

``HtmxMiddleware``
^^^^^^^^^^^^^^^^^^

The middleware does a few things:

* It attaches ``request.htmx``, an instance of ``django_htmx.HtmxDetails`` (see below).
* It modifies ``request.method`` based upon the ``X-HTTP-Method-Override`` header.
  htmx sends this header to set what the method “really is” since the JavaScript ``XMLHttpRequest`` API only allows sending ``GET`` and ``POST`` requests.

``HtmxDetails``
^^^^^^^^^^^^^^^

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

``active_element: Optional[str]``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``id`` of the active element if it exists, or ``None``.
Based on the ``HX-Active-Element`` header.

``active_element_name: Optional[str]``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``name`` of the active element if it exists, or ``None``.
Based on the ``HX-Active-Element-Name`` header.

``active_element_value: Optional[str]``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``value`` of the active element if it exists, or ``None``.
Based on the ``HX-Active-Element-Value`` header.

``current_url: Optional[str]``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The current URL of the browser, or ``None`` for non-htmx requests.
Based on the ``HX-Current-URL`` header.

``event_target: Optional[str]``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``id`` of the original event target element, or ``None``.
Based on the ``HX-Event-Target`` header.

``prompt: Optional[str]``
~~~~~~~~~~~~~~~~~~~~~~~~~

The user response to `hx-prompt <https://htmx.org/attributes/hx-prompt/>`__ if it was used, or ``None``.

``target: Optional[str]``
~~~~~~~~~~~~~~~~~~~~~~~~~

The ``id`` of the target element if it exists, or ``None``.
Based on the ``HX-Target`` header.

``trigger: Optional[str]``
~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``id`` of the triggered element if it exists, or ``None``.
Based on the ``HX-Trigger`` header.

``trigger_name: Optional[str]``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``name`` of the triggered element if it exists, or ``None``.
Based on the ``HX-Trigger-Name`` header.
