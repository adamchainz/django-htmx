from __future__ import annotations

from django.http import HttpResponse
from django.template.loader import render_to_string


def render_multiple(request, template_names, context):
    rendered_html = ""
    for template_name in template_names:
        rendered_html += render_to_string(
            template_name, context=context, request=request
        )

    return HttpResponse(rendered_html)
