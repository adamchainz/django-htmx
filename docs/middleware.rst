Middleware
==========

.. currentmodule:: django_htmx.middleware

.. class:: HtmxMiddleware

   This middleware attaches ``request.htmx``, an instance of :obj:`HtmxDetails` (below).
   Your views, and any following middleware, can use ``request.htmx`` to switch behaviour for requests from htmx.
   The middleware supports both sync and async modes.

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

      The current URL in the browser that htmx made this request from, or ``None`` for non-htmx requests.
      Based on the ``HX-Current-URL`` header.

   .. attribute:: current_url_abs_path
      :type: str | None

      The absolute-path form of ``current_url``, that is the URL without scheme or netloc, or ``None`` for non-htmx requests.

      This value will also be ``None`` if the scheme and netloc do not match the request.
      The could happen if the request is cross-origin, or if Django is not configured correctly.

      For example:

      .. code-block:: ipython

          >>> request.htmx.current_url
          'https://example.com/dashboard/?year=2022'
          >>> # assuming request.scheme and request.get_host() match:
          >>> request.htmx.current_url_abs_path
          '/dashboard/?year=2022'

      This is useful for redirects:

      .. code-block:: python

            if not sudo_mode_active(request):
                next_url = request.htmx.current_url_abs_path or ""
                return HttpResponseClientRedirect(f"/activate-sudo/?next={next_url}")

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
