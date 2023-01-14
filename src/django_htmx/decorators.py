from functools import wraps

from django.http import HttpResponseNotFound


def htmx_view(view_func):
    @wraps(view_func)
    def wrap(request, *args, **kwargs):
        if request.htmx:
            return view_func(request, *args, **kwargs)
        return HttpResponseNotFound()

    return wrap
