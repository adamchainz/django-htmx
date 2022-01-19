from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import unquote

from django.http import HttpRequest, HttpResponse
from django.utils.functional import cached_property


class HtmxMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.htmx = HtmxDetails(request)
        return self.get_response(request)


class HtmxDetails:
    def __init__(self, request: HttpRequest) -> None:
        self.request = request

    def _get_header_value(self, name: str) -> str | None:
        value = self.request.headers.get(name) or None
        if value:
            if self.request.headers.get(f"{name}-URI-AutoEncoded") == "true":
                value = unquote(value)
        return value

    def __bool__(self) -> bool:
        return self._get_header_value("HX-Request") == "true"

    @cached_property
    def boosted(self) -> bool:
        return self._get_header_value("HX-Boosted") == "true"

    @cached_property
    def current_url(self) -> str | None:
        return self._get_header_value("HX-Current-URL")

    @cached_property
    def history_restore_request(self) -> bool:
        return self._get_header_value("HX-History-Restore-Request") == "true"

    @cached_property
    def prompt(self) -> str | None:
        return self._get_header_value("HX-Prompt")

    @cached_property
    def target(self) -> str | None:
        return self._get_header_value("HX-Target")

    @cached_property
    def trigger(self) -> str | None:
        return self._get_header_value("HX-Trigger")

    @cached_property
    def trigger_name(self) -> str | None:
        return self._get_header_value("HX-Trigger-Name")

    @cached_property
    def triggering_event(self) -> Any:
        value = self._get_header_value("Triggering-Event")
        if value is not None:
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = None
        return value
