from __future__ import annotations

from django.urls import path

from tests.example import endpoint, secure_endpoint, token_endpoint

urlpatterns = [
    path("mcp", endpoint.as_view()),
    path("secure-mcp", secure_endpoint.as_view()),
    path("token-mcp", token_endpoint.as_view()),
]
