from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio.to_thread
import httpx2
from django.core.handlers.wsgi import WSGIHandler
from django.test import SimpleTestCase
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client


class WSGITransport(httpx2.AsyncBaseTransport):
    """Serve httpx requests with Django's WSGI application, in a thread."""

    def __init__(self) -> None:
        self._transport = httpx2.WSGITransport(app=WSGIHandler())

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        return await anyio.to_thread.run_sync(self._handle, request)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        response = self._transport.handle_request(request)
        try:
            response.read()
        finally:
            response.close()
        headers = [
            (name, value)
            for name, value in response.headers.items()
            if name.lower() not in ("content-length", "transfer-encoding")
        ]
        return httpx2.Response(
            status_code=response.status_code,
            headers=headers,
            content=response.content,
        )


@asynccontextmanager
async def mcp_client(
    path: str = "/mcp", headers: dict[str, str] | None = None
) -> AsyncGenerator[Client]:
    async with httpx2.AsyncClient(
        transport=WSGITransport(), headers=headers
    ) as http_client:
        transport = streamable_http_client(
            f"http://testserver{path}", http_client=http_client
        )
        async with Client(transport) as client:
            yield client


class MCPClientTests(SimpleTestCase):
    """
    Test the endpoint with the official MCP Python SDK client, over its
    Streamable HTTP transport, routed to the Django WSGI application.
    """

    def test_list_tools(self):
        async def run():
            async with mcp_client() as client:
                return await client.list_tools()

        result = asyncio.run(run())

        names = [tool.name for tool in result.tools]
        assert names == [
            "add",
            "greet",
            "unavailable",
            "crash",
            "noop",
            "unencodable",
            "regional",
            "multiply",
            "add_typed",
            "segment_length",
        ]
        add = result.tools[0]
        assert add.description == "Add two integers."
        assert add.input_schema["required"] == ["a", "b"]
        assert add.output_schema is not None
        assert add.output_schema["required"] == ["sum"]

    def test_call_tool_structured(self):
        async def run():
            async with mcp_client() as client:
                return await client.call_tool("add", {"a": 20, "b": 22})

        result = asyncio.run(run())

        assert result.is_error is False
        assert result.structured_content == {"sum": 42}

    def test_call_tool_typed_schema(self):
        async def run():
            async with mcp_client() as client:
                return await client.call_tool("multiply", {"a": 6, "b": 7})

        result = asyncio.run(run())

        assert result.is_error is False
        assert result.structured_content == {"product": 42}

    def test_call_tool_text(self):
        async def run():
            async with mcp_client() as client:
                return await client.call_tool("greet", {"name": "Alice"})

        result = asyncio.run(run())

        assert result.is_error is False
        assert result.content[0].text == "Hello, Alice!"

    def test_call_tool_error(self):
        async def run():
            async with mcp_client() as client:
                return await client.call_tool("unavailable")

        result = asyncio.run(run())

        assert result.is_error is True
        assert (
            result.content[0].text
            == "The flux capacitor is offline. Try the DeLorean instead."
        )

    def test_auth(self):
        async def run():
            async with mcp_client(
                "/secure-mcp", headers={"Authorization": "Bearer test-token"}
            ) as client:
                return await client.call_tool("secret_word")

        result = asyncio.run(run())

        assert result.content[0].text == "xyzzy"
