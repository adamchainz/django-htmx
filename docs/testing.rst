Testing
=======

.. currentmodule:: django_htmx.testing

Testing htmx views can be repetitive as they usually switch behaviour based on htmx-specific headers.
The ``django_htmx.testing`` module provides tools to simplify this.

.. class:: HtmxClient

   A subclass of Django’s :class:`~django.test.client.Client` that adds an ``htmx`` argument to all request methods (``get()``, ``post()``, etc.).

   When ``htmx`` is set, the client automatically adds htmx-specific headers to the request.

   If ``htmx=True``, the ``HX-Request`` header is set to ``"true"``:

   .. code-block:: python

       from django_htmx.testing import HtmxClient

       client = HtmxClient()

       def test_htmx_view(self):
           # Simple htmx request
           response = client.get("/my-view/", htmx=True)
           assert response.status_code == 200

   If ``htmx`` is a :class:`dict`, the keys are mapped to htmx headers.
   The ``HX-Request`` header is always set to ``"true"`` in this case:

   .. code-block:: python

       def test_htmx_target(self):
           # Request with a specific target
           response = client.get("/my-view/", htmx={"target": "#info-pane"})
           assert response.status_code == 200

   The following keys are supported in the ``htmx`` dictionary:

   * ``boosted`` (maps to ``HX-Boosted``)
   * ``current_url`` (maps to ``HX-Current-URL``)
   * ``history_restore_request`` (maps to ``HX-History-Restore-Request``)
   * ``prompt`` (maps to ``HX-Prompt``)
   * ``target`` (maps to ``HX-Target``)
   * ``trigger`` (maps to ``HX-Trigger``)
   * ``trigger_name`` (maps to ``HX-Trigger-Name``)

   Passing any other key will raise a :class:`ValueError`.

   Using the client without the ``htmx`` argument (or setting it to ``False`` or ``None``) results in a normal non-htmx request.

.. class:: HtmxClientMixin

   A mixin that can be added to custom client classes to add the ``htmx`` argument support.
   This is what :class:`HtmxClient` uses internally.
