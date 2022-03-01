Middleware
==========

.. currentmodule:: django_htmx.middleware

.. class:: HtmxMiddleware

   This middleware attaches ``request.htmx``, an instance of :obj:`HtmxDetails` (below).
   Your views, and any following middleware, can use ``request.htmx`` to switch behaviour for requests from htmx.

   See it action in the “Middleware Tester” section of the :doc:`example project <example_project>`.

.. class:: HtmxDetails

   This class provides shortcuts for reading the htmx-specific `request headers <https://htmx.org/reference/#request_headers>`__.

   .. automethod:: __bool__

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

   .. attribute:: boosted
      :type: bool

      ``True`` if the request came from an element with the ``hx-boost`` attribute.
      Based on the ``HX-Boosted`` header.

      You can use this attribute to change behaviour for boosted requests like so:

      .. code-block:: python

          def my_view(request):
              if request.htmx.boosted:
                  # do something special
                  ...
              return render(...)

   .. attribute:: current_url
      :type: str | None

      The current URL of the browser, or ``None`` for non-htmx requests.
      Based on the ``HX-Current-URL`` header.

   .. attribute:: history_restore_request
      :type: bool

      ``True`` if the request is for history restoration after a miss in the local
      history cache. Based on the ``HX-History-Restore-Request`` header.

   .. attribute:: prompt
      :type: str | None

      The user response to `hx-prompt <https://htmx.org/attributes/hx-prompt/>`__ if it was used, or ``None``.

   .. attribute:: target
      :type: str | None

      The ``id`` of the target element if it exists, or ``None``.
      Based on the ``HX-Target`` header.

   .. attribute:: trigger
      :type: str | None

      The ``id`` of the triggered element if it exists, or ``None``.
      Based on the ``HX-Trigger`` header.

   .. attribute:: trigger_name
      :type: str | None

      The ``name`` of the triggered element if it exists, or ``None``.
      Based on the ``HX-Trigger-Name`` header.

   .. attribute:: triggering_event
      :type: Any | None

      The deserialized JSON representation of the event that triggered the request if it exists, or ``None``.
      This header is set by the `event-header htmx extension <https://htmx.org/extensions/event-header/>`__, and contains details of the DOM event that triggered the request.
