from typing import Any

from django.http import HttpResponse, HttpResponseRedirect

HTMX_STOP_POLLING = 286


class HttpResponseStopPolling(HttpResponse):
    status_code = HTMX_STOP_POLLING

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._reason_phrase = "Stop Polling"


class HXResponseRedirect(HttpResponseRedirect):
    status_code = 200

    def __init__(self, redirect_to: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(redirect_to, *args, **kwargs)

        self["HX-Redirect"] = self["Location"]
