from django.http import HttpResponse

HTMX_STOP_POLLING = 286


class HttpResponseStopPolling(HttpResponse):
    status_code = HTMX_STOP_POLLING

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._reason_phrase = "Stop Polling"
