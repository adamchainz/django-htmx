from __future__ import annotations

from typing import Any, cast

from django.core.handlers.wsgi import WSGIRequest
from django.http import HttpResponse
from django.http.response import HttpResponseBase
from django.test import RequestFactory as BaseRequestFactory
from django.test import SimpleTestCase
from django.test.client import MULTIPART_CONTENT

from django_mcpz.middleware import MCPDetails, MCPMiddleware


class MCPWSGIRequest(WSGIRequest):
    mcp: MCPDetails


class RequestFactory(BaseRequestFactory):
    def post(
        self,
        path: Any = "/",
        data: Any = None,
        content_type: str = MULTIPART_CONTENT,
        secure: bool = False,
        **extra: Any,
    ) -> MCPWSGIRequest:
        return cast(
            MCPWSGIRequest, super().post(path, data, content_type, secure, **extra)
        )


def dummy_view(request):
    return HttpResponse("Hello!")


async def async_dummy_view(request):
    return HttpResponse("Hello!")


class MCPMiddlewareTests(SimpleTestCase):
    request_factory = RequestFactory()
    middleware = MCPMiddleware(dummy_view)
    async_middleware = MCPMiddleware(async_dummy_view)

    def test_bool_default(self):
        request = self.request_factory.post()
        self.middleware(request)
        assert bool(request.mcp) is False

    def test_bool_true(self):
        request = self.request_factory.post(HTTP_MCP_PROTOCOL_VERSION="2026-07-28")
        self.middleware(request)
        assert bool(request.mcp) is True

    async def test_async(self):
        request = self.request_factory.post(HTTP_MCP_PROTOCOL_VERSION="2026-07-28")
        result = self.async_middleware(request)
        assert not isinstance(result, HttpResponseBase)  # type narrow
        await result
        assert bool(request.mcp) is True

    def test_protocol_version_default(self):
        request = self.request_factory.post()
        self.middleware(request)
        assert request.mcp.protocol_version is None

    def test_protocol_version_set(self):
        request = self.request_factory.post(HTTP_MCP_PROTOCOL_VERSION="2026-07-28")
        self.middleware(request)
        assert request.mcp.protocol_version == "2026-07-28"

    def test_method_default(self):
        request = self.request_factory.post()
        self.middleware(request)
        assert request.mcp.method is None

    def test_method_set(self):
        request = self.request_factory.post(HTTP_MCP_METHOD="tools/call")
        self.middleware(request)
        assert request.mcp.method == "tools/call"

    def test_name_default(self):
        request = self.request_factory.post()
        self.middleware(request)
        assert request.mcp.name is None

    def test_name_set(self):
        request = self.request_factory.post(HTTP_MCP_NAME="get_weather")
        self.middleware(request)
        assert request.mcp.name == "get_weather"

    def test_name_base64(self):
        request = self.request_factory.post(
            HTTP_MCP_NAME="=?base64?SGVsbG8sIOS4lueVjA==?="
        )
        self.middleware(request)
        assert request.mcp.name == "Hello, 世界"

    def test_name_malformed_base64(self):
        request = self.request_factory.post(HTTP_MCP_NAME="=?base64?!!!?=")
        self.middleware(request)
        assert request.mcp.name is None

    def test_params_default(self):
        request = self.request_factory.post()
        self.middleware(request)
        assert request.mcp.params == {}

    def test_params_set(self):
        request = self.request_factory.post(
            HTTP_MCP_PARAM_REGION="us-west1",
            HTTP_MCP_PARAM_TEXT="=?base64?IHBhZGRlZCA=?=",
        )
        self.middleware(request)
        assert request.mcp.params == {"Region": "us-west1", "Text": " padded "}

    def test_params_skips_malformed(self):
        request = self.request_factory.post(
            HTTP_MCP_PARAM_REGION="us-west1",
            HTTP_MCP_PARAM_BAD="=?base64?!!!?=",
        )
        self.middleware(request)
        assert request.mcp.params == {"Region": "us-west1"}

    def test_param_case_insensitive(self):
        request = self.request_factory.post(HTTP_MCP_PARAM_REGION="us-west1")
        self.middleware(request)
        assert request.mcp.param("region") == "us-west1"
        assert request.mcp.param("REGION") == "us-west1"

    def test_param_underscore_name(self):
        # WSGI reports the header Mcp-Param-My_Region as Mcp-Param-My-Region,
        # so lookups by the x-mcp-header name "My_Region" must still match.
        request = self.request_factory.post(HTTP_MCP_PARAM_MY_REGION="us-west1")
        self.middleware(request)
        assert request.mcp.param("My_Region") == "us-west1"

    def test_param_missing(self):
        request = self.request_factory.post()
        self.middleware(request)
        assert request.mcp.param("region") is None

    def test_param_no_match(self):
        request = self.request_factory.post(HTTP_MCP_PARAM_REGION="us-west1")
        self.middleware(request)
        assert request.mcp.param("shard") is None
