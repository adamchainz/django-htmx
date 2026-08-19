from __future__ import annotations

from collections.abc import Awaitable, Callable

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.http import HttpRequest
from django.http.response import HttpResponseBase
from django.utils.functional import cached_property

from django_mcpz.headers import decode_header_value

PARAM_HEADER_PREFIX = "Mcp-Param-"


class MCPMiddleware:
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
        self.async_mode = iscoroutinefunction(self.get_response)

        if self.async_mode:
            # Mark the class as async-capable, but do the actual switch
            # inside __call__ to avoid swapping out dunder methods
            markcoroutinefunction(self)

    def __call__(
        self, request: HttpRequest
    ) -> HttpResponseBase | Awaitable[HttpResponseBase]:
        if self.async_mode:
            return self.__acall__(request)
        request.mcp = MCPDetails(request)  # type: ignore [attr-defined]
        return self.get_response(request)

    async def __acall__(self, request: HttpRequest) -> HttpResponseBase:
        request.mcp = MCPDetails(request)  # type: ignore [attr-defined]
        return await self.get_response(request)  # type: ignore [no-any-return, misc]


class MCPDetails:
    def __init__(self, request: HttpRequest) -> None:
        self.request = request

    def __bool__(self) -> bool:
        return "MCP-Protocol-Version" in self.request.headers

    @cached_property
    def protocol_version(self) -> str | None:
        return self.request.headers.get("MCP-Protocol-Version")

    @cached_property
    def method(self) -> str | None:
        return self.request.headers.get("Mcp-Method")

    @cached_property
    def name(self) -> str | None:
        value = self.request.headers.get("Mcp-Name")
        if value is not None:
            try:
                value = decode_header_value(value)
            except ValueError:
                value = None
        return value

    @cached_property
    def params(self) -> dict[str, str]:
        params = {}
        for header, value in self.request.headers.items():
            if header.startswith(PARAM_HEADER_PREFIX):
                try:
                    decoded = decode_header_value(value)
                except ValueError:
                    continue
                params[header[len(PARAM_HEADER_PREFIX) :]] = decoded
        return params

    def param(self, name: str) -> str | None:
        # WSGI reports header names with underscores converted to hyphens, so
        # normalize to match x-mcp-header names containing underscores too.
        wanted = name.lower().replace("_", "-")
        for key, value in self.params.items():
            if key.lower() == wanted:
                return value
        return None
