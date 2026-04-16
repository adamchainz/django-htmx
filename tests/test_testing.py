from __future__ import annotations

import json
from typing import Any

import pytest
from django.http import HttpResponse
from django.test import SimpleTestCase, override_settings
from django.urls import path

from django_htmx.testing import HtmxClient


def echo_view(request: Any) -> HttpResponse:
    return HttpResponse(json.dumps(dict(request.headers)))


urlpatterns = [
    path("echo/", echo_view),
]


@override_settings(ROOT_URLCONF=__name__)
class HtmxClientTests(SimpleTestCase):
    def test_htmx_true_sets_hx_request(self):
        client = HtmxClient()
        response = client.get("/echo/", htmx=True)

        headers = json.loads(response.content)
        assert headers.get("Hx-Request") == "true"

    def test_htmx_dict_sets_target(self):
        client = HtmxClient()
        response = client.get("/echo/", htmx={"target": "#dogs"})

        headers = json.loads(response.content)
        assert headers.get("Hx-Request") == "true"
        assert headers.get("Hx-Target") == "#dogs"

    def test_htmx_dict_sets_trigger(self):
        client = HtmxClient()
        response = client.get("/echo/", htmx={"trigger": "load"})

        headers = json.loads(response.content)
        assert headers.get("Hx-Request") == "true"
        assert headers.get("Hx-Trigger") == "load"

    def test_htmx_dict_sets_multiple_headers(self):
        client = HtmxClient()
        response = client.get(
            "/echo/",
            htmx={"target": "#content", "trigger": "click", "trigger_name": "save-btn"},
        )

        headers = json.loads(response.content)
        assert headers.get("Hx-Request") == "true"
        assert headers.get("Hx-Target") == "#content"
        assert headers.get("Hx-Trigger") == "click"
        assert headers.get("Hx-Trigger-Name") == "save-btn"

    def test_htmx_sets_current_url(self):
        client = HtmxClient()
        response = client.get("/echo/", htmx={"current_url": "http://localhost/dogs/"})

        headers = json.loads(response.content)
        assert headers.get("Hx-Current-Url") == "http://localhost/dogs/"

    def test_htmx_prompt(self):
        client = HtmxClient()
        response = client.get("/echo/", htmx={"prompt": "are you sure?"})

        headers = json.loads(response.content)
        assert headers.get("Hx-Prompt") == "are you sure?"

    def test_htmx_boosted(self):
        client = HtmxClient()
        response = client.get("/echo/", htmx={"boosted": "true"})

        headers = json.loads(response.content)
        assert headers.get("Hx-Boosted") == "true"

    def test_htmx_invalid_key_raises(self):
        client = HtmxClient()
        with pytest.raises(ValueError, match="Unknown htmx kwarg"):
            client.get("/echo/", htmx={"typo_key": "bad"})

    def test_no_htmx_kwarg_is_a_normal_request(self):
        client = HtmxClient()
        response = client.get("/echo/")

        headers = json.loads(response.content)
        assert "Hx-Request" not in headers

    def test_htmx_works_on_post(self):
        client = HtmxClient()
        response = client.post("/echo/", htmx={"target": "#form-result"})

        headers = json.loads(response.content)
        assert headers.get("Hx-Request") == "true"
        assert headers.get("Hx-Target") == "#form-result"

    @override_settings(
        MIDDLEWARE=["django_htmx.middleware.HtmxMiddleware"],
    )
    def test_with_middleware_includes_vary_header(self):
        client = HtmxClient()
        response = client.get("/echo/", htmx=True)
        assert response.has_header("Vary")
        assert "HX-Request" in response["Vary"]
