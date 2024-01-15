Tips
====

This page contains some tips for using htmx with Django.

Make htmx Pass the CSRF Token
-----------------------------

If you use htmx to make requests with “unsafe” methods, such as POST via `hx-post <https://htmx.org/attributes/hx-post/>`__, you will need to make htmx cooperate with Django’s `Cross Site Request Forgery (CSRF) protection <https://docs.djangoproject.com/en/stable/ref/csrf/>`__.
Django can accept the CSRF token in a header, normally `X-CSRFToken` (configurable with the |CSRF_HEADER_NAME setting|__, but there’s rarely a reason to change it).

.. |CSRF_HEADER_NAME setting| replace:: ``CSRF_HEADER_NAME`` setting
__ https://docs.djangoproject.com/en/stable/ref/settings/#std:setting-CSRF_HEADER_NAME

You can make htmx pass the header with its |hx-headers attribute|__.
It’s most convenient to place ``hx-headers`` on your ``<body>`` tag, as then all elements will inherit it.
For example:

.. |hx-headers attribute| replace:: ``hx-headers`` attribute
__ https://htmx.org/attributes/hx-headers/

.. code-block:: django

   <body hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'>
      ...
   </body>

Note this uses ``{{ csrf_token }}``, the variable, as opposed to ``{% csrf_token %}``, the tag that renders a hidden ``<input>``.

This snippet should work with both Django templates and Jinja.

For an example of this in action, see the “CSRF Demo” page of the :doc:`example project <example_project>`.

Partial Rendering
-----------------

For requests made with htmx, you may want to reduce the page content you render, since only part of the page gets updated.
This is a small optimization compared to correctly setting up compression, caching, etc.

You can use Django’s template inheritance to limit rendered content to only the affected section.
In your view, set up a context variable for your base template like so:

.. code-block:: python

   from django.http import HttpRequest, HttpResponse
   from django.shortcuts import render
   from django.views.decorators.http import require_GET


   @require_GET
   def partial_rendering(request: HttpRequest) -> HttpResponse:
       if request.htmx:
           base_template = "_partial.html"
       else:
           base_template = "_base.html"

       ...

       return render(
           request,
           "page.html",
           {
               "base_template": base_template,
               # ...
           },
       )

Then in the template (``page.html``), use that variable in ``{% extends %}``:

.. code-block:: django

   {% extends base_template %}

   {% block main %}
     ...
   {% endblock %}

Here, ``_base.html`` would be the main site base:

.. code-block:: django

    <!doctype html>
    <html>
    <head>
      ...
    </head>
    <body>
      <header>
        <nav>
          ...
        </nav>
      </header>
      <main id="main">
        {% block main %}{% endblock %}
      </main>
    </body>

…whilst ``_partial.html`` would contain only the minimum element to update:

.. code-block:: django

   <main id="main">
     {% block main %}{% endblock %}
   </main>

For an example of this in action, see the “Partial Rendering” page of the :doc:`example project <example_project>`.

Type Hints
----------

For autocompletion with the `request.htmx` property and and to avoid type-checking errors, such as those identified by mypy, regarding a non-existent property, this package provides
an `HttpRequest` subclass. This subclass adds the `htmx` property type hint to the request object. Here's how to use it:

.. code-block:: python

   from django_htmx.http import HttpRequest


   def my_view(request: HtmxRequest) -> HttpResponse:
       if request.htmx:
           ...
       else:
           ...
