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

.. _partial-rendering:

Partial Rendering
-----------------

For requests made with htmx, you may want to reduce the page content you render, since only part of the page gets updated.
This is a small optimization compared to correctly setting up compression, caching, etc.

Using django-template-partials
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The `django-template-partials package <https://github.com/carltongibson/django-template-partials>`__ extends the Django Template Language with reusable sections called “partials”.
It then allows you to render just one partial from a template.

Install ``django-template-partials`` and add its ``{% partialdef %}`` tag around a template section:

.. code-block:: django

    {% extends "_base.html" %}

    {% load partials %}

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
For a working example, see the “Partial Rendering” page of the :doc:`example project <example_project>`.

It’s also possible to use a partial from within a separate view.
This may be preferable if other customizations are required for htmx requests.

For more information on django-template-partials, see `its documentation <https://github.com/carltongibson/django-template-partials>`__.

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
