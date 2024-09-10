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

Using template partials
~~~~~~~~~~~~~~~~~~~~~~~

There's a third-party package called `django-template-partials`_ that allows
defining reusable named inline partials for the Django Template Language.

Once you've installed ``django-template-partials`` you can add the ``{%
partialdef %}`` tag to your template to mark the reusable section:

{% extends "_base.html" %}
{% load partials %}

{% block main %}

  ...

  {% partialdef table-section inline %}

    Reusable table content here

  {% endpartialdef %}

  ...

{% endblock %}

Here the core content for your table is contained by the ``table-section``
partial. Due to the ``inline`` argument, when the full page is rendered the
table will be output as normal.

In your view, then, when you're making an HTMX request, you can append the
partial name to your template name in order to render only that fragment:

.. code-block:: python

    from django.http import HttpRequest, HttpResponse
    from django.shortcuts import render
    from django.views.decorators.http import require_GET


    @require_GET
    def partial_rendering(request: HtmxHttpRequest) -> HttpResponse:
        # Standard Django pagination
        page_num = request.GET.get("page", "1")
        page = Paginator(object_list=people, per_page=10).get_page(page_num)

        # The htmx magic - render just the `#table-section` partial for htmx
        # requests, allowing us to skip rendering the unchanging parts of the
        # template.
        template_name = "partial-rendering.html"
        if request.htmx:
            template_name += "#table-section"

        return render(
            request,
            template_name,
            {
                "page": page,
            },
        )

For an example of this in action, see the “Partial Rendering” page of the
:doc:`example project <example_project>`.

See the `django-template-partials`_ README for more details.

.. _django-template-partials: https://github.com/carltongibson/django-template-partials

Swapping the base template
~~~~~~~~~~~~~~~~~~~~~~~~~~

Another technique, that's a little more manual, but good to have on-hand in
case you need it, is to swap the base template in your view.

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
