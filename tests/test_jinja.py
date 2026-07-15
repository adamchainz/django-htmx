from __future__ import annotations

import secrets

import django
import pytest
from django.test import SimpleTestCase, override_settings

from django_htmx.jinja import django_htmx_script, htmx_script


class HtmxScriptTests(SimpleTestCase):
    def test_default(self):
        result = htmx_script()

        assert result == '<script src="django_htmx/htmx-2.min.js" defer></script>'

    @pytest.mark.skipif(django.VERSION < (6, 0), reason="Django 6.0+")
    def test_default_nonce(self):
        from django.utils.csp import LazyNonce

        nonce = LazyNonce()

        result = htmx_script(nonce=nonce)

        assert (
            result
            == f'<script src="django_htmx/htmx-2.min.js" defer nonce="{nonce}"></script>'
        )

    def test_debug(self):
        with override_settings(DEBUG=True):
            result = htmx_script()

        assert result == (
            '<script src="django_htmx/htmx-2.min.js" defer></script>'
            + '<script src="django_htmx/django-htmx.js" data-debug="True" defer></script>'
        )

    @pytest.mark.skipif(django.VERSION < (6, 0), reason="Django 6.0+")
    def test_debug_nonce(self):
        from django.utils.csp import LazyNonce

        nonce = LazyNonce()

        with override_settings(DEBUG=True):
            result = htmx_script(nonce=nonce)

        assert result == (
            f'<script src="django_htmx/htmx-2.min.js" defer nonce="{nonce}"></script>'
            + f'<script src="django_htmx/django-htmx.js" data-debug="True" defer nonce="{nonce}"></script>'
        )

    def test_version_2(self):
        result = htmx_script(version=2)

        assert result == '<script src="django_htmx/htmx-2.min.js" defer></script>'

    def test_version_4(self):
        result = htmx_script(version=4)

        assert result == '<script src="django_htmx/htmx-4.min.js" defer></script>'

    def test_version_4_unminified(self):
        result = htmx_script(version=4, minified=False)

        assert result == '<script src="django_htmx/htmx-4.js" defer></script>'

    def test_version_unsupported(self):
        with pytest.raises(ValueError) as excinfo:
            htmx_script(version=3)

        assert excinfo.value.args[0] == (
            "Unsupported htmx version 3, must be one of: 2, 4"
        )

    def test_extensions_string(self):
        result = htmx_script(extensions="sse")

        assert result == (
            '<script src="django_htmx/htmx-2.min.js" defer></script>'
            + '<script src="django_htmx/ext/sse-2.min.js" defer></script>'
        )

    def test_extensions_string_multiple(self):
        result = htmx_script(extensions="sse, ws")

        assert result == (
            '<script src="django_htmx/htmx-2.min.js" defer></script>'
            + '<script src="django_htmx/ext/sse-2.min.js" defer></script>'
            + '<script src="django_htmx/ext/ws-2.min.js" defer></script>'
        )

    def test_extensions_sequence(self):
        result = htmx_script(extensions=["head-support", "preload"])

        assert result == (
            '<script src="django_htmx/htmx-2.min.js" defer></script>'
            + '<script src="django_htmx/ext/head-support-2.min.js" defer></script>'
            + '<script src="django_htmx/ext/preload-2.min.js" defer></script>'
        )

    def test_extensions_version_4(self):
        result = htmx_script(version=4, extensions="ws")

        assert result == (
            '<script src="django_htmx/htmx-4.min.js" defer></script>'
            + '<script src="django_htmx/ext/ws-4.min.js" defer></script>'
        )

    def test_extensions_unminified_nonce(self):
        nonce = secrets.token_urlsafe(16)

        result = htmx_script(minified=False, extensions="sse", nonce=nonce)

        assert result == (
            f'<script src="django_htmx/htmx-2.js" defer nonce="{nonce}"></script>'
            + f'<script src="django_htmx/ext/sse-2.js" defer nonce="{nonce}"></script>'
        )

    def test_extensions_unknown(self):
        with pytest.raises(ValueError) as excinfo:
            htmx_script(extensions="response-targets")

        assert excinfo.value.args[0] == (
            "Unknown htmx extension 'response-targets', must be one of: "
            + "head-support, preload, sse, ws"
        )

    def test_unminified(self):
        result = htmx_script(minified=False)

        assert result == '<script src="django_htmx/htmx-2.js" defer></script>'

    def test_unminified_nonce(self):
        nonce = secrets.token_urlsafe(16)

        result = htmx_script(minified=False, nonce=nonce)

        assert (
            result
            == f'<script src="django_htmx/htmx-2.js" defer nonce="{nonce}"></script>'
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

    @pytest.mark.skipif(django.VERSION < (6, 0), reason="Django 6.0+")
    def test_debug_nonce(self):
        from django.utils.csp import LazyNonce

        nonce = LazyNonce()

        with override_settings(DEBUG=True):
            result = django_htmx_script(nonce=nonce)

        assert result == (
            f'<script src="django_htmx/django-htmx.js" data-debug="True" defer nonce="{nonce}"></script>'
        )
