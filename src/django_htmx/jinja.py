from __future__ import annotations

from django.conf import settings
from django.templatetags.static import static
from django.utils.html import format_html
from jinja2.environment import Environment
from jinja2.ext import Extension


def django_htmx_script() -> str:
    # Optimization: whilst the script has no behaviour outside of debug mode,
    # don't include it.
    if not settings.DEBUG:
        return ""
    return format_html(
        '<script src="{}" data-debug="{}" defer></script>',
        static("django-htmx.js"),
        str(bool(settings.DEBUG)),
    )


class DjangoHtmxExtension(Extension):
    def __init__(self, environment: Environment) -> None:
        super().__init__(environment)
        environment.globals["django_htmx_script"] = django_htmx_script
