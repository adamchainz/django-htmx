from __future__ import annotations

from django.test import SimpleTestCase
from django.test import override_settings

from django_htmx.jinja import django_htmx_script


class DjangoHtmxScriptTests(SimpleTestCase):
    def test_non_debug_empty(self):
        result = django_htmx_script()

        assert result == ""

    def test_debug_success(self):
        with override_settings(DEBUG=True):
            result = django_htmx_script()

        assert result == (
            '<script src="django-htmx.js" data-debug="True" defer></script>'
        )
