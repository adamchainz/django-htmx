from __future__ import annotations

import asyncio
import json
from typing import Any
from typing import Awaitable
from typing import Callable
from urllib.parse import unquote
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

from django.http import HttpRequest
from django.http.response import HttpResponseBase
from django.utils.functional import cached_property


class HtmxMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(
        self,
        get_response: (
            Callable[[HttpRequest], HttpResponseBase]
            | Callable[[HttpRequest], Awaitable[HttpResponseBase]]
        ),
    ) -> None:
        self.get_response = get_response

        if asyncio.iscoroutinefunction(self.get_response):
            # Mark the class as async-capable, but do the actual switch
            # inside __call__ to avoid swapping out dunder methods
            self._is_coroutine = (
                asyncio.coroutines._is_coroutine  # type: ignore [attr-defined]
            )
        else:
            self._is_coroutine = None

    def __call__(
        self, request: HttpRequest
    ) -> HttpResponseBase | Awaitable[HttpResponseBase]:
        if self._is_coroutine:
            return self.__acall__(request)
        request.htmx = HtmxDetails(request)  # type: ignore [attr-defined]
        return self.get_response(request)

    async def __acall__(self, request: HttpRequest) -> HttpResponseBase:
        request.htmx = HtmxDetails(request)  # type: ignore [attr-defined]
        result = self.get_response(request)
        assert not isinstance(result, HttpResponseBase)  # type narrow
        return await result


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
    def current_url_abs_path(self) -> str | None:
        url = self.current_url
        if url is not None:
            split = urlsplit(url)
            if (
                split.scheme == self.request.scheme
                and split.netloc == self.request.get_host()
            ):
                url = urlunsplit(split._replace(scheme="", netloc=""))
            else:
                url = None
        return url

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
