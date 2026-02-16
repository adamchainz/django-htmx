Tips
====

This page contains some tips for using htmx with Django.

.. _tips-csrf-token:

Make htmx pass Django’s CSRF token
----------------------------------

If you use htmx to make requests with “unsafe” methods, such as POST via `hx-post <https://htmx.org/attributes/hx-post/>`__, you will need to make htmx cooperate with Django’s `Cross Site Request Forgery (CSRF) protection <https://docs.djangoproject.com/en/stable/ref/csrf/>`__.
Django can accept the CSRF token in a header, normally ``x-csrftoken`` (configurable with the |CSRF_HEADER_NAME setting|__, but there’s rarely a reason to change it).

.. |CSRF_HEADER_NAME setting| replace:: ``CSRF_HEADER_NAME`` setting
__ https://docs.djangoproject.com/en/stable/ref/settings/#std:setting-CSRF_HEADER_NAME

You can make htmx pass the header with its |hx-headers attribute|__.
It’s most convenient to place ``hx-headers`` on your ``<body>`` tag, as then all elements will inherit it.
For example:

.. |hx-headers attribute| replace:: ``hx-headers`` attribute
__ https://htmx.org/attributes/hx-headers/

.. code-block:: django

   <body hx-headers='{"x-csrftoken": "{{ csrf_token }}"}'>
     ...
   </body>

Note this uses ``{{ csrf_token }}``, the variable, as opposed to ``{% csrf_token %}``, the tag that renders a hidden ``<input>``.

This snippet should work with both Django templates and Jinja.

For an example of this in action, see the “CSRF Demo” page of the :doc:`example project <example_project>`.

.. _partial-rendering:

Partial Rendering
-----------------

For requests made with htmx, you may want to reduce the page content you render, since only part of the page gets updated.
This is a small optimization compared to correctly setting up compression, caching, etc.

Using template partials
~~~~~~~~~~~~~~~~~~~~~~~~

Django 6.0 introduced built-in support for template partials.
This feature allows you to render just one partial from a template, which is particularly useful for htmx requests.
If you are using Django < 6.0, you can use the `django-template-partials <https://github.com/carltongibson/django-template-partials>`__ package.
The only major difference is the usage of the ``{% load partials %}`` tag at the top of the template.

Define a partial using the ``{% partialdef %}`` tag around a template section:

.. code-block:: django

    {% extends "_base.html" %}

    {# {% load partials %}  Only needed for Django < 6.0; not required for Django >= 6.0 #}

    {% block main %}

      <h1>Countries</h1>

      ...

      {% partialdef country-table inline %}
        <table id=country-data>
          <thead>...</thead>
          <tbody>
            {% for country in countries %}
              ...
            {% endfor %}
          </tbody>
        </table>
      {% endpartialdef %}

      ...

    {% endblock main %}

The above template defines a partial named ``country-table``, which renders some table of country data.
The ``inline`` argument makes the partial render when the full page renders.

In the view, you can select to render the partial for htmx requests.
This is done by adding ``#`` and the partial name to the template name:

.. code-block:: python

    from django.shortcuts import render

    from example.models import Country


    def country_listing(request):
        template_name = "countries.html"
        if request.htmx:
            template_name += "#country-table"

        countries = Country.objects.all()

        return render(
            request,
            template_name,
            {
                "countries": countries,
            },
        )

htmx requests will render only the partial, whilst full page requests will render the full page.
This allows refreshing of the table without an extra view or separating the template contents from its context.
For a working example, see the "Partial Rendering" page of the :doc:`example project <example_project>`.

It’s also possible to use a partial from within a separate view.
This may be preferable if other customizations are required for htmx requests.

For more information on Django's template partials, see `the Django documentation <https://docs.djangoproject.com/en/6.0/ref/templates/language/#template-partials>`__.

Swapping the base template
~~~~~~~~~~~~~~~~~~~~~~~~~~

Another technique is to swap the base template in your view.
This is a little more manual but good to have on-hand in case you need it,

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

.. _htmx-extensions:

Install htmx extensions
-----------------------

django-htmx vendors htmx and can render it with the ``{% htmx_script %}`` :doc:`template tag <template_tags>`.
However, it does not include any of `the many htmx extensions <https://htmx.org/extensions/>`__, so it’s up to you to add such extensions to your project.

Avoid using JavaScript CDNs like unpkg.com to include extensions, or any other resources.
They reduce privacy, performance, and security - see `this blog post <https://blog.wesleyac.com/posts/why-not-javascript-cdn>`__.

Instead, download extension scripts into your project’s static files and serve them directly.
Include their script tags after your htmx ``<script>`` tag (from ``{% htmx_script %}`` or otherwise).
For example, if you were using the `WebSocket extension <https://htmx.org/extensions/ws/>`__, you might:

1. Download ``ws.min.js`` from the latest release:

   .. code-block:: sh

       curl -L https://unpkg.com/htmx-ext-ws/dist/ws.min.js -o example/static/htmx-ext-ws.min.js

2. Include it in your base template:

   .. code-block:: django
      :emphasize-lines: 7

       {% load django_htmx static %}
       <!doctype html>
       <html>
         <head>
           ...
           {% htmx_script %}
           <script src="{% static 'htmx-ext-ws.min.js' %}" defer></script>
         </head>
         <body>
           ...
         </body>
       </html>

For another example, see the :doc:`example project <example_project>`, which includes two extensions and a Python script to download their latest versions (``download_htmx_extensions.py``).
