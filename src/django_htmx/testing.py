from __future__ import annotations

from typing import Any

from django.test.client import Client

_HTMX_HEADER_MAP = {
    "boosted": "HTTP_HX_BOOSTED",
    "current_url": "HTTP_HX_CURRENT_URL",
    "history_restore_request": "HTTP_HX_HISTORY_RESTORE_REQUEST",
    "prompt": "HTTP_HX_PROMPT",
    "target": "HTTP_HX_TARGET",
    "trigger": "HTTP_HX_TRIGGER",
    "trigger_name": "HTTP_HX_TRIGGER_NAME",
}


def _build_htmx_headers(htmx: bool | dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {"HTTP_HX_REQUEST": "true"}

    if isinstance(htmx, dict):
        for key, value in htmx.items():
            environ_key = _HTMX_HEADER_MAP.get(key)
            if environ_key is None:
                raise ValueError(
                    f"Unknown htmx kwarg {key!r}. "
                    f"Valid keys are: {sorted(_HTMX_HEADER_MAP)}."
                )
            headers[environ_key] = str(value)

    return headers


class HtmxClientMixin:
    def request(self, **request: Any) -> Any:
        htmx = request.pop("htmx", None)
        if htmx is not None:
            for key, value in _build_htmx_headers(htmx).items():
                request.setdefault(key, value)
        return super().request(**request)  # type: ignore [misc]


class HtmxClient(HtmxClientMixin, Client):
    pass
