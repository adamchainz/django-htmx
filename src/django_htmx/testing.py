from __future__ import annotations

from typing import Any

from django.test.client import Client

_HTMX_HEADER_MAP = {
    "boosted": "HX-Boosted",
    "current_url": "HX-Current-URL",
    "history_restore_request": "HX-History-Restore-Request",
    "prompt": "HX-Prompt",
    "target": "HX-Target",
    "trigger": "HX-Trigger",
    "trigger_name": "HX-Trigger-Name",
}


class HtmxClientMixin:
    def generic(
        self,
        method: str,
        path: str,
        data: Any = "",
        content_type: str | None = "application/octet-stream",
        secure: bool = False,
        **extra: Any,
    ) -> Any:
        htmx = extra.pop("htmx", None)
        if htmx is not None:
            headers = extra.get("headers")
            if headers is None:
                headers = {}
                extra["headers"] = headers

            headers.setdefault("HX-Request", "true")

            if isinstance(htmx, dict):
                for key, value in htmx.items():
                    try:
                        header_name = _HTMX_HEADER_MAP[key]
                    except KeyError:
                        valid_keys = sorted(_HTMX_HEADER_MAP)
                        raise ValueError(
                            f"Unknown htmx kwarg {key!r}. Valid keys are: {valid_keys}."
                        ) from None
                    headers.setdefault(header_name, str(value))

        return super().generic(  # type: ignore [misc]
            method=method,
            path=path,
            data=data,
            content_type=content_type,
            secure=secure,
            **extra,
        )


class HtmxClient(HtmxClientMixin, Client):
    pass
