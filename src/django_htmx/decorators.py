from functools import wraps
from typing import Callable

from django.http import HttpRequest, HttpResponseNotFound


def htmx_view(view_func: Callable):
    @wraps(view_func)
    def wrap(request: HttpRequest, *args, **kwargs):
        if request.htmx:
            return view_func(request, *args, **kwargs)
        return HttpResponseNotFound()

    return wrap
