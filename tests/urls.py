from __future__ import annotations

from django.urls import path

from tests.views import htmx_test_view

urlpatterns = [
    path("htmx-test/", htmx_test_view),
]
