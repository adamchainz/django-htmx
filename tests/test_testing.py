from __future__ import annotations
import json
from typing import Any
import pytest
from django.http import HttpResponse
from django.test import SimpleTestCase, override_settings
from django.urls import path
from django_htmx.http import HttpResponseClientRedirect, HttpResponseStopPolling
from django_htmx.testing import HtmxClient, HtmxTestCaseAssertions


def echo_view(request: Any) -> HttpResponse:
    return HttpResponse(json.dumps(dict(request.headers)))
urlpatterns = [
    path("echo/", echo_view),
]

@override_settings(ROOT_URLCONF=__name__)
class HtmxClientTests(SimpleTestCase):
    def test_adds_hx_request_header(self):
        client = HtmxClient()
        response = client.get("/echo/")
        headers = json.loads(response.content)
        assert headers.get("Hx-Request") == "true"

    def test_retains_other_headers(self):
        client = HtmxClient()
        response = client.get("/echo/", HTTP_HX_TARGET="the-target")
        headers = json.loads(response.content)
        assert headers.get("Hx-Request") == "true"
        assert headers.get("Hx-Target") == "the-target"

    def test_can_override_hx_request_header(self):
        client = HtmxClient()
        response = client.get("/echo/", HTTP_HX_REQUEST="false")
        headers = json.loads(response.content)
        assert headers.get("Hx-Request") == "false"


class HtmxTestCaseAssertionsTests(SimpleTestCase, HtmxTestCaseAssertions):
    def test_assert_htmx_client_redirect_success(self):
        response = HttpResponseClientRedirect("https://example.com")
        self.assertHtmxClientRedirect(response, "https://example.com")

    def test_assert_htmx_stop_polling_success(self):
        response = HttpResponseStopPolling()
        self.assertHtmxStopPolling(response)

    def test_assert_htmx_client_redirect_failure(self):
        response = HttpResponseClientRedirect("https://example.com")
        with pytest.raises(AssertionError):
            self.assertHtmxClientRedirect(response, "https://wrong.com")

    def test_assert_htmx_client_redirect_wrong_status(self):
        response = HttpResponse(status=404)
        with pytest.raises(AssertionError):
            self.assertHtmxClientRedirect(response, "https://example.com")

    def test_assert_htmx_stop_polling_failure(self):
        response = HttpResponse(status=200)
        with pytest.raises(AssertionError):
            self.assertHtmxStopPolling(response)
