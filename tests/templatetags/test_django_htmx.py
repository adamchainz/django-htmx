from django.template import Context, Template
from django.test import SimpleTestCase, override_settings


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
            '<script type="text/javascript" src="django-htmx.js" '
            + 'data-debug="True" async defer></script>'
        )
