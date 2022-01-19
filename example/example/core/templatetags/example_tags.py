from __future__ import annotations

import json
from typing import Any

from django import template

register = template.Library()


@register.filter
def json_dumps(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)
