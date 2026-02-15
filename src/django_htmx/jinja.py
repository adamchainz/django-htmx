from __future__ import annotations

from django.conf import settings
from django.templatetags.static import static
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe

# https://htmx.org/extensions/#core-extensions
HTMX_EXT_NAMES = {
    "head-support",
    "htmx-1-compat",
    # "idiomorph",
    "preload",
    "response-targets",
    "sse",
    "ws",
}


def htmx_script(
    *, minified: bool = True, nonce: str | None = None, ext: str = ""
) -> SafeString:
    path = f"django_htmx/htmx{'.min' if minified else ''}.js"
    if nonce is not None:
        result = format_html(
            '<script src="{}" defer nonce="{}"></script>',
            static(path),
            nonce,
        )
    else:
        result = format_html(
            '<script src="{}" defer></script>',
            static(path),
        )

    if nonce:
        nonce_str = f' nonce="{nonce}"'
    else:
        nonce_str = ""

    for ext_name in ext.split(","):
        if not ext_name:
            continue

        if ext_name not in HTMX_EXT_NAMES:
            raise ValueError(f"Unknown HTMX extension: [{ext_name}]")

        result += format_html(
            '<script src="{}" defer{}></script>',
            static(f"django_htmx/htmx-ext-{ext_name}.js"),
            nonce_str,
        )

    if settings.DEBUG:
        result += django_htmx_script(nonce=nonce)
    return result


def django_htmx_script(*, nonce: str | None = None) -> SafeString:
    # Optimization: whilst the script has no behaviour outside of debug mode,
    # don't include it.
    if not settings.DEBUG:
        return mark_safe("")
    if nonce is not None:
        return format_html(
            '<script src="{}" data-debug="{}" defer nonce="{}"></script>',
            static("django_htmx/django-htmx.js"),
            str(bool(settings.DEBUG)),
            nonce,
        )
    else:
        return format_html(
            '<script src="{}" data-debug="{}" defer></script>',
            static("django_htmx/django-htmx.js"),
            str(bool(settings.DEBUG)),
        )
