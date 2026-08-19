Installation
============

Requirements
------------

Python 3.10 to 3.15 supported.

Django 5.2 to 6.1 supported.

Installation
------------

1. Install with **pip**:

   .. code-block:: sh

       python -m pip install django-mcpz

2. Add django-mcpz to your ``INSTALLED_APPS``:

   .. code-block:: python

       INSTALLED_APPS = [
           ...,
           "django_mcpz",
           ...,
       ]

3. Define an endpoint and route it, as covered in :doc:`endpoints`.

4. Set the ``MCPZ_TOKEN`` setting to a long random string, the bearer token that clients must send.
   Endpoints are authenticated by default; see :ref:`endpoints-authentication` for alternatives, including disabling authentication.

5. (Optional) Add the middleware:

   .. code-block:: python

       MIDDLEWARE = [
           ...,
           "django_mcpz.middleware.MCPMiddleware",
           ...,
       ]

   The middleware adds ``request.mcp``, as described in :doc:`middleware`.
