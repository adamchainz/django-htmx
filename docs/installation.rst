Installation
============

Requirements
------------

Python 3.7 to 3.12 supported.

Django 3.2 to 4.2 supported.

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

   The middleware adds ``request.htmx``, as described in :doc:`middleware`.

4. (Optional) Add the extension script to your base template, as documented in :doc:`extension_script`.

Install htmx
------------

django-htmx does not include htmx itself, since it can work with many different versions.
It’s up to you to add htmx (and any extensions) to your project.

.. warning:: **JavaScript CDN’s**

   Avoid using JavaScript CDN’s like unpkg.com to include htmx (or any resources).
   They reduce privacy, performance, and security - see `this post <https://blog.wesleyac.com/posts/why-not-javascript-cdn>`__.

You can add htmx like so:

1. Download ``htmx.min.js`` from `its latest release <https://unpkg.com/browse/htmx.org/dist/>`__.

2. Put ``htmx.min.js`` in a static directory in your project.
   For example, if you have a ``static/`` directory in your |STATICFILES_DIRS setting|__:

   .. |STATICFILES_DIRS setting| replace:: ``STATICFILES_DIRS`` setting
   __ https://docs.djangoproject.com/en/stable/ref/settings/#std:setting-STATICFILES_DIRS

   .. code-block:: python

      STATICFILES_DIRS = [BASE_DIR / "static"]

   …then put it in there, organized as you like, such as in a ``js/`` sub-directory.

3. Add a ``<script>`` tag in your base template, within your ``<head>``:

   .. code-block:: django

      {% load static %}
      <script src="{% static 'htmx.min.js' %}" defer></script>

   (or ``js/htmx.min.js``, etc.).

   The |defer attribute|__ allows the browser to continue rendering the page whilst htmx is downloading, making your site’s first render faster.

   .. |defer attribute| replace:: ``defer`` attribute
   __ https://html.spec.whatwg.org/multipage/scripting.html#attr-script-defer

   If you have multiple base templates for pages that you want htmx on, add the ``<script>`` on all of them.

.. note:: **Extensions**

   You can adapt the above steps to set up `htmx’s extensions <https://htmx.org/extensions/#reference>`__ that you wish to use.
   Download them from htmx’s ``ext/`` folder into your project, and include their script tags after htmx, for example:

   .. code-block:: django

      {% load static %}
      <script src="{% static 'js/htmx/htmx.min.js' %}" defer></script>
      <script src="{% static 'js/htmx/debug.js' %}" defer></script>
