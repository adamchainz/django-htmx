from typing import Any

from django.http.response import HttpResponse, HttpResponseRedirectBase


class HXRedirectMixin:
    def dispatch(self, *args: Any, **kwds: Any) -> HttpResponse:
        response: HttpResponse = super().dispatch(*args, **kwds)  # type: ignore

        if not issubclass(response.__class__, HttpResponseRedirectBase):
            return response

        response.status_code = 200
        response["HX-Redirect"] = response["Location"]

        return response
