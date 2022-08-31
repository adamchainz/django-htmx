from __future__ import annotations

import json
import sys
from typing import Any, TypeVar

from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse
from django.http.response import HttpResponseBase, HttpResponseRedirectBase

if sys.version_info >= (3, 8):
    from typing import Literal

    EventAfterType = Literal["receive", "settle", "swap"]
else:
    EventAfterType = str


HTMX_STOP_POLLING = 286


class HttpResponseStopPolling(HttpResponse):
    status_code = HTMX_STOP_POLLING

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._reason_phrase = "Stop Polling"


class HttpResponseClientRedirect(HttpResponseRedirectBase):
    status_code = 200

    def __init__(self, redirect_to: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(redirect_to, *args, **kwargs)
        self["HX-Redirect"] = self["Location"]
        del self["Location"]

    @property
    def url(self) -> str:
        return self["HX-Redirect"]


class HttpResponseClientRefresh(HttpResponse):
    def __init__(self) -> None:
        super().__init__()
        self["HX-Refresh"] = "true"


_R = TypeVar("_R", bound=HttpResponseBase)


def trigger_client_event(
    response: _R,
    name: str,
    params: dict[str, Any],
    *,
    after: EventAfterType = "receive",
) -> _R:
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

    # django-stubs missing method
    # https://github.com/typeddjango/django-stubs/pull/1099
    if header in response:  # type: ignore [operator]
        value = response[header]
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{header!r} value should be valid JSON.") from exc
        data[name] = params
    else:
        data = {name: params}

    response[header] = json.dumps(data, cls=DjangoJSONEncoder)

    return response
