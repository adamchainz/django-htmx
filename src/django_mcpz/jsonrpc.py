"""
Internal JSON-RPC 2.0 message layer, as restricted by MCP: one message per
request (no batches), string or integer ids only (no null), and params must
be an object.
"""

from __future__ import annotations

from typing import Any, Literal

import msgspec
import msgspec.json
from django.http import HttpResponse
from django_msgspec.http import JsonResponse
from msgspec import UNSET, UnsetType

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class Message(msgspec.Struct):
    """A single JSON-RPC request, or notification when id is UNSET."""

    jsonrpc: Literal["2.0"]
    method: str
    id: str | int | UnsetType = UNSET
    params: dict[str, Any] | UnsetType = UNSET


class MessageError(Exception):
    """An invalid JSON-RPC message, reportable as an error response."""

    def __init__(
        self,
        code: int,
        error_message: str,
        request_id: str | int | None = None,
    ) -> None:
        super().__init__(error_message)
        self.code = code
        self.error_message = error_message
        self.request_id = request_id


def decode_message(body: bytes) -> Message:
    """
    Decode and validate a JSON-RPC message body, raising MessageError with
    the appropriate error code for unparsable or invalid input.
    """
    try:
        raw = msgspec.json.decode(body)
    except msgspec.DecodeError:
        raise MessageError(PARSE_ERROR, "Invalid JSON") from None
    try:
        return msgspec.convert(raw, Message)
    except msgspec.ValidationError as exc:
        raise MessageError(
            INVALID_REQUEST,
            f"Invalid JSON-RPC 2.0 message: {exc}",
            request_id=read_id(raw),
        ) from None


def read_id(raw: object) -> str | int | None:
    """
    Best-effort extraction of the request id from an invalid message, so
    error responses can echo it when it is readable, per the specification.
    """
    if isinstance(raw, dict):
        id_ = raw.get("id")
        if not isinstance(id_, bool) and isinstance(id_, str | int):
            return id_
    return None


def result_response(request_id: str | int, result: dict[str, Any]) -> HttpResponse:
    return JsonResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def error_response(
    request_id: str | int | None,
    code: int,
    message: str,
    *,
    data: Any = None,
    status: int = 200,
) -> HttpResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JsonResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": error},
        status=status,
    )
