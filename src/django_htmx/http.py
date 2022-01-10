import json
import sys
from typing import Any, Dict

from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse
from django.http.response import HttpResponseRedirectBase

if sys.version_info >= (3, 8):
    from typing import Literal

    EventAfterType = Literal["receive", "settle", "swap"]
else:
    EventAfterType = str

HTMX_STOP_POLLING = 286


class HttpResponseStopPolling(HttpResponse):
    status_code = HTMX_STOP_POLLING

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._reason_phrase = "Stop Polling"


class HttpResponseClientRedirect(HttpResponseRedirectBase):
    status_code = 200

    def __init__(self, redirect_to: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(redirect_to, *args, **kwargs)
        self["HX-Redirect"] = self["Location"]
        del self["Location"]


def trigger_client_event(
    response: HttpResponse,
    name: str,
    params: Dict[str, Any],
    *,
    after: EventAfterType = "receive",
) -> None:
    if after == "receive":
        header = "HX-Trigger"
    elif after == "settle":
        header = "HX-Trigger-After-Settle"
    elif after == "swap":
        header = "HX-Trigger-After-Swap"
    else:
        raise ValueError(
            "Value for 'after' must be one of: 'receive', 'settle', or 'swap'."
        )

    if header in response:
        value = response[header]
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{header!r} value should be valid JSON.") from exc
        data[name] = params
    else:
        data = {name: params}

    response[header] = json.dumps(data, cls=DjangoJSONEncoder)
