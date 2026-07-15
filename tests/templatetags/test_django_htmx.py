from __future__ import annotations

import secrets

import django
import pytest
from django.template import Context, Template
from django.test import SimpleTestCase, override_settings


class HtmxScriptTests(SimpleTestCase):
    def test_default(self):
        result = Template("{% load django_htmx %}{% htmx_script %}").render(Context())

        assert result == '<script src="django_htmx/htmx-2.min.js" defer></script>'

    @pytest.mark.skipif(django.VERSION < (6, 0), reason="Django 6.0+")
    def test_default_nonce(self):
        from django.utils.csp import LazyNonce

        nonce = LazyNonce()
        result = Template("{% load django_htmx %}{% htmx_script %}").render(
            Context({"csp_nonce": nonce})
        )

        assert (
            result
            == f'<script src="django_htmx/htmx-2.min.js" defer nonce="{nonce}"></script>'
        )

    def test_debug(self):
        with override_settings(DEBUG=True):
            result = Template("{% load django_htmx %}{% htmx_script %}").render(
                Context()
            )

        assert result == (
            '<script src="django_htmx/htmx-2.min.js" defer></script>'
            + '<script src="django_htmx/django-htmx.js" data-debug="True" defer></script>'
        )

    def test_debug_nonce(self):
        nonce = secrets.token_urlsafe(16)
        with override_settings(DEBUG=True):
            result = Template("{% load django_htmx %}{% htmx_script %}").render(
                Context({"csp_nonce": nonce})
            )

        assert result == (
            f'<script src="django_htmx/htmx-2.min.js" defer nonce="{nonce}"></script>'
            + f'<script src="django_htmx/django-htmx.js" data-debug="True" defer nonce="{nonce}"></script>'
        )

    def test_version_2(self):
        result = Template("{% load django_htmx %}{% htmx_script version=2 %}").render(
            Context()
        )

        assert result == '<script src="django_htmx/htmx-2.min.js" defer></script>'

    def test_version_4(self):
        result = Template("{% load django_htmx %}{% htmx_script version=4 %}").render(
            Context()
        )

        assert result == '<script src="django_htmx/htmx-4.min.js" defer></script>'

    def test_version_4_unminified(self):
        result = Template(
            "{% load django_htmx %}{% htmx_script version=4 minified=False %}"
        ).render(Context())

        assert result == '<script src="django_htmx/htmx-4.js" defer></script>'

    def test_version_unsupported(self):
        template = Template("{% load django_htmx %}{% htmx_script version=3 %}")

        with pytest.raises(ValueError) as excinfo:
            template.render(Context())

        assert excinfo.value.args[0] == (
            "Unsupported htmx version 3, must be one of: 2, 4"
        )

    def test_extensions_one(self):
        result = Template(
            '{% load django_htmx %}{% htmx_script extensions="sse" %}'
        ).render(Context())

        assert result == (
            '<script src="django_htmx/htmx-2.min.js" defer></script>'
            + '<script src="django_htmx/ext/sse-2.min.js" defer></script>'
        )

    def test_extensions_multiple(self):
        result = Template(
            '{% load django_htmx %}{% htmx_script extensions="sse,ws" %}'
        ).render(Context())

        assert result == (
            '<script src="django_htmx/htmx-2.min.js" defer></script>'
            + '<script src="django_htmx/ext/sse-2.min.js" defer></script>'
            + '<script src="django_htmx/ext/ws-2.min.js" defer></script>'
        )

    def test_extensions_version_4(self):
        result = Template(
            '{% load django_htmx %}{% htmx_script version=4 extensions="head-support" %}'
        ).render(Context())

        assert result == (
            '<script src="django_htmx/htmx-4.min.js" defer></script>'
            + '<script src="django_htmx/ext/head-support-4.min.js" defer></script>'
        )

    def test_extensions_unminified(self):
        result = Template(
            '{% load django_htmx %}{% htmx_script minified=False extensions="preload" %}'
        ).render(Context())

        assert result == (
            '<script src="django_htmx/htmx-2.js" defer></script>'
            + '<script src="django_htmx/ext/preload-2.js" defer></script>'
        )

    def test_extensions_nonce(self):
        nonce = secrets.token_urlsafe(16)
        result = Template(
            '{% load django_htmx %}{% htmx_script extensions="sse" %}'
        ).render(Context({"csp_nonce": nonce}))

        assert result == (
            f'<script src="django_htmx/htmx-2.min.js" defer nonce="{nonce}"></script>'
            + f'<script src="django_htmx/ext/sse-2.min.js" defer nonce="{nonce}"></script>'
        )

    def test_extensions_unknown(self):
        template = Template(
            '{% load django_htmx %}{% htmx_script extensions="json-enc" %}'
        )

        with pytest.raises(ValueError) as excinfo:
            template.render(Context())

        assert excinfo.value.args[0] == (
            "Unknown htmx extension 'json-enc', must be one of: "
            + "head-support, preload, sse, ws"
        )

    def test_unminified(self):
        result = Template(
            "{% load django_htmx %}{% htmx_script minified=False %}"
        ).render(Context())

        assert result == '<script src="django_htmx/htmx-2.js" defer></script>'

    def test_unminified_nonce(self):
        nonce = secrets.token_urlsafe(16)
        result = Template(
            "{% load django_htmx %}{% htmx_script minified=False %}"
        ).render(Context({"csp_nonce": nonce}))

        assert (
            result
            == f'<script src="django_htmx/htmx-2.js" defer nonce="{nonce}"></script>'
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
