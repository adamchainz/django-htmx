HTTP tools
==========

.. currentmodule:: django_htmx.http

Response classes
----------------

.. autoclass:: HttpResponseClientRedirect

   htmx can trigger a client side redirect when it receives a response with the |HX-Redirect header|__.
   ``HttpResponseClientRedirect`` is a `HttpResponseRedirect <https://docs.djangoproject.com/en/stable/ref/request-response/#django.http.HttpResponseRedirect>`__ subclass for triggering such redirects.

   .. |HX-Redirect header| replace:: ``HX-Redirect`` header
   __ https://htmx.org/reference/#response_headers

   :param redirect_to:
      The path to redirect to, as per ``HttpResponseRedirect``.

   :param args:
      Other ``HTTPResponse`` parameters.

   :param kwargs:
      Other ``HTTPResponse`` parameters.

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

.. autoclass:: HttpResponseLocation

   An HTTP response class for sending the |HX-Location header|__.
   This header makes htmx make a client-side “boosted” request, acting like a client side redirect with a page reload.

   .. |HX-Location header| replace:: ``HX-Location`` header
   __ https://htmx.org/headers/hx-location/

   :param redirect_to:
      The path to redirect to, as per |HttpResponseRedirect|__.

      .. |HttpResponseRedirect| replace:: ``HttpResponseRedirect``
      __ https://docs.djangoproject.com/en/stable/ref/request-response/#django.http.HttpResponseRedirect

   :param source:
      The source element of the request.

   :param event:
      The event that “triggered” the request.

   :param target:
      CSS selector to target.

   :param swap:
      How the response will be swapped into the target.

   :param select:
      Select the content that will be swapped from a response.

   :param values:
      values to submit with the request.

   :param headers:
      headers to submit with the request.

   :param args:
      Other ``HTTPResponse`` parameters.

   :param kwargs:
      Other ``HTTPResponse`` parameters.

   For example:

   .. code-block:: python

        from django_htmx.http import HttpResponseLocation


        def wait_for_completion(request, action_id):
            ...
            if action.completed:
                return HttpResponseLocation(f"/action/{action.id}/completed/")
            ...

.. autoclass:: HttpResponseStopPolling

   When using a `polling trigger <https://htmx.org/docs/#polling>`__, htmx will stop polling when it encounters a response with the special HTTP status code 286.
   ``HttpResponseStopPolling`` is a `custom response class <https://docs.djangoproject.com/en/stable/ref/request-response/#custom-response-classes>`__ with that status code.

   :param args:
      Other ``HTTPResponse`` parameters.

   :param kwargs:
      Other ``HTTPResponse`` parameters.

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
   You can use this instead of ``HttpResponseStopPolling`` to stop htmx from polling.

   For example, with Django’s `render shortcut <https://docs.djangoproject.com/en/stable/topics/http/shortcuts/#django.shortcuts.render>`__:

   .. code-block:: python

       from django.shortcuts import render
       from django_htmx.http import HTMX_STOP_POLLING


       def my_pollable_view(request):
           if event_finished():
               return render(request, "event-finished.html", status=HTMX_STOP_POLLING)
           ...

Response modifying functions
----------------------------

.. autofunction:: push_url

   Set the |HX-Push-Url header|__ of ``response`` and return it.
   This header makes htmx push the given URL into the browser location history.

   .. |HX-Push-Url header| replace:: ``HX-Push-Url`` header
   __ https://htmx.org/headers/hx-push-url/


   :param response:
      The response to modify and return.

   :param url:
      The (relative) URL to push, or ``False`` to prevent the location history from being updated.

   For example:

   .. code-block:: python

      from django_htmx.http import push_url


      def leaf(request, leaf_id):
          ...
          if leaf is None:
              # Directly render branch view
              response = branch(request, branch=leaf.branch)
              return push_url(response, f"/branch/{leaf.branch.id}")
          ...

.. autofunction:: replace_url

   Set the |HX-Replace-Url header|__ of ``response`` and return it.
   This header causes htmx to replace the current URL in the browser location history.

   .. |HX-Replace-Url header| replace:: ``HX-Replace-Url`` header
   __ https://htmx.org/headers/hx-replace-url/

   :param response:
      The response to modify and return.

   :param url:
      The (relative) URL to replace, or ``False`` to prevent the location history from being updated.

   For example:

   .. code-block:: python

      from django_htmx.http import replace_url


      def dashboard(request):
          ...
          response = render(request, "dashboard.html", ...)
          # Pretend the user was always on the dashboard, rather than wherever
          # they were on before.
          return replace_url(response, "/dashboard/")

.. autofunction:: reswap

   Set the |HX-Reswap header|__ of ``response`` and return it.
   This header overrides the `swap method <https://htmx.org/attributes/hx-swap/>`__ that htmx will use.

   .. |HX-Reswap header| replace:: ``HX-Reswap`` header
   __ https://htmx.org/reference/#response_headers

   :param response:
      The response to modify and return.

   :param method:
      The swap method.

   For example:

   .. code-block:: python

      from django.shortcuts import render
      from django_htmx.http import reswap


      def employee_table_row(request):
          ...
          response = render(...)
          if employee.is_boss:
              reswap(response, "afterbegin")
          return response

.. autofunction:: retarget

   Set the |HX-Retarget header|__ of ``response`` and return it.
   This header overrides the element that htmx will swap content into.

   .. |HX-Retarget header| replace:: ``HX-Retarget`` header
   __ https://htmx.org/reference/#response_headers

   :param response:
      The response to modify and return.

   :param target:
      CSS selector to target.

   For example:

   .. code-block:: python

      from django.shortcuts import render
      from django.views.decorators.http import require_POST
      from django_htmx.http import retarget


      @require_POST
      def add_widget(request):
          ...

          if form.is_valid():
              # Rerender the whole table on success
              response = render(request, "widget-table.html", ...)
              return retarget(response, "#widgets")

          # Render just inline table row on failure
          return render(request, "widget-table-row.html", ...)

.. autofunction:: trigger_client_event

   Modify one of the |HX-Trigger headers|__ of ``response`` and return it.
   These headers make htmx trigger client-side events.

   Calling ``trigger_client_event`` multiple times for the same ``response`` and ``after`` will update the appropriate header, preserving existing event specifications.

   .. |HX-Trigger headers| replace:: ``HX-Trigger`` headers
   __ https://htmx.org/headers/hx-trigger/

   :param response:
      The response to modify and return.

   :param name:
      The name of the event to trigger.

   :param params:
      Optional JSON-compatible parameters for the event.

   :param after:
      Which ``HX-Trigger`` header to modify:

      * ``"receive"``, the default, maps to ``HX-Trigger``
      * ``"settle"`` maps to ``HX-Trigger-After-Settle``
      * ``"swap"`` maps to ``HX-Trigger-After-Swap``

   :param encoder:

      The |JSONEncoder|__ class used to generate the JSON.
      Defaults to |DjangoJSONEncoder|__ for its extended data type support.

      .. |JSONEncoder| replace:: ``JSONEncoder``
      __ https://docs.python.org/3/library/json.html#json.JSONEncoder

      .. |DjangoJSONEncoder| replace:: ``DjangoJSONEncoder``
      __ https://docs.djangoproject.com/en/stable/topics/serialization/#django.core.serializers.json.DjangoJSONEncoder

   For example:

   .. code-block:: python

       from django.shortcuts import render
       from django_htmx.http import trigger_client_event


       def end_of_long_process(request):
           response = render(request, "end-of-long-process.html")
           return trigger_client_event(
               response,
               "showConfetti",
               {"colours": ["purple", "red", "pink"]},
               after="swap",
           )
