from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.template.loader import render_to_string


def render_multiple(
    request: HttpRequest, template_names: list[str], context: dict[str, Any]
) -> HttpResponse:
    rendered_html = ""
    for template_name in template_names:
        rendered_html += render_to_string(
            template_name, context=context, request=request
        )

    return HttpResponse(rendered_html)
