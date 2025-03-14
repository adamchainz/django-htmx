from __future__ import annotations

from django import template

from django_htmx.jinja import django_htmx_script
from django_htmx.jinja import htmx_script

register = template.Library()
register.simple_tag(htmx_script)
register.simple_tag(django_htmx_script)
