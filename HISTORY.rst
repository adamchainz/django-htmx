=======
History
=======

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
