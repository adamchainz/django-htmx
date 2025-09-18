from __future__ import annotations

import secrets

from django.test import SimpleTestCase, override_settings

from django_htmx.jinja import django_htmx_script, htmx_script


class HtmxScriptTests(SimpleTestCase):
    def test_default(self):
        result = htmx_script()

        assert result == '<script src="django_htmx/htmx.min.js" defer></script>'

    def test_default_nonce(self):
        nonce = secrets.token_urlsafe(16)

        result = htmx_script(nonce=nonce)

        assert (
            result
            == f'<script src="django_htmx/htmx.min.js" defer nonce="{nonce}"></script>'
        )

    def test_debug(self):
        with override_settings(DEBUG=True):
            result = htmx_script()

        assert result == (
            '<script src="django_htmx/htmx.min.js" defer></script>'
            + '<script src="django_htmx/django-htmx.js" data-debug="True" defer></script>'
        )

    def test_debug_nonce(self):
        nonce = secrets.token_urlsafe(16)

        with override_settings(DEBUG=True):
            result = htmx_script(nonce=nonce)

        assert result == (
            f'<script src="django_htmx/htmx.min.js" defer nonce="{nonce}"></script>'
            + f'<script src="django_htmx/django-htmx.js" data-debug="True" defer nonce="{nonce}"></script>'
        )

    def test_unminified(self):
        result = htmx_script(minified=False)

        assert result == '<script src="django_htmx/htmx.js" defer></script>'

    def test_unminified_nonce(self):
        nonce = secrets.token_urlsafe(16)

        result = htmx_script(minified=False, nonce=nonce)

        assert (
            result
            == f'<script src="django_htmx/htmx.js" defer nonce="{nonce}"></script>'
        )


class DjangoHtmxScriptTests(SimpleTestCase):
    def test_non_debug_empty(self):
        result = django_htmx_script()

        assert result == ""

    def test_debug(self):
        with override_settings(DEBUG=True):
            result = django_htmx_script()

        assert result == (
            '<script src="django_htmx/django-htmx.js" data-debug="True" defer></script>'
        )

    def test_debug_nonce(self):
        nonce = secrets.token_urlsafe(16)

        with override_settings(DEBUG=True):
            result = django_htmx_script(nonce=nonce)

        assert result == (
            f'<script src="django_htmx/django-htmx.js" data-debug="True" defer nonce="{nonce}"></script>'
        )
