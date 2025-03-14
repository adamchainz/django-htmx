from __future__ import annotations

from django.conf import settings
from django.templatetags.static import static
from django.utils.html import format_html
from django.utils.safestring import SafeString
from django.utils.safestring import mark_safe


def htmx_script(*, minified: bool = True) -> SafeString:
    path = f"django_htmx/htmx{'.min' if minified else ''}.js"
    result = format_html(
        '<script src="{}" defer></script>',
        static(path),
    )
    if settings.DEBUG:
        result += django_htmx_script()
    return result


def django_htmx_script() -> SafeString:
    # Optimization: whilst the script has no behaviour outside of debug mode,
    # don't include it.
    if not settings.DEBUG:
        return mark_safe("")
    return format_html(
        '<script src="{}" data-debug="{}" defer></script>',
        static("django_htmx/django-htmx.js"),
        str(bool(settings.DEBUG)),
    )
