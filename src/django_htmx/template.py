from django.template.loader import render_to_string
from django.http import HttpResponse

def render_multiple(request, template_names, context):
    rendered_html = ""
    for template_name in template_names:
        rendered_html += render_to_string(
            template_name,
            context=context,
            request=request
        )

    return HttpResponse(rendered_html)
