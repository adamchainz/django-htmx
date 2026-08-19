from __future__ import annotations

from diner.mcp import endpoint
from django.http import HttpRequest, HttpResponse
from django.urls import path


def index(request: HttpRequest) -> HttpResponse:
    return HttpResponse(
        "Django’s Diner serves MCP at /mcp. See README.rst for how to connect.",
        content_type="text/plain; charset=utf-8",
    )


urlpatterns = [
    path("", index),
    path("mcp", endpoint.as_view()),
]
