from __future__ import annotations

from django.template import Context
from django.template import Template
from django.test import override_settings
from django.test import SimpleTestCase


class DjangoHtmxScriptTests(SimpleTestCase):
    def test_non_debug_empty(self):
        result = Template("{% load django_htmx %}{% django_htmx_script %}").render(
            Context()
        )

        assert result == ""

    def test_debug_success(self):
        with override_settings(DEBUG=True):
            result = Template("{% load django_htmx %}{% django_htmx_script %}").render(
                Context()
            )

        assert result == (
            '<script src="django-htmx.js" data-debug="True" defer></script>'
        )
