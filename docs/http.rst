HTTP
====

.. currentmodule:: django_htmx.http

.. autoclass:: HttpResponseClientRedirect

   htmx can trigger a client side redirect when it receives a response with the |HX-Redirect header|__.
   ``HttpResponseClientRedirect`` is a `HttpResponseRedirect <https://docs.djangoproject.com/en/stable/ref/request-response/#django.http.HttpResponseRedirect>`__ subclass for triggering such redirects.

   .. |HX-Redirect header| replace:: ``HX-Redirect`` header
   __ https://htmx.org/reference/#response_headers

   For example:

   .. code-block:: python

       from django_htmx.http import HttpResponseClientRedirect


       def sensitive_view(request):
           if not sudo_mode.active(request):
               return HttpResponseClientRedirect("/activate-sudo-mode/")
           ...

.. autoclass:: HttpResponseClientRefresh

   htmx will trigger a page reload when it receives a response with the |HX-Refresh header|__.
   ``HttpResponseClientRefresh`` is a `custom response class <https://docs.djangoproject.com/en/stable/ref/request-response/#custom-response-classes>`__ that allows you to send such a response.
   It takes no arguments, since htmx ignores any content.

   .. |HX-Refresh header| replace:: ``HX-Refresh`` header
   __ https://htmx.org/reference/#response_headers

   For example:

   .. code-block:: python

       from django_htmx.http import HttpResponseClientRefresh


       def partial_table_view(request):
           if page_outdated(request):
               return HttpResponseClientRefresh()
           ...

.. autoclass:: HttpResponseStopPolling

   When using a `polling trigger <https://htmx.org/docs/#polling>`__, htmx will stop polling when it encounters a response with the special HTTP status code 286.
   ``HttpResponseStopPolling`` is a `custom response class <https://docs.djangoproject.com/en/stable/ref/request-response/#custom-response-classes>`__ with that status code.

   For example:

   .. code-block:: python

       from django_htmx.http import HttpResponseStopPolling


       def my_pollable_view(request):
           if event_finished():
               return HttpResponseStopPolling()
           ...

.. data:: HTMX_STOP_POLLING
   :type: int
   :value: 286

   A constant for the HTTP status code 286.
   You can use this instead of ``HttpResponseStopPolling`` to tell htmx to stop polling.

   For example, with Django’s `render shortcut <https://docs.djangoproject.com/en/stable/topics/http/shortcuts/#django.shortcuts.render>`__:

   .. code-block:: python

       from django_htmx.http import HTMX_STOP_POLLING


       def my_pollable_view(request):
           if event_finished():
               return render("event-finished.html", status=HTMX_STOP_POLLING)
           ...

.. autofunction:: push_url

   Modify the |HX-Push-Url header|__ of ``response`` to push a URL into the browser location history, and return the response.
   Pass a string with the URL to push, or ``False`` to prevent the location history from being updated.

   .. |HX-Push-Url header| replace:: ``HX-Push-Url`` header
   __ https://htmx.org/headers/hx-push-url/

   Calling ``push_url`` multiple times for the same ``response`` will replace the value of the header.

.. autofunction:: trigger_client_event

   Modify the |HX-Trigger headers|__ of ``response`` to trigger client-side events, and return the response.
   Takes the name of the event to trigger and any JSON-compatible parameters for it, and stores them in the appropriate header. Uses |DjangoJSONEncoder|__ for its extended data type support.

   .. |HX-Trigger headers| replace:: ``HX-Trigger`` headers
   __ https://htmx.org/headers/hx-trigger/

   .. |DjangoJSONEncoder| replace:: ``DjangoJSONEncoder``
   __ https://docs.djangoproject.com/en/stable/topics/serialization/#django.core.serializers.json.DjangoJSONEncoder

   Which of the ``HX-Trigger`` headers is modified depends on the value of ``after``:

   * ``"receive"``, the default, maps to ``HX-Trigger``
   * ``"settle"`` maps to ``HX-Trigger-After-Settle``
   * ``"swap"`` maps to ``HX-Trigger-After-Swap``

   Calling ``trigger_client_event`` multiple times for the same ``response`` and ``after`` will add or replace the given event name and preserve others.

   For example:

   .. code-block:: python

       from django_htmx.http import trigger_client_event


       def end_of_long_process(request):
           response = render("end-of-long-process.html")
           return trigger_client_event(
               response,
               "showConfetti",
               {"colours": ["purple", "red", "pink"]},
               after="swap",
           )
