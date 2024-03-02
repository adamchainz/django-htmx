# -*- coding: utf-8 -*-
"""django-htmx related context processors.
"""
from __future__ import annotations
from django.http import HttpRequest
from typing import Dict


def base_template(request: HttpRequest) -> Dict[str, str]:
    """Sets the base_template context variable.

    If the request is a htmx request, the `base_template` will be set to a partial template.
    Otherwise, it will be set to a full template.
    Parameters:
    - request: Current request object.
    Returns:
    - dict: A dictionary with a key set to `base_template` and the appropriate value.
    """

    if request.htmx:
        base_template_var = "_partial.html"
    else:
        base_template_var = "_base.html"
    return dict(base_template=base_template_var)
