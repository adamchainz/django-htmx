=======
History
=======

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
