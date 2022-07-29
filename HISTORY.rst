=======
History
=======

1.12.1 (2022-07-29)
-------------------

* Override ``HttpResponseClientRedirect.url`` property to fix ``HttpResponseClientRedirect.__repr__``.

1.12.0 (2022-06-05)
-------------------

* Support Python 3.11.

* Support Django 4.1.

1.11.0 (2022-05-10)
-------------------

* Drop support for Django 2.2, 3.0, and 3.1.

1.10.0 (2022-05-07)
-------------------

* Make ``trigger_client_event()`` return the response.

* Add async support to ``HtmxMiddleware`` to reduce overhead on async views.

1.9.0 (2022-03-02)
------------------

* Move documentation from the README to `Read the Docs <https://django-htmx.readthedocs.io/>`__.
  Also expand it with sections on installing htmx, and configuring CSRF.

  Thanks to Ben Beecher for intial setup in `PR #194 <https://github.com/adamchainz/django-htmx/pull/194>`__.

* Add ``HttpResponseClientRefresh`` for telling htmx to reload the page.

  Thanks to Bogumil Schube in `PR #193 <https://github.com/adamchainz/django-htmx/pull/193>`__.

1.8.0 (2022-01-10)
------------------

* Drop Python 3.6 support.

1.7.0 (2022-01-10)
------------------

* Use ``DjangoJSONEncoder`` for encoding the ``HX-Trigger`` event.

  Thanks to Cleiton de Lima in `PR #182 <https://github.com/adamchainz/django-htmx/pull/182>`__.

* Drop redundant 'async' from debug ``<script>`` tag.

1.6.0 (2021-10-06)
------------------

* Add ``HttpResponseClientRedirect`` class for sending HTMX client-side redirects.

  Thanks to Julio César in `PR #121 <https://github.com/adamchainz/django-htmx/pull/121>`__.

* Add ``django_htmx.http.trigger_client_event()`` for triggering client side events.

1.5.0 (2021-10-05)
------------------

* Support Python 3.10.

1.4.0 (2021-10-02)
------------------

* Support the ``HX-Boosted`` header, which was added in htmx 1.6.0.
  This is parsed into the ``request.htmx.boosted`` attribute.

1.3.0 (2021-09-28)
------------------

* Support Django 4.0.

1.2.1 (2021-07-09)
------------------

* Make extension script error handler also show 404 errors.

1.2.0 (2021-07-08)
------------------

* Installation now requires adding ``"django_htmx"`` to your ``INSTALLED_APPS``
  setting.

* Add extension script with debug error handler. To install it, follow the new
  instructions in the README.

  htmx’s default behaviour is to discard error responses. The extension
  overrides this in debug mode to shows Django’s debug error responses.

* Add ``django_htmx.http`` module with ``HttpResponseStopPolling`` class and
  ``HTMX_STOP_POLLING`` constant.

1.1.0 (2021-06-03)
------------------

* Support the ``HX-History-Restore-Request`` header, which was added in htmx
  1.2.0. This is parsed into the ``request.htmx.history_restore_request``
  attribute.

* Support the ``Triggering-Event`` header, which is sent by the
  `event-header extension <https://htmx.org/extensions/event-header/>`__.
  This is parsed into the ``request.htmx.triggering_event`` attribute.

* Stop distributing tests to reduce package size. Tests are not intended to be
  run outside of the tox setup in the repository. Repackagers can use GitHub's
  tarballs per tag.

1.0.1 (2021-02-08)
------------------

* Remove ``X-HTTP-Method-Override`` handling from ``HtmxMiddleware``. This has
  not been needed since htmx 0.0.5, when use of the header was extracted
  to its ``method-override`` extension in `htmx commit
  2305ae <https://github.com/bigskysoftware/htmx/commit/2305aed18e925da55f15dc5798db37ac0142f2b4>`__.

1.0.0 (2021-02-07)
------------------

* Add ``HtmxMiddleware`` which handles request headers from htmx.
* Add example app on GitHub repository which demonstrates using django-htmx
  features.
* Remove the ``{% htmx_script %}`` template tag. Include htmx on your pages
  yourself - this allows you to better customize the way htmx is installed to
  suit your project - for example by using the ``async`` script attribute or
  by bundling it with extensions.
* Remove the ``HTMXViewMixin``, ``{% htmx_include %}`` and ``{% htmx_attrs %}``
  tags. Partial rendering can be done more with a simpler techinque - see
  the demo page in the example app, added in
  `Pull Request #30 <https://github.com/adamchainz/django-htmx/pull/30>`__.

0.1.4 (2020-06-30)
------------------

* This version and those before explored what's possible with htmx and django,
  but were not documented.
