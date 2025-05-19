from __future__ import annotations

from django.template import Context, Template
from django.test import SimpleTestCase, override_settings


class HtmxScriptTests(SimpleTestCase):
    def test_default(self):
        result = Template("{% load django_htmx %}{% htmx_script %}").render(Context())

        assert result == '<script src="django_htmx/htmx.min.js" defer></script>'

    def test_debug(self):
        with override_settings(DEBUG=True):
            result = Template("{% load django_htmx %}{% htmx_script %}").render(
                Context()
            )

        assert result == (
            '<script src="django_htmx/htmx.min.js" defer></script>'
            + '<script src="django_htmx/django-htmx.js" data-debug="True" defer></script>'
        )

    def test_unminified(self):
        result = Template(
            "{% load django_htmx %}{% htmx_script minified=False %}"
        ).render(Context())

        assert result == '<script src="django_htmx/htmx.js" defer></script>'


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
            '<script src="django_htmx/django-htmx.js" data-debug="True" defer></script>'
        )
