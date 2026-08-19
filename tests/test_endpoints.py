from __future__ import annotations

from typing import Annotated, Any, TypedDict

import msgspec
import msgspec.json
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from django_mcpz.endpoints import (
    HEADER_MISMATCH,
    PROTOCOL_VERSION,
    UNSUPPORTED_PROTOCOL_VERSION,
    MCPEndpoint,
)
from django_mcpz.headers import encode_header_value
from django_mcpz.jsonrpc import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
)

SERVER_INFO = {"name": "example-server", "version": "1.2.3", "title": "Example Server"}


# Module level, since msgspec resolves deferred annotations in module scope.


class Node(msgspec.Struct):
    name: str
    children: list[Node] = []


class Inner(msgspec.Struct):
    region: Annotated[str, msgspec.Meta(extra_json_schema={"x-mcp-header": "Region"})]


class Outer(msgspec.Struct):
    inner: Inner


def make_message(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    id: Any = 1,
    protocol_version: str | None = PROTOCOL_VERSION,
    client_capabilities: Any = ...,
) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if protocol_version is not None:
        meta["io.modelcontextprotocol/protocolVersion"] = protocol_version
    if client_capabilities is ...:
        client_capabilities = {}
    if client_capabilities is not None:
        meta["io.modelcontextprotocol/clientCapabilities"] = client_capabilities
    message: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": method,
        "params": {**(params or {}), "_meta": meta},
    }
    if id is not None:
        message["id"] = id
    return message


class EndpointTestCase(SimpleTestCase):
    url = "/mcp"

    def post(
        self, message: dict[str, Any], headers: dict[str, str | None] | None = None
    ) -> Any:
        final_headers: dict[str, str | None] = {
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        method = message.get("method")
        if isinstance(method, str):
            final_headers["Mcp-Method"] = method
        params = message.get("params")
        if isinstance(params, dict) and isinstance(params.get("name"), str):
            final_headers["Mcp-Name"] = encode_header_value(params["name"])
        if headers:
            final_headers.update(headers)
        return self.client.post(
            self.url,
            data=msgspec.json.encode(message),
            content_type="application/json",
            headers={
                name: value
                for name, value in final_headers.items()
                if value is not None
            },
        )

    def assert_error(self, response, code, *, id=1, status=200):
        assert response.status_code == status
        data = response.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == id
        assert data["error"]["code"] == code
        return data["error"]

    def assert_result(self, response, *, id=1):
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "application/json"
        data = response.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == id
        result = data["result"]
        assert result["resultType"] == "complete"
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"] == SERVER_INFO
        return result


class TransportTests(EndpointTestCase):
    def test_get_not_allowed(self):
        response = self.client.get(self.url)

        assert response.status_code == 405
        assert response.headers["Allow"] == "POST"

    def test_delete_not_allowed(self):
        response = self.client.delete(self.url)

        assert response.status_code == 405

    def test_origin_disallowed(self):
        response = self.post(
            make_message("tools/list"),
            headers={"Origin": "https://evil.example.com"},
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == INVALID_REQUEST

    def test_origin_unparsable(self):
        response = self.post(make_message("tools/list"), headers={"Origin": "null"})

        assert response.status_code == 403

    def test_origin_invalid_ipv6(self):
        response = self.post(
            make_message("tools/list"), headers={"Origin": "https://[::1"}
        )

        assert response.status_code == 403

    def test_origin_allowed(self):
        response = self.post(
            make_message("tools/list"),
            headers={"Origin": "http://testserver"},
        )

        assert response.status_code == 200

    @override_settings(DEBUG=True, ALLOWED_HOSTS=[])
    def test_origin_allowed_debug_localhost(self):
        response = self.post(
            make_message("tools/list"),
            headers={"Origin": "http://localhost:8000"},
        )

        assert response.status_code == 200

    def test_invalid_json(self):
        response = self.client.post(
            self.url, data=b"{not json", content_type="application/json"
        )

        self.assert_error(response, PARSE_ERROR, id=None, status=400)

    def test_not_a_json_object(self):
        response = self.client.post(
            self.url, data=b"[1, 2]", content_type="application/json"
        )

        self.assert_error(response, INVALID_REQUEST, id=None, status=400)

    def test_wrong_jsonrpc_version(self):
        response = self.client.post(
            self.url,
            data=b'{"jsonrpc": "1.0", "id": 1, "method": "tools/list"}',
            content_type="application/json",
        )

        # The id was readable, so it is echoed in the error response.
        self.assert_error(response, INVALID_REQUEST, status=400)

    def test_missing_method(self):
        response = self.post({"jsonrpc": "2.0", "id": 1})

        self.assert_error(response, INVALID_REQUEST, status=400)

    def test_missing_params(self):
        response = self.post({"jsonrpc": "2.0", "id": 1, "method": "server/discover"})

        self.assert_error(response, INVALID_PARAMS, status=400)

    def test_notification_accepted(self):
        message = make_message("notifications/whatever", id=None)

        response = self.post(message)

        assert response.status_code == 202
        assert response.content == b""

    def test_notification_skips_header_checks(self):
        message = make_message("notifications/whatever", id=None)

        response = self.post(
            message,
            headers={"MCP-Protocol-Version": None, "Mcp-Method": None},
        )

        assert response.status_code == 202

    def test_null_id_invalid(self):
        message = make_message("tools/list")
        message["id"] = None

        response = self.post(message)

        self.assert_error(response, INVALID_REQUEST, id=None, status=400)

    def test_boolean_id_invalid(self):
        response = self.post(make_message("tools/list", id=True))

        self.assert_error(response, INVALID_REQUEST, id=None, status=400)

    def test_params_not_object(self):
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]}

        response = self.post(message)

        self.assert_error(response, INVALID_REQUEST, status=400)

    def test_string_id_allowed(self):
        response = self.post(make_message("tools/list", id="abc"))

        self.assert_result(response, id="abc")


class MetadataValidationTests(EndpointTestCase):
    def test_missing_protocol_version_header(self):
        response = self.post(
            make_message("tools/list"),
            headers={"MCP-Protocol-Version": None},
        )

        error = self.assert_error(response, HEADER_MISMATCH, status=400)
        assert "MCP-Protocol-Version" in error["message"]

    def test_unsupported_protocol_version(self):
        response = self.post(
            make_message("tools/list", protocol_version="2025-11-25"),
            headers={"MCP-Protocol-Version": "2025-11-25"},
        )

        error = self.assert_error(response, UNSUPPORTED_PROTOCOL_VERSION, status=400)
        assert error["data"] == {
            "supported": [PROTOCOL_VERSION],
            "requested": "2025-11-25",
        }

    def test_missing_meta_protocol_version(self):
        response = self.post(make_message("tools/list", protocol_version=None))

        error = self.assert_error(response, INVALID_PARAMS, status=400)
        assert "protocolVersion" in error["message"]

    def test_missing_meta_entirely(self):
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

        response = self.post(message)

        self.assert_error(response, INVALID_PARAMS, status=400)

    def test_missing_client_capabilities(self):
        response = self.post(make_message("tools/list", client_capabilities=None))

        error = self.assert_error(response, INVALID_PARAMS, status=400)
        assert "clientCapabilities" in error["message"]

    def test_protocol_version_header_body_mismatch(self):
        response = self.post(make_message("tools/list", protocol_version="1900-01-01"))

        error = self.assert_error(response, HEADER_MISMATCH, status=400)
        assert "does not match body value" in error["message"]

    def test_missing_method_header(self):
        response = self.post(make_message("tools/list"), headers={"Mcp-Method": None})

        error = self.assert_error(response, HEADER_MISMATCH, status=400)
        assert "Mcp-Method" in error["message"]

    def test_method_header_mismatch(self):
        response = self.post(
            make_message("tools/list"), headers={"Mcp-Method": "tools/call"}
        )

        self.assert_error(response, HEADER_MISMATCH, status=400)


class DispatchTests(EndpointTestCase):
    def test_unknown_method(self):
        response = self.post(make_message("resources/list"))

        error = self.assert_error(response, METHOD_NOT_FOUND, status=404)
        assert "resources/list" in error["message"]

    def test_initialize_names_supported_versions(self):
        response = self.post(make_message("initialize"))

        error = self.assert_error(response, METHOD_NOT_FOUND, status=404)
        assert PROTOCOL_VERSION in error["message"]


class DiscoverTests(EndpointTestCase):
    def test_success(self):
        response = self.post(make_message("server/discover"))

        result = self.assert_result(response)
        assert result["supportedVersions"] == [PROTOCOL_VERSION]
        assert result["capabilities"] == {"tools": {}}
        assert result["instructions"] == (
            "Example MCP server used in the django-mcpz test suite."
        )
        assert result["ttlMs"] == 300_000
        assert result["cacheScope"] == "public"

    def test_defaults(self):
        response = self.client.post(
            "/secure-mcp",
            data=msgspec.json.encode(make_message("server/discover")),
            content_type="application/json",
            headers={
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Mcp-Method": "server/discover",
                "Authorization": "Bearer test-token",
            },
        )

        result = response.json()["result"]
        assert "instructions" not in result
        assert result["ttlMs"] == 0
        assert result["cacheScope"] == "private"


class ToolsListTests(EndpointTestCase):
    def test_success(self):
        response = self.post(make_message("tools/list"))

        result = self.assert_result(response)
        assert result["ttlMs"] == 300_000
        assert result["cacheScope"] == "public"
        assert "nextCursor" not in result
        names = [tool["name"] for tool in result["tools"]]
        # Deterministic (registration) order.
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

        add = result["tools"][0]
        assert add["description"] == "Add two integers."
        assert add["inputSchema"]["required"] == ["a", "b"]
        assert add["outputSchema"]["required"] == ["sum"]

        greet = result["tools"][1]
        assert greet["title"] == "Greeter"
        assert greet["annotations"] == {"readOnlyHint": True}
        assert "outputSchema" not in greet

        regional = result["tools"][6]
        assert regional["icons"] == [
            {"src": "https://example.com/regional.png", "mimeType": "image/png"}
        ]

    def test_invalid_cursor(self):
        response = self.post(make_message("tools/list", {"cursor": "opaque"}))

        self.assert_error(response, INVALID_PARAMS)


class ToolsCallTests(EndpointTestCase):
    def call(
        self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        params: dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        return self.post(make_message("tools/call", params), **kwargs)

    def test_structured_result(self):
        response = self.call("add", {"a": 20, "b": 22})

        result = self.assert_result(response)
        assert result["isError"] is False
        assert result["structuredContent"] == {"sum": 42}
        assert result["content"] == [{"type": "text", "text": '{"sum":42}'}]

    def test_text_result(self):
        response = self.call("greet", {"name": "Alice"})

        result = self.assert_result(response)
        assert result["isError"] is False
        assert result["content"] == [{"type": "text", "text": "Hello, Alice!"}]
        assert "structuredContent" not in result

    def test_no_arguments(self):
        response = self.call("greet")

        result = self.assert_result(response)
        assert result["content"] == [{"type": "text", "text": "Hello, world!"}]

    def test_none_result(self):
        response = self.call("noop")

        result = self.assert_result(response)
        assert result["isError"] is False
        assert result["content"] == []
        assert "structuredContent" not in result

    def test_tool_error(self):
        response = self.call("unavailable")

        result = self.assert_result(response)
        assert result["isError"] is True
        assert result["content"] == [
            {
                "type": "text",
                "text": "The flux capacitor is offline. Try the DeLorean instead.",
            }
        ]

    def test_unexpected_exception(self):
        with self.assertLogs("django_mcpz", level="ERROR"):
            response = self.call("crash")

        result = self.assert_result(response)
        assert result["isError"] is True
        # Internal details are not leaked.
        assert result["content"] == [
            {"type": "text", "text": "Tool 'crash' failed unexpectedly."}
        ]

    def test_unencodable_result(self):
        # A return value msgspec cannot encode is reported in-band too, not
        # as a 500 error.
        with self.assertLogs("django_mcpz", level="ERROR"):
            response = self.call("unencodable")

        result = self.assert_result(response)
        assert result["isError"] is True
        assert result["content"] == [
            {"type": "text", "text": "Tool 'unencodable' failed unexpectedly."}
        ]

    def test_unknown_tool(self):
        response = self.call("does_not_exist")

        error = self.assert_error(response, INVALID_PARAMS)
        assert error["message"] == "Unknown tool: 'does_not_exist'"

    def test_missing_name(self):
        response = self.post(make_message("tools/call", {"arguments": {}}))

        self.assert_error(response, INVALID_PARAMS)

    def test_arguments_not_object(self):
        message = make_message("tools/call", {"name": "add", "arguments": [1]})

        response = self.post(message)

        self.assert_error(response, INVALID_PARAMS)

    def test_missing_name_header(self):
        response = self.call("add", {"a": 1, "b": 2}, headers={"Mcp-Name": None})

        error = self.assert_error(response, HEADER_MISMATCH, status=400)
        assert "Mcp-Name" in error["message"]

    def test_name_header_mismatch(self):
        response = self.call("add", {"a": 1, "b": 2}, headers={"Mcp-Name": "greet"})

        self.assert_error(response, HEADER_MISMATCH, status=400)

    def test_name_header_base64(self):
        response = self.call(
            "add",
            {"a": 1, "b": 2},
            # "add" encoded with the Base64 sentinel format.
            headers={"Mcp-Name": "=?base64?YWRk?="},
        )

        result = self.assert_result(response)
        assert result["structuredContent"] == {"sum": 3}

    def test_name_header_malformed_base64(self):
        response = self.call(
            "add", {"a": 1, "b": 2}, headers={"Mcp-Name": "=?base64?!!!?="}
        )

        self.assert_error(response, HEADER_MISMATCH, status=400)


class TypedSchemaTests(EndpointTestCase):
    def call(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        return self.post(
            make_message("tools/call", {"name": name, "arguments": arguments}),
            **kwargs,
        )

    def test_generated_schemas_in_list(self):
        response = self.post(make_message("tools/list"))

        tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
        multiply = tools["multiply"]
        assert multiply["inputSchema"] == {
            "title": "MultiplyParams",
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "The first factor."},
                "b": {"type": "integer", "default": 2},
            },
            "required": ["a"],
            "additionalProperties": False,
        }
        assert multiply["outputSchema"] == {
            "title": "MultiplyResult",
            "type": "object",
            "properties": {"product": {"type": "integer"}},
            "required": ["product"],
        }

    def test_nested_struct_schema_keeps_defs(self):
        response = self.post(make_message("tools/list"))

        tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
        schema = tools["segment_length"]["inputSchema"]
        assert schema["properties"]["start"] == {"$ref": "#/$defs/Point"}
        assert schema["$defs"]["Point"]["required"] == ["x", "y"]

    def test_call(self):
        response = self.call("multiply", {"a": 6, "b": 7})

        result = self.assert_result(response)
        assert result["isError"] is False
        assert result["structuredContent"] == {"product": 42}
        assert result["content"] == [{"type": "text", "text": '{"product":42}'}]

    def test_call_default_applied(self):
        response = self.call("multiply", {"a": 5})

        result = self.assert_result(response)
        assert result["structuredContent"] == {"product": 10}

    def test_invalid_arguments_wrong_type(self):
        response = self.call("multiply", {"a": "six"})

        result = self.assert_result(response)
        assert result["isError"] is True
        assert result["content"] == [
            {
                "type": "text",
                "text": "Invalid arguments: Expected `int`, got `str` - at `$.a`",
            }
        ]

    def test_invalid_arguments_missing_field(self):
        response = self.call("multiply", {})

        result = self.assert_result(response)
        assert result["isError"] is True
        assert "missing required field `a`" in result["content"][0]["text"]

    def test_invalid_arguments_unknown_field(self):
        response = self.call("multiply", {"a": 1, "c": 2})

        result = self.assert_result(response)
        assert result["isError"] is True
        assert result["content"] == [
            {"type": "text", "text": "Invalid arguments: unknown field `c`"}
        ]

    def test_invalid_arguments_unknown_fields(self):
        response = self.call("multiply", {"a": 1, "c": 2, "d": 3})

        result = self.assert_result(response)
        assert result["isError"] is True
        assert result["content"] == [
            {"type": "text", "text": "Invalid arguments: unknown fields `c`, `d`"}
        ]

    def test_forbid_unknown_fields_call(self):
        response = self.call("add_typed", {"a": 20, "b": 22})

        result = self.assert_result(response)
        assert result["structuredContent"] == {"sum": 42}

    def test_forbid_unknown_fields_still_enforced(self):
        # A Struct that itself forbids unknown fields: msgspec enforces it,
        # with its own message.
        response = self.call("add_typed", {"a": 1, "b": 2, "c": 3})

        result = self.assert_result(response)
        assert result["isError"] is True
        assert result["content"] == [
            {
                "type": "text",
                "text": "Invalid arguments: Object contains unknown field `c`",
            }
        ]

    def test_nested_struct_call(self):
        response = self.call(
            "segment_length",
            {"start": {"x": 0, "y": 0}, "end": {"x": 3, "y": 4}},
        )

        result = self.assert_result(response)
        assert result["structuredContent"] == {"label": "", "length": 7}

    def test_header_annotation_from_meta(self):
        # The x-mcp-header annotation via msgspec.Meta extra_json_schema is
        # enforced like any other.
        arguments = {
            "start": {"x": 0, "y": 0},
            "end": {"x": 1, "y": 1},
            "label": "diag",
        }

        response = self.call("segment_length", arguments)
        self.assert_error(response, HEADER_MISMATCH, status=400)

        response = self.call(
            "segment_length", arguments, headers={"Mcp-Param-Label": "diag"}
        )
        result = self.assert_result(response)
        assert result["structuredContent"] == {"label": "diag", "length": 2}


class ParamHeaderTests(EndpointTestCase):
    def call_regional(
        self, arguments: dict[str, Any], headers: dict[str, str | None]
    ) -> Any:
        return self.post(
            make_message("tools/call", {"name": "regional", "arguments": arguments}),
            headers=headers,
        )

    def test_matching_headers(self):
        response = self.call_regional(
            {"region": "us-west1", "shard": 3, "fast": True},
            {
                "Mcp-Param-Region": "us-west1",
                "Mcp-Param-Shard": "3",
                "Mcp-Param-Fast": "true",
            },
        )

        result = self.assert_result(response)
        assert result["structuredContent"] == {"region": "us-west1"}

    def test_omitted_arguments_omitted_headers(self):
        response = self.call_regional({}, {})

        self.assert_result(response)

    def test_header_names_case_insensitive(self):
        response = self.call_regional(
            {"region": "us-west1"}, {"MCP-PARAM-REGION": "us-west1"}
        )

        self.assert_result(response)

    def test_base64_encoded_value(self):
        response = self.call_regional(
            {"region": "Hello, 世界"},
            {"Mcp-Param-Region": encode_header_value("Hello, 世界")},
        )

        result = self.assert_result(response)
        assert result["structuredContent"] == {"region": "Hello, 世界"}

    def test_integer_numeric_comparison(self):
        response = self.call_regional(
            {"region": "r", "shard": 42},
            {"Mcp-Param-Region": "r", "Mcp-Param-Shard": "42.0"},
        )

        self.assert_result(response)

    def test_integer_above_float_precision_match(self):
        # 2**53 + 1 is not representable as a float, so float comparison
        # would wrongly reject this conforming request.
        response = self.call_regional(
            {"region": "r", "shard": 9007199254740993},
            {"Mcp-Param-Region": "r", "Mcp-Param-Shard": "9007199254740993"},
        )

        self.assert_result(response)

    def test_integer_above_float_precision_mismatch(self):
        # …and float comparison would wrongly accept this mismatch.
        response = self.call_regional(
            {"region": "r", "shard": 9007199254740992},
            {"Mcp-Param-Region": "r", "Mcp-Param-Shard": "9007199254740993"},
        )

        self.assert_error(response, HEADER_MISMATCH, status=400)

    def test_integer_rejects_exponent_notation(self):
        response = self.call_regional(
            {"region": "r", "shard": 100},
            {"Mcp-Param-Region": "r", "Mcp-Param-Shard": "1e2"},
        )

        self.assert_error(response, HEADER_MISMATCH, status=400)

    def test_integer_rejects_underscore_separators(self):
        response = self.call_regional(
            {"region": "r", "shard": 42},
            {"Mcp-Param-Region": "r", "Mcp-Param-Shard": "4_2"},
        )

        self.assert_error(response, HEADER_MISMATCH, status=400)

    def test_integer_rejects_fractional_decimal(self):
        response = self.call_regional(
            {"region": "r", "shard": 42},
            {"Mcp-Param-Region": "r", "Mcp-Param-Shard": "42.5"},
        )

        self.assert_error(response, HEADER_MISMATCH, status=400)

    def test_missing_header_with_body_value(self):
        response = self.call_regional({"region": "us-west1"}, {})

        error = self.assert_error(response, HEADER_MISMATCH, status=400)
        assert "Mcp-Param-Region" in error["message"]

    def test_header_without_body_value(self):
        response = self.call_regional({}, {"Mcp-Param-Region": "us-west1"})

        self.assert_error(response, HEADER_MISMATCH, status=400)

    def test_header_with_null_body_value(self):
        response = self.call_regional(
            {"region": None}, {"Mcp-Param-Region": "us-west1"}
        )

        self.assert_error(response, HEADER_MISMATCH, status=400)

    def test_value_mismatch(self):
        response = self.call_regional(
            {"region": "us-west1"}, {"Mcp-Param-Region": "eu-north1"}
        )

        error = self.assert_error(response, HEADER_MISMATCH, status=400)
        assert "does not match body value" in error["message"]

    def test_nested_path_through_non_object(self):
        # The "config" value is not an object, so the nested "zone" parameter
        # has no value and no Mcp-Param-Zone header is expected.
        response = self.call_regional({"config": "not-an-object"}, {})

        self.assert_result(response)

    def test_nested_path_match(self):
        response = self.call_regional(
            {"config": {"zone": "a"}}, {"Mcp-Param-Zone": "a"}
        )

        self.assert_result(response)

    def test_integer_with_boolean_body_value(self):
        response = self.call_regional({"shard": True}, {"Mcp-Param-Shard": "1"})

        self.assert_error(response, HEADER_MISMATCH, status=400)

    def test_integer_mismatch(self):
        response = self.call_regional(
            {"region": "r", "shard": 3},
            {"Mcp-Param-Region": "r", "Mcp-Param-Shard": "four"},
        )

        self.assert_error(response, HEADER_MISMATCH, status=400)

    def test_boolean_mismatch(self):
        response = self.call_regional(
            {"region": "r", "fast": False},
            {"Mcp-Param-Region": "r", "Mcp-Param-Fast": "true"},
        )

        self.assert_error(response, HEADER_MISMATCH, status=400)

    def test_malformed_base64_value(self):
        response = self.call_regional(
            {"region": "us-west1"}, {"Mcp-Param-Region": "=?base64?!!!?="}
        )

        self.assert_error(response, HEADER_MISMATCH, status=400)


class AuthTests(EndpointTestCase):
    url = "/secure-mcp"

    def test_unauthorized(self):
        response = self.post(make_message("tools/list"))

        assert response.status_code == 401

    def test_authorized(self):
        response = self.post(
            make_message("tools/list"),
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 200
        tools = response.json()["result"]["tools"]
        assert [tool["name"] for tool in tools] == ["secret_word"]


class DefaultAuthTests(EndpointTestCase):
    url = "/token-mcp"

    def test_unauthorized_no_header(self):
        response = self.post(make_message("tools/list"))

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.content == b""

    def test_unauthorized_wrong_token(self):
        response = self.post(
            make_message("tools/list"),
            headers={"Authorization": "Bearer wrong-token"},
        )

        assert response.status_code == 401

    def test_unauthorized_wrong_scheme(self):
        response = self.post(
            make_message("tools/list"),
            headers={"Authorization": "Basic test-mcpz-token"},
        )

        assert response.status_code == 401

    def test_authorized(self):
        response = self.post(
            make_message("tools/list"),
            headers={"Authorization": "Bearer test-mcpz-token"},
        )

        assert response.status_code == 200
        tools = response.json()["result"]["tools"]
        assert [tool["name"] for tool in tools] == ["other_secret_word"]

    def test_authorized_lowercase_scheme(self):
        # The auth scheme is case-insensitive (RFC 9110 §11.1).
        response = self.post(
            make_message("tools/list"),
            headers={"Authorization": "bearer test-mcpz-token"},
        )

        assert response.status_code == 200

    def test_authorized_call(self):
        response = self.post(
            make_message("tools/call", {"name": "other_secret_word"}),
            headers={"Authorization": "Bearer test-mcpz-token"},
        )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["content"] == [{"type": "text", "text": "swordfish"}]

    @override_settings(MCPZ_TOKEN="")
    def test_token_unconfigured_at_request_time(self):
        # The endpoint was defined while MCPZ_TOKEN was set, but the setting
        # has since been cleared.
        with pytest.raises(ImproperlyConfigured, match="MCPZ_TOKEN"):
            self.post(make_message("tools/list"))

    @override_settings(MCPZ_TOKEN="")
    def test_token_unconfigured_at_definition_time(self):
        with pytest.raises(ImproperlyConfigured, match="MCPZ_TOKEN"):
            MCPEndpoint(name="test", version="1.0.0")

    @override_settings(MCPZ_TOKEN="")
    def test_no_token_required_when_auth_disabled(self):
        MCPEndpoint(name="test", version="1.0.0", auth=None)


class RegistrationTests(SimpleTestCase):
    def make_endpoint(self):
        return MCPEndpoint(name="test", version="1.0.0")

    def test_duplicate_tool_name(self):
        endpoint = self.make_endpoint()

        @endpoint.tool(description="One.", input_schema={"type": "object"})
        def something(request, arguments):  # pragma: no cover
            return None

        with pytest.raises(ImproperlyConfigured, match="already registered"):

            @endpoint.tool(
                name="something", description="Two.", input_schema={"type": "object"}
            )
            def other(request, arguments):  # pragma: no cover
                return None

    def test_invalid_header_annotation_syntax(self):
        endpoint = self.make_endpoint()

        with pytest.raises(ImproperlyConfigured, match="token syntax"):

            @endpoint.tool(
                description="Bad.",
                input_schema={
                    "type": "object",
                    "properties": {"a": {"type": "string", "x-mcp-header": "Bad Name"}},
                },
            )
            def bad(request, arguments):  # pragma: no cover
                return None

    def test_duplicate_header_annotation(self):
        endpoint = self.make_endpoint()

        with pytest.raises(ImproperlyConfigured, match="case-insensitively unique"):

            @endpoint.tool(
                description="Bad.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "a": {"type": "string", "x-mcp-header": "Region"},
                        "b": {"type": "string", "x-mcp-header": "region"},
                    },
                },
            )
            def bad(request, arguments):  # pragma: no cover
                return None

    def test_header_annotation_on_number(self):
        endpoint = self.make_endpoint()

        with pytest.raises(ImproperlyConfigured, match="string, integer, and boolean"):

            @endpoint.tool(
                description="Bad.",
                input_schema={
                    "type": "object",
                    "properties": {"a": {"type": "number", "x-mcp-header": "Amount"}},
                },
            )
            def bad(request, arguments):  # pragma: no cover
                return None

    def test_header_annotation_not_statically_reachable(self):
        endpoint = self.make_endpoint()

        with pytest.raises(ImproperlyConfigured, match="statically reachable"):

            @endpoint.tool(
                description="Bad.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "a": {
                                        "type": "string",
                                        "x-mcp-header": "Deep",
                                    }
                                },
                            },
                        }
                    },
                },
            )
            def bad(request, arguments):  # pragma: no cover
                return None

    def test_header_annotation_nested_properties_allowed(self):
        endpoint = self.make_endpoint()

        @endpoint.tool(
            description="Nested.",
            input_schema={
                "type": "object",
                "properties": {
                    "config": {
                        "type": "object",
                        "properties": {
                            "region": {"type": "string", "x-mcp-header": "Region"}
                        },
                    }
                },
            },
        )
        def nested(request, arguments):  # pragma: no cover
            return None

        (header_param,) = endpoint._tools["nested"].header_params
        assert header_param.header_name == "Region"
        assert header_param.path == ("config", "region")

    def test_typeddict_input_schema(self):
        endpoint = self.make_endpoint()

        class EchoParams(TypedDict):
            text: str

        @endpoint.tool(description="Echo.", input_schema=EchoParams)
        def echo(request, params):  # pragma: no cover
            return params["text"]

        tool = endpoint._tools["echo"]
        assert tool.input_type is EchoParams
        assert tool.definition["inputSchema"] == {
            "title": "EchoParams",
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }
        assert tool.known_keys == {"text"}

    def test_recursive_struct_not_inlined(self):
        endpoint = self.make_endpoint()

        @endpoint.tool(description="Tree.", input_schema=Node)
        def tree(request, params):  # pragma: no cover
            return None

        schema = endpoint._tools["tree"].definition["inputSchema"]
        assert schema["$ref"] == "#/$defs/Node"
        assert "Node" in schema["$defs"]

    def test_non_ref_type_schema(self):
        endpoint = self.make_endpoint()

        @endpoint.tool(description="Counts.", input_schema=dict[str, int])
        def counts(request, params):  # pragma: no cover
            return None

        assert endpoint._tools["counts"].definition["inputSchema"] == {
            "type": "object",
            "additionalProperties": {"type": "integer"},
        }

    def test_nested_struct_header_annotation_rejected(self):
        endpoint = self.make_endpoint()

        # Inner's annotation ends up behind a $ref, which the specification
        # does not allow.
        with pytest.raises(ImproperlyConfigured, match="statically reachable"):

            @endpoint.tool(description="Bad.", input_schema=Outer)
            def bad(request, params):  # pragma: no cover
                return None

    def test_non_object_property_schema_skipped(self):
        endpoint = self.make_endpoint()

        @endpoint.tool(
            description="Boolean schemas are valid JSON Schema.",
            input_schema={
                "type": "object",
                "properties": {"anything": True},
            },
        )
        def flexible(request, arguments):  # pragma: no cover
            return None

        assert endpoint._tools["flexible"].header_params == ()
