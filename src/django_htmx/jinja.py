from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import django
from django.conf import settings
from django.templatetags.static import static
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe

if TYPE_CHECKING or django.VERSION >= (6, 0):
    from django.utils.csp import LazyNonce
else:
    LazyNonce = None

EXTENSIONS = frozenset(
    {
        "head-support",
        "preload",
        "sse",
        "ws",
    }
)


def htmx_script(
    *,
    version: int = 2,
    minified: bool = True,
    extensions: str | Sequence[str] = (),
    nonce: LazyNonce | str | None = None,
) -> SafeString:
    if version not in (2, 4):
        raise ValueError(f"Unsupported htmx version {version!r}, must be one of: 2, 4")
    if isinstance(extensions, str):
        extension_names = [e.strip() for e in extensions.split(",") if e.strip()]
    else:
        extension_names = list(extensions)
    for name in extension_names:
        if name not in EXTENSIONS:
            raise ValueError(
                f"Unknown htmx extension {name!r}, must be one of: "
                + ", ".join(sorted(EXTENSIONS))
            )
    suffix = ".min" if minified else ""
    result = _script_tag(f"django_htmx/htmx-{version}{suffix}.js", nonce)
    for name in extension_names:
        result += _script_tag(f"django_htmx/ext/{name}-{version}{suffix}.js", nonce)
    if settings.DEBUG:
        result += django_htmx_script(nonce=nonce)
    return result


def _script_tag(path: str, nonce: LazyNonce | str | None) -> SafeString:
    if nonce is not None:
        return format_html(
            '<script src="{}" defer nonce="{}"></script>',
            static(path),
            nonce,
        )
    else:
        return format_html(
            '<script src="{}" defer></script>',
            static(path),
        )


def django_htmx_script(*, nonce: LazyNonce | str | None = None) -> SafeString:
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
