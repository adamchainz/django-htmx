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

4. (Optional) Add the extension script to your template, as documented in
   :doc:`extension`

It’s up to you to add htmx (and any extensions) to your project, via a
``<script>`` tag in your base template. Forut resilience, you probably want to
download it into your project’s static files, rather than rely on the
``unpkg.com`` hosted version.
