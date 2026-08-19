from __future__ import annotations

from typing import Annotated, Any

import msgspec
from django.http import HttpRequest, HttpResponse

from django_mcpz.endpoints import MCPEndpoint, ToolError

endpoint = MCPEndpoint(
    name="example-server",
    version="1.2.3",
    title="Example Server",
    instructions="Example MCP server used in the django-mcpz test suite.",
    ttl_ms=300_000,
    cache_scope="public",
    auth=None,
)


@endpoint.tool(
    description="Add two integers.",
    input_schema={
        "type": "object",
        "properties": {
            "a": {"type": "integer"},
            "b": {"type": "integer"},
        },
        "required": ["a", "b"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"sum": {"type": "integer"}},
        "required": ["sum"],
    },
)
def add(request: HttpRequest, arguments: dict[str, Any]) -> dict[str, int]:
    return {"sum": arguments["a"] + arguments["b"]}


@endpoint.tool(
    name="greet",
    title="Greeter",
    description="Greet someone by name.",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "additionalProperties": False,
    },
    annotations={"readOnlyHint": True},
)
def greet_tool(request: HttpRequest, arguments: dict[str, Any]) -> str:
    return f"Hello, {arguments.get('name', 'world')}!"


@endpoint.tool(
    description="Report an in-band tool execution error.",
    input_schema={"type": "object", "additionalProperties": False},
)
def unavailable(request: HttpRequest, arguments: dict[str, Any]) -> None:
    raise ToolError("The flux capacitor is offline. Try the DeLorean instead.")


@endpoint.tool(
    description="Crash with an unexpected exception.",
    input_schema={"type": "object", "additionalProperties": False},
)
def crash(request: HttpRequest, arguments: dict[str, Any]) -> None:
    raise ValueError("secret internal details")


@endpoint.tool(
    description="Do nothing.",
    input_schema={"type": "object", "additionalProperties": False},
)
def noop(request: HttpRequest, arguments: dict[str, Any]) -> None:
    return None


@endpoint.tool(
    description="Return something JSON cannot represent.",
    input_schema={"type": "object", "additionalProperties": False},
)
def unencodable(request: HttpRequest, arguments: dict[str, Any]) -> object:
    return object()


@endpoint.tool(
    description="Echo the region, mirrored into a header for routing.",
    input_schema={
        "type": "object",
        "properties": {
            "region": {"type": "string", "x-mcp-header": "Region"},
            "shard": {"type": "integer", "x-mcp-header": "Shard"},
            "fast": {"type": "boolean", "x-mcp-header": "Fast"},
            "config": {
                "type": "object",
                "properties": {
                    "zone": {"type": "string", "x-mcp-header": "Zone"},
                },
            },
        },
        "additionalProperties": False,
    },
    icons=[{"src": "https://example.com/regional.png", "mimeType": "image/png"}],
)
def regional(request: HttpRequest, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"region": arguments.get("region")}


def bearer_auth(request: HttpRequest) -> HttpResponse | None:
    if request.headers.get("Authorization") != "Bearer test-token":
        return HttpResponse(status=401)
    return None


secure_endpoint = MCPEndpoint(
    name="secure-server",
    version="1.0.0",
    auth=bearer_auth,
)


@secure_endpoint.tool(
    description="Return the secret word.",
    input_schema={"type": "object", "additionalProperties": False},
)
def secret_word(request: HttpRequest, arguments: dict[str, Any]) -> str:
    return "xyzzy"


# Typed schemas: msgspec Structs generate the JSON Schemas and validate
# arguments before the tool runs.


class MultiplyParams(msgspec.Struct):
    a: Annotated[int, msgspec.Meta(description="The first factor.")]
    b: int = 2


class MultiplyResult(msgspec.Struct):
    product: int


@endpoint.tool(
    description="Multiply two integers.",
    input_schema=MultiplyParams,
    output_schema=MultiplyResult,
)
def multiply(request: HttpRequest, params: MultiplyParams) -> MultiplyResult:
    return MultiplyResult(product=params.a * params.b)


class AddTypedParams(msgspec.Struct, forbid_unknown_fields=True):
    a: int
    b: int


@endpoint.tool(
    description="Add two integers, with typed parameters.",
    input_schema=AddTypedParams,
)
def add_typed(request: HttpRequest, params: AddTypedParams) -> dict[str, int]:
    return {"sum": params.a + params.b}


class Point(msgspec.Struct):
    x: int
    y: int


class SegmentParams(msgspec.Struct):
    start: Point
    end: Point
    label: Annotated[
        str,
        msgspec.Meta(extra_json_schema={"x-mcp-header": "Label"}),
    ] = ""


@endpoint.tool(
    description="Measure the Manhattan length of a line segment.",
    input_schema=SegmentParams,
)
def segment_length(request: HttpRequest, params: SegmentParams) -> dict[str, Any]:
    length = abs(params.end.x - params.start.x) + abs(params.end.y - params.start.y)
    return {"label": params.label, "length": length}


# Uses the default auth, comparing against the MCPZ_TOKEN setting.
token_endpoint = MCPEndpoint(
    name="token-server",
    version="1.0.0",
)


@token_endpoint.tool(
    description="Return another secret word.",
    input_schema={"type": "object", "additionalProperties": False},
)
def other_secret_word(request: HttpRequest, arguments: dict[str, Any]) -> str:
    return "swordfish"
