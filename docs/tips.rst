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
