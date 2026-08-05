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

# Extension names mapped to the htmx versions they’re available for.
EXTENSIONS = {
    "htmx-2-compat": frozenset({4}),
    "hx-browser-indicator": frozenset({4}),
    "hx-download": frozenset({4}),
    "hx-head": frozenset({2, 4}),
    "hx-optimistic": frozenset({4}),
    "hx-preload": frozenset({2, 4}),
    "hx-prompt": frozenset({4}),
    "hx-ptag": frozenset({4}),
    "hx-sse": frozenset({2, 4}),
    "hx-targets": frozenset({4}),
    "hx-upsert": frozenset({4}),
    "hx-ws": frozenset({2, 4}),
}


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
    htmax = "htmax" in extension_names
    if htmax:
        if version != 4:
            raise ValueError("htmax is only available for htmx version 4")
        if len(extension_names) > 1:
            raise ValueError(
                "htmax already bundles extensions, so it cannot be combined "
                + "with other extension names."
            )
    else:
        for name in extension_names:
            if name not in EXTENSIONS:
                raise ValueError(
                    f"Unknown htmx extension {name!r}, must be one of: "
                    + ", ".join(sorted([*EXTENSIONS, "htmax"]))
                )
            if version not in EXTENSIONS[name]:
                raise ValueError(
                    f"htmx extension {name!r} is not available for htmx "
                    + f"version {version}"
                )
    suffix = ".min" if minified else ""
    if htmax:
        result = _script_tag(f"django_htmx/htmax-4{suffix}.js", nonce)
    else:
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
