from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from django_htmx.jinja import django_htmx_script, htmx_script


class HtmxScriptTests(SimpleTestCase):
    def test_default(self):
        result = htmx_script()

        assert result == '<script src="django_htmx/htmx.min.js" defer></script>'

    def test_debug(self):
        with override_settings(DEBUG=True):
            result = htmx_script()

        assert result == (
            '<script src="django_htmx/htmx.min.js" defer></script>'
            + '<script src="django_htmx/django-htmx.js" data-debug="True" defer></script>'
        )

    def test_unminified(self):
        result = htmx_script(minified=False)

        assert result == '<script src="django_htmx/htmx.js" defer></script>'


class DjangoHtmxScriptTests(SimpleTestCase):
    def test_non_debug_empty(self):
        result = django_htmx_script()

        assert result == ""

    def test_debug_success(self):
        with override_settings(DEBUG=True):
            result = django_htmx_script()

        assert result == (
            '<script src="django_htmx/django-htmx.js" data-debug="True" defer></script>'
        )
