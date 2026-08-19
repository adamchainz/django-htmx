from __future__ import annotations

import secrets

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse


def _get_token() -> str:
    token = getattr(settings, "MCPZ_TOKEN", "")
    if not token:
        raise ImproperlyConfigured(
            "The MCPZ_TOKEN setting is required by django-mcpz's default"
            " bearer token authentication. Set it to a long random string,"
            " or pass a different auth callable, or None, to MCPEndpoint."
        )
    return token


def bearer_token_auth(request: HttpRequest) -> HttpResponse | None:
    """
    The default endpoint authentication: require the Authorization header to
    carry the static bearer token in the MCPZ_TOKEN setting.
    """
    token = _get_token()
    header = request.headers.get("Authorization", "")
    # The auth scheme is case-insensitive (RFC 9110 §11.1).
    scheme, _, given = header.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        given.strip(" ").encode(), token.encode()
    ):
        return HttpResponse(status=401, headers={"WWW-Authenticate": "Bearer"})
    return None
