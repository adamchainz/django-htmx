from __future__ import annotations

import secrets

import django
import pytest
from django.template import Context, Template
from django.test import SimpleTestCase, override_settings


class HtmxScriptTests(SimpleTestCase):
    def test_default(self):
        result = Template("{% load django_htmx %}{% htmx_script %}").render(Context())

        assert result == '<script src="django_htmx/htmx.min.js" defer></script>'

    @pytest.mark.skipif(django.VERSION < (6, 0), reason="Django 6.0+")
    def test_default_nonce(self):
        from django.utils.csp import LazyNonce

        nonce = LazyNonce()
        result = Template("{% load django_htmx %}{% htmx_script %}").render(
            Context({"csp_nonce": nonce})
        )

        assert (
            result
            == f'<script src="django_htmx/htmx.min.js" defer nonce="{nonce}"></script>'
        )

    def test_debug(self):
        with override_settings(DEBUG=True):
            result = Template("{% load django_htmx %}{% htmx_script %}").render(
                Context()
            )

        assert result == (
            '<script src="django_htmx/htmx.min.js" defer></script>'
            + '<script src="django_htmx/django-htmx.js" data-debug="True" defer></script>'
        )

    def test_debug_nonce(self):
        nonce = secrets.token_urlsafe(16)
        with override_settings(DEBUG=True):
            result = Template("{% load django_htmx %}{% htmx_script %}").render(
                Context({"csp_nonce": nonce})
            )

        assert result == (
            f'<script src="django_htmx/htmx.min.js" defer nonce="{nonce}"></script>'
            + f'<script src="django_htmx/django-htmx.js" data-debug="True" defer nonce="{nonce}"></script>'
        )

    def test_unminified(self):
        result = Template(
            "{% load django_htmx %}{% htmx_script minified=False %}"
        ).render(Context())

        assert result == '<script src="django_htmx/htmx.js" defer></script>'

    def test_unminified_nonce(self):
        nonce = secrets.token_urlsafe(16)
        result = Template(
            "{% load django_htmx %}{% htmx_script minified=False %}"
        ).render(Context({"csp_nonce": nonce}))

        assert (
            result
            == f'<script src="django_htmx/htmx.js" defer nonce="{nonce}"></script>'
        )


class DjangoHtmxScriptTests(SimpleTestCase):
    def test_non_debug_empty(self):
        result = Template("{% load django_htmx %}{% django_htmx_script %}").render(
            Context()
        )

        assert result == ""

    def test_debug(self):
        with override_settings(DEBUG=True):
            result = Template("{% load django_htmx %}{% django_htmx_script %}").render(
                Context()
            )

        assert result == (
            '<script src="django_htmx/django-htmx.js" data-debug="True" defer></script>'
        )

    @pytest.mark.skipif(django.VERSION < (6, 0), reason="Django 6.0+")
    def test_debug_nonce(self):
        from django.utils.csp import LazyNonce

        nonce = LazyNonce()
        with override_settings(DEBUG=True):
            result = Template("{% load django_htmx %}{% django_htmx_script %}").render(
                Context({"csp_nonce": nonce})
            )

        assert result == (
            f'<script src="django_htmx/django-htmx.js" data-debug="True" defer nonce="{nonce}"></script>'
        )
