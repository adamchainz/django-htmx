from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import msgspec
import msgspec.json
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.http.request import validate_host
from django.views.decorators.csrf import csrf_exempt
from django_msgspec import enc_hook
from msgspec import UnsetType

from django_mcpz.auth import _get_token, bearer_token_auth
from django_mcpz.headers import decode_header_value
from django_mcpz.jsonrpc import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    MessageError,
    decode_message,
    error_response,
    result_response,
)

logger = logging.getLogger("django_mcpz")

PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_PROTOCOL_VERSIONS = [PROTOCOL_VERSION]

# MCP-defined error codes (reserved sub-range -32020 to -32099)
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# HTTP field-name token syntax (RFC 9110 §5.1)
_TCHAR_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")

_HEADER_PARAM_TYPES = frozenset(["string", "integer", "boolean"])


class ToolError(Exception):
    """
    Raise in a tool function to report a tool execution error in-band, so the
    calling language model can see the message and self-correct.
    """


@dataclass(frozen=True)
class HeaderParam:
    """A tool parameter annotated with x-mcp-header."""

    header_name: str
    path: tuple[str, ...]
    type: str


@dataclass(frozen=True)
class Tool:
    name: str
    func: Callable[[HttpRequest, Any], Any]
    definition: dict[str, Any]
    header_params: tuple[HeaderParam, ...]
    input_type: Any
    known_keys: frozenset[str] | None


def _type_schema(type_: Any) -> dict[str, Any]:
    """
    Generate a JSON Schema for a msgspec-supported type, inlining the
    top-level $ref so that properties stay statically reachable, as
    x-mcp-header annotations require.
    """
    schema = msgspec.json.schema(type_)
    ref = schema.get("$ref", "")
    defs = schema.get("$defs")
    if not (
        isinstance(ref, str) and ref.startswith("#/$defs/") and isinstance(defs, dict)
    ):
        return schema
    name = ref.removeprefix("#/$defs/")
    # A self-referencing (recursive) definition cannot be inlined.
    if f'"#/$defs/{name}"'.encode() in msgspec.json.encode(defs):
        return schema
    root = dict(defs[name])
    remaining = {key: value for key, value in defs.items() if key != name}
    if remaining:
        root["$defs"] = remaining
    return root


def _collect_header_params(input_schema: dict[str, Any]) -> tuple[HeaderParam, ...]:
    """
    Find x-mcp-header annotations in an input schema and validate them per the
    MCP specification, raising ImproperlyConfigured for invalid annotations.
    """
    found: list[HeaderParam] = []
    seen_names: set[str] = set()

    def nested_dicts(value: object) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            return [value]
        elif isinstance(value, list):
            return [d for item in value for d in nested_dicts(item)]
        else:
            return []

    def walk(subschema: dict[str, Any], path: tuple[str, ...], reachable: bool) -> None:
        header_name = subschema.get("x-mcp-header")
        # In unreachable positions, only a string value identifies an
        # annotation: mapping containers like $defs are walked as schemas
        # too, and there a dict value just means a member with that name.
        if header_name is not None and (reachable or isinstance(header_name, str)):
            if not reachable or not path:
                raise ImproperlyConfigured(
                    "x-mcp-header annotations may only be applied to properties"
                    " statically reachable through 'properties' keys, not at"
                    f" {'/'.join(path) or '<root>'}."
                )
            if not isinstance(header_name, str) or not _TCHAR_RE.match(header_name):
                raise ImproperlyConfigured(
                    f"Invalid x-mcp-header value {header_name!r}: values must be"
                    " non-empty and use HTTP field-name token syntax."
                )
            if header_name.lower() in seen_names:
                raise ImproperlyConfigured(
                    f"Duplicate x-mcp-header value {header_name!r}: values must"
                    " be case-insensitively unique within an input schema."
                )
            type_ = subschema.get("type")
            if type_ not in _HEADER_PARAM_TYPES:
                raise ImproperlyConfigured(
                    f"Invalid x-mcp-header on property {'/'.join(path)!r}: only"
                    " string, integer, and boolean parameters may be annotated."
                )
            seen_names.add(header_name.lower())
            found.append(HeaderParam(header_name, path, type_))

        for key, value in subschema.items():
            if key == "properties" and isinstance(value, dict):
                for prop_name, prop_schema in value.items():
                    if isinstance(prop_schema, dict):
                        walk(prop_schema, (*path, prop_name), reachable)
            else:
                # Any other keyword (items, oneOf, $defs, if/then/else, …)
                # breaks static reachability for everything beneath it.
                for subvalue in nested_dicts(value):
                    walk(subvalue, path, reachable=False)

    walk(input_schema, (), reachable=True)
    return tuple(found)


_INTEGER_RE = re.compile(r"[+-]?[0-9]+\Z")
_INTEGER_DECIMAL_RE = re.compile(r"[+-]?[0-9]+\.[0-9]+\Z")


def _header_value_matches(header_value: str, body_value: object, type_: str) -> bool:
    if type_ == "boolean":
        return isinstance(body_value, bool) and header_value == str(body_value).lower()
    elif type_ == "integer":
        if isinstance(body_value, bool) or not isinstance(body_value, int | float):
            return False
        if _INTEGER_RE.match(header_value):
            # int() compares exactly, unlike float(), which loses precision
            # above 2**53. The regex also excludes int()'s laxities, like
            # underscore separators.
            return int(header_value) == body_value
        if _INTEGER_DECIMAL_RE.match(header_value):
            # Compare numerically, per the specification: "42.0" equals 42.
            header_float = float(header_value)
            return header_float.is_integer() and int(header_float) == body_value
        return False
    else:
        return isinstance(body_value, str) and header_value == body_value


def _tool_result(output: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"isError": False}
    if output is None:
        result["content"] = []
    elif isinstance(output, str):
        result["content"] = [{"type": "text", "text": output}]
    else:
        encoded = msgspec.json.encode(output, enc_hook=enc_hook)
        result["content"] = [{"type": "text", "text": encoded.decode()}]
        # Reuse the encoding: msgspec inlines Raw values when serializing the
        # response, avoiding encoding the output twice.
        result["structuredContent"] = msgspec.Raw(encoded)
    return result


class MCPEndpoint:
    """
    A Model Context Protocol (MCP) endpoint, speaking MCP protocol revision
    2026-07-28 over the Streamable HTTP transport. Attach tool functions with
    the tool() decorator and route requests to as_view().
    """

    def __init__(
        self,
        *,
        name: str,
        version: str,
        title: str | None = None,
        instructions: str | None = None,
        ttl_ms: int = 0,
        cache_scope: Literal["public", "private"] = "private",
        auth: Callable[[HttpRequest], HttpResponse | None] | None = bearer_token_auth,
    ) -> None:
        if auth is bearer_token_auth:
            # Fail at endpoint definition, typically import time, if
            # MCPZ_TOKEN is missing, rather than on the first request.
            _get_token()
        self.server_info: dict[str, str] = {"name": name, "version": version}
        if title is not None:
            self.server_info["title"] = title
        self.instructions = instructions
        self.ttl_ms = ttl_ms
        self.cache_scope = cache_scope
        self.auth = auth
        self._tools: dict[str, Tool] = {}

    # -- Tool registration

    def tool(
        self,
        *,
        description: str,
        input_schema: dict[str, Any] | type[Any],
        name: str | None = None,
        title: str | None = None,
        output_schema: dict[str, Any] | type[Any] | None = None,
        annotations: dict[str, Any] | None = None,
        icons: list[dict[str, Any]] | None = None,
    ) -> Callable[
        [Callable[[HttpRequest, Any], Any]],
        Callable[[HttpRequest, Any], Any],
    ]:
        """Register the decorated function as an MCP tool on this endpoint."""

        def decorator(
            func: Callable[[HttpRequest, Any], Any],
        ) -> Callable[[HttpRequest, Any], Any]:
            tool_name = name if name is not None else func.__name__
            if tool_name in self._tools:
                raise ImproperlyConfigured(
                    f"A tool named {tool_name!r} is already registered."
                )
            known_keys = None
            if isinstance(input_schema, dict):
                input_type = None
                input_schema_dict = input_schema
            else:
                input_type = input_schema
                input_schema_dict = _type_schema(input_schema)
                properties = input_schema_dict.get("properties")
                if (
                    isinstance(properties, dict)
                    and "additionalProperties" not in input_schema_dict
                ):
                    # Unknown arguments are rejected by default: tools are
                    # called by language models, for which a silently ignored
                    # misspelt argument is an invisible bug, where an in-band
                    # error allows self-correction.
                    input_schema_dict["additionalProperties"] = False
                    known_keys = frozenset(properties)
            definition: dict[str, Any] = {
                "name": tool_name,
                "description": description,
                "inputSchema": input_schema_dict,
            }
            if title is not None:
                definition["title"] = title
            if output_schema is not None:
                if isinstance(output_schema, dict):
                    definition["outputSchema"] = output_schema
                else:
                    definition["outputSchema"] = _type_schema(output_schema)
            if annotations is not None:
                definition["annotations"] = annotations
            if icons is not None:
                definition["icons"] = icons
            self._tools[tool_name] = Tool(
                name=tool_name,
                func=func,
                definition=definition,
                header_params=_collect_header_params(input_schema_dict),
                input_type=input_type,
                known_keys=known_keys,
            )
            return func

        return decorator

    # -- The view

    def as_view(self) -> Callable[[HttpRequest], HttpResponse]:
        @csrf_exempt
        def view(request: HttpRequest) -> HttpResponse:
            return self._handle(request)

        view.mcp_endpoint = self  # type: ignore [attr-defined]
        return view

    def _handle(self, request: HttpRequest) -> HttpResponse:
        # MCP 2 removed the GET/SSE stream and DELETE-terminated sessions:
        # the MCP endpoint accepts POST only.
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])

        if not self._origin_allowed(request):
            return error_response(
                None, INVALID_REQUEST, "Invalid Origin header", status=403
            )

        if self.auth is not None:
            auth_response = self.auth(request)
            if auth_response is not None:
                return auth_response

        try:
            message = decode_message(request.body)
        except MessageError as exc:
            return error_response(
                exc.request_id, exc.code, exc.error_message, status=400
            )

        method = message.method
        request_id = message.id
        if isinstance(request_id, UnsetType):
            # A notification. This protocol revision defines no client-to-
            # server notifications, and no header requirements for
            # notification POSTs: acknowledge and do nothing.
            return HttpResponse(status=202)

        params = message.params
        if isinstance(params, UnsetType):
            params = {}

        error = self._validate_metadata(request, request_id, method, params)
        if error is not None:
            return error

        if method == "server/discover":
            return self._discover(request_id)
        elif method == "tools/list":
            return self._tools_list(request_id, params)
        elif method == "tools/call":
            return self._tools_call(request, request_id, params)
        elif method == "initialize":
            # Legacy (pre-2026-07-28) clients open with an initialize
            # handshake. Name the supported versions, since this error may be
            # the only diagnostic such clients can surface.
            return error_response(
                request_id,
                METHOD_NOT_FOUND,
                (
                    "Method not found: 'initialize'. This server only supports"
                    " stateless MCP protocol versions:"
                    f" {', '.join(SUPPORTED_PROTOCOL_VERSIONS)}."
                ),
                status=404,
            )
        else:
            return error_response(
                request_id,
                METHOD_NOT_FOUND,
                f"Method not found: {method!r}",
                status=404,
            )

    def _origin_allowed(self, request: HttpRequest) -> bool:
        origin = request.headers.get("Origin")
        if origin is None:
            return True
        try:
            host = urlsplit(origin).hostname
        except ValueError:
            return False
        if host is None:
            return False
        allowed_hosts = settings.ALLOWED_HOSTS
        if settings.DEBUG and not allowed_hosts:
            allowed_hosts = [".localhost", "127.0.0.1", "[::1]"]
        return validate_host(host, allowed_hosts)

    def _validate_metadata(
        self,
        request: HttpRequest,
        request_id: str | int,
        method: str,
        params: dict[str, Any],
    ) -> HttpResponse | None:
        header_version = request.headers.get("MCP-Protocol-Version")
        if header_version is None:
            return error_response(
                request_id,
                HEADER_MISMATCH,
                "Missing required MCP-Protocol-Version header",
                status=400,
            )
        if header_version not in SUPPORTED_PROTOCOL_VERSIONS:
            return error_response(
                request_id,
                UNSUPPORTED_PROTOCOL_VERSION,
                "Unsupported protocol version",
                data={
                    "supported": SUPPORTED_PROTOCOL_VERSIONS,
                    "requested": header_version,
                },
                status=400,
            )

        meta = params.get("_meta")
        if not isinstance(meta, dict):
            meta = {}
        meta_version = meta.get(META_PROTOCOL_VERSION)
        if meta_version is None:
            return error_response(
                request_id,
                INVALID_PARAMS,
                f"Missing required _meta field: {META_PROTOCOL_VERSION!r}",
                status=400,
            )
        if META_CLIENT_CAPABILITIES not in meta:
            return error_response(
                request_id,
                INVALID_PARAMS,
                f"Missing required _meta field: {META_CLIENT_CAPABILITIES!r}",
                status=400,
            )
        if meta_version != header_version:
            return error_response(
                request_id,
                HEADER_MISMATCH,
                (
                    "Header mismatch: MCP-Protocol-Version header value"
                    f" {header_version!r} does not match body value"
                    f" {meta_version!r}"
                ),
                status=400,
            )

        header_method = request.headers.get("Mcp-Method")
        if header_method is None:
            return error_response(
                request_id,
                HEADER_MISMATCH,
                "Missing required Mcp-Method header",
                status=400,
            )
        if header_method != method:
            return error_response(
                request_id,
                HEADER_MISMATCH,
                (
                    f"Header mismatch: Mcp-Method header value {header_method!r}"
                    f" does not match body value {method!r}"
                ),
                status=400,
            )
        return None

    # -- Method handlers

    def _discover(self, request_id: str | int) -> HttpResponse:
        result: dict[str, Any] = {
            "supportedVersions": SUPPORTED_PROTOCOL_VERSIONS,
            "capabilities": {"tools": {}},
            "ttlMs": self.ttl_ms,
            "cacheScope": self.cache_scope,
        }
        if self.instructions is not None:
            result["instructions"] = self.instructions
        return self._result(request_id, result)

    def _tools_list(
        self, request_id: str | int, params: dict[str, Any]
    ) -> HttpResponse:
        if params.get("cursor") is not None:
            # All tools are returned in one page, so no cursor is ever valid.
            return error_response(request_id, INVALID_PARAMS, "Invalid cursor")
        return self._result(
            request_id,
            {
                "tools": [tool.definition for tool in self._tools.values()],
                "ttlMs": self.ttl_ms,
                "cacheScope": self.cache_scope,
            },
        )

    def _tools_call(
        self, request: HttpRequest, request_id: str | int, params: dict[str, Any]
    ) -> HttpResponse:
        name = params.get("name")
        if not isinstance(name, str):
            return error_response(request_id, INVALID_PARAMS, "Missing tool name")

        error = _validate_name_header(request, request_id, name)
        if error is not None:
            return error

        tool = self._tools.get(name)
        if tool is None:
            return error_response(request_id, INVALID_PARAMS, f"Unknown tool: {name!r}")

        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        elif not isinstance(arguments, dict):
            return error_response(
                request_id, INVALID_PARAMS, "arguments must be an object"
            )

        error = _validate_param_headers(request, request_id, tool, arguments)
        if error is not None:
            return error

        # Input validation errors are tool execution errors, reported in-band
        # so the calling model can self-correct.
        if tool.known_keys is not None:
            unknown = sorted(set(arguments) - tool.known_keys)
            if unknown:
                names = ", ".join(f"`{key}`" for key in unknown)
                plural = "s" if len(unknown) > 1 else ""
                return self._tool_error(
                    request_id, f"Invalid arguments: unknown field{plural} {names}"
                )
        tool_arguments: Any = arguments
        if tool.input_type is not None:
            try:
                tool_arguments = msgspec.convert(arguments, tool.input_type)
            except msgspec.ValidationError as exc:
                return self._tool_error(request_id, f"Invalid arguments: {exc}")

        try:
            output = tool.func(request, tool_arguments)
            result = _tool_result(output)
        except ToolError as exc:
            return self._tool_error(request_id, str(exc))
        except Exception:
            logger.exception("Tool %r raised an exception", name)
            return self._tool_error(request_id, f"Tool {name!r} failed unexpectedly.")

        return self._result(request_id, result)

    def _tool_error(self, request_id: str | int, text: str) -> HttpResponse:
        # Tool execution failures are reported in-band, not as JSON-RPC
        # errors, so the calling model can see them and self-correct.
        return self._result(
            request_id,
            {"content": [{"type": "text", "text": text}], "isError": True},
        )

    def _result(self, request_id: str | int, result: dict[str, Any]) -> HttpResponse:
        return result_response(
            request_id,
            {
                "resultType": "complete",
                **result,
                "_meta": {META_SERVER_INFO: self.server_info},
            },
        )


def _validate_name_header(
    request: HttpRequest, request_id: str | int, name: str
) -> HttpResponse | None:
    header_name = request.headers.get("Mcp-Name")
    if header_name is None:
        return error_response(
            request_id, HEADER_MISMATCH, "Missing required Mcp-Name header", status=400
        )
    try:
        decoded = decode_header_value(header_name)
    except ValueError:
        return error_response(
            request_id, HEADER_MISMATCH, "Malformed Mcp-Name header", status=400
        )
    if decoded != name:
        return error_response(
            request_id,
            HEADER_MISMATCH,
            (
                f"Header mismatch: Mcp-Name header value {decoded!r} does not"
                f" match body value {name!r}"
            ),
            status=400,
        )
    return None


def _validate_param_headers(
    request: HttpRequest,
    request_id: str | int,
    tool: Tool,
    arguments: dict[str, Any],
) -> HttpResponse | None:
    for header_param in tool.header_params:
        header = f"Mcp-Param-{header_param.header_name}"
        header_value = request.headers.get(header)

        body_value: object = arguments
        for key in header_param.path:
            if not isinstance(body_value, dict):
                body_value = None
                break
            body_value = body_value.get(key)

        if body_value is None:
            if header_value is not None:
                return error_response(
                    request_id,
                    HEADER_MISMATCH,
                    (
                        f"Header mismatch: {header} header provided but no"
                        " corresponding value in the request body"
                    ),
                    status=400,
                )
            continue

        if header_value is None:
            return error_response(
                request_id,
                HEADER_MISMATCH,
                f"Missing required header: {header}",
                status=400,
            )
        try:
            decoded = decode_header_value(header_value)
        except ValueError:
            return error_response(
                request_id,
                HEADER_MISMATCH,
                f"Malformed header value: {header}",
                status=400,
            )
        if not _header_value_matches(decoded, body_value, header_param.type):
            return error_response(
                request_id,
                HEADER_MISMATCH,
                (
                    f"Header mismatch: {header} header value {decoded!r} does"
                    f" not match body value {body_value!r}"
                ),
                status=400,
            )
    return None
