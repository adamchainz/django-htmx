from __future__ import annotations

import base64
import binascii

BASE64_SENTINEL_PREFIX = "=?base64?"
BASE64_SENTINEL_SUFFIX = "?="


def decode_header_value(value: str) -> str:
    """
    Decode an MCP header value, handling the Base64 sentinel format defined by
    the Streamable HTTP transport for values that cannot be safely represented
    as plain ASCII header values.

    Raise ValueError if the value uses the sentinel format but does not
    contain valid Base64-encoded UTF-8.
    """
    if not (
        value.startswith(BASE64_SENTINEL_PREFIX)
        and value.endswith(BASE64_SENTINEL_SUFFIX)
        and len(value) >= len(BASE64_SENTINEL_PREFIX) + len(BASE64_SENTINEL_SUFFIX)
    ):
        return value
    encoded = value[len(BASE64_SENTINEL_PREFIX) : -len(BASE64_SENTINEL_SUFFIX)]
    try:
        return base64.b64decode(encoded.encode(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid Base64-encoded header value: {value!r}") from exc


def encode_header_value(value: str) -> str:
    """
    Encode a string for use as an MCP header value, applying the Base64
    sentinel format when the value cannot be safely represented as a plain
    ASCII header value.
    """
    encodable = all("\x21" <= char <= "\x7e" or char in " \t" for char in value)
    if (
        encodable
        and not (value and (value[0] in " \t" or value[-1] in " \t"))
        and not (
            value.startswith(BASE64_SENTINEL_PREFIX)
            and value.endswith(BASE64_SENTINEL_SUFFIX)
        )
    ):
        return value
    encoded = base64.b64encode(value.encode()).decode()
    return f"{BASE64_SENTINEL_PREFIX}{encoded}{BASE64_SENTINEL_SUFFIX}"
