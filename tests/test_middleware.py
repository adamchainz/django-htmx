from __future__ import annotations

from typing import Any
from typing import cast

from django.core.handlers.wsgi import WSGIRequest
from django.http import HttpResponse
from django.http.response import HttpResponseBase
from django.test import RequestFactory as BaseRequestFactory
from django.test import SimpleTestCase

from django_htmx.middleware import HtmxDetails
from django_htmx.middleware import HtmxMiddleware


class HtmxWSGIRequest(WSGIRequest):
    htmx: HtmxDetails


class RequestFactory(BaseRequestFactory):
    def get(
        self, path: str, data: Any = None, secure: bool = False, **extra: Any
    ) -> HtmxWSGIRequest:
        return cast(HtmxWSGIRequest, super().get(path, data, secure, **extra))


def dummy_view(request):
    return HttpResponse("Hello!")


class HtmxMiddlewareTests(SimpleTestCase):
    request_factory = RequestFactory()
    middleware = HtmxMiddleware(dummy_view)

    def test_bool_default(self):
        request = self.request_factory.get("/")
        self.middleware(request)
        assert bool(request.htmx) is False

    def test_bool_false(self):
        request = self.request_factory.get("/", HTTP_HX_REQUEST="false")
        self.middleware(request)
        assert bool(request.htmx) is False

    def test_bool_true(self):
        request = self.request_factory.get("/", HTTP_HX_REQUEST="true")
        self.middleware(request)
        assert bool(request.htmx) is True

    def test_boosted_default(self):
        request = self.request_factory.get("/")
        self.middleware(request)
        assert not request.htmx.boosted

    def test_boosted_set(self):
        request = self.request_factory.get("/", HTTP_HX_BOOSTED="true")
        self.middleware(request)
        assert request.htmx.boosted

    def test_current_url_default(self):
        request = self.request_factory.get("/")
        self.middleware(request)
        assert request.htmx.current_url is None

    def test_current_url_set(self):
        request = self.request_factory.get(
            "/", HTTP_HX_CURRENT_URL="https://example.com"
        )
        self.middleware(request)
        assert request.htmx.current_url == "https://example.com"

    def test_current_url_set_url_encoded(self):
        request = self.request_factory.get(
            "/",
            HTTP_HX_CURRENT_URL="https%3A%2F%2Fexample.com%2F%3F",
            HTTP_HX_CURRENT_URL_URI_AUTOENCODED="true",
        )
        self.middleware(request)
        assert request.htmx.current_url == "https://example.com/?"

    def test_current_url_abs_path_default(self):
        request = self.request_factory.get("/")
        self.middleware(request)
        assert request.htmx.current_url_abs_path is None

    def test_current_url_abs_path_set_same_domain(self):
        request = self.request_factory.get(
            "/", HTTP_HX_CURRENT_URL="http://testserver/duck/?quack=true#h2"
        )
        self.middleware(request)
        assert request.htmx.current_url_abs_path == "/duck/?quack=true#h2"

    def test_current_url_abs_path_set_different_domain(self):
        request = self.request_factory.get(
            "/", HTTP_HX_CURRENT_URL="https://example.com/duck/?quack=true#h2"
        )
        self.middleware(request)
        assert request.htmx.current_url_abs_path is None

    def test_history_restore_request_false(self):
        request = self.request_factory.get("/", HTTP_HX_HISTORY_RESTORE_REQUEST="false")
        self.middleware(request)
        assert request.htmx.history_restore_request is False

    def test_history_restore_request_true(self):
        request = self.request_factory.get("/", HTTP_HX_HISTORY_RESTORE_REQUEST="true")
        self.middleware(request)
        assert request.htmx.history_restore_request is True

    def test_prompt_default(self):
        request = self.request_factory.get("/")
        self.middleware(request)
        assert request.htmx.prompt is None

    def test_prompt_set(self):
        request = self.request_factory.get("/", HTTP_HX_PROMPT="yes please")
        self.middleware(request)
        assert request.htmx.prompt == "yes please"

    def test_target_default(self):
        request = self.request_factory.get("/")
        self.middleware(request)
        assert request.htmx.target is None

    def test_target_set(self):
        request = self.request_factory.get("/", HTTP_HX_TARGET="some-element")
        self.middleware(request)
        assert request.htmx.target == "some-element"

    def test_trigger_default(self):
        request = self.request_factory.get("/")
        self.middleware(request)
        assert request.htmx.trigger is None

    def test_trigger_set(self):
        request = self.request_factory.get("/", HTTP_HX_TRIGGER="some-element")
        self.middleware(request)
        assert request.htmx.trigger == "some-element"

    def test_trigger_name_default(self):
        request = self.request_factory.get("/")
        self.middleware(request)
        assert request.htmx.trigger_name is None

    def test_trigger_name_set(self):
        request = self.request_factory.get("/", HTTP_HX_TRIGGER_NAME="some-name")
        self.middleware(request)
        assert request.htmx.trigger_name == "some-name"

    def test_triggering_event_none(self):
        request = self.request_factory.get("/")
        self.middleware(request)
        assert request.htmx.triggering_event is None

    def test_triggering_event_bad_json(self):
        request = self.request_factory.get("/", HTTP_TRIGGERING_EVENT="{")
        self.middleware(request)
        assert request.htmx.triggering_event is None

    def test_triggering_event_good_json(self):
        request = self.request_factory.get(
            "/",
            HTTP_TRIGGERING_EVENT="%7B%22target%22%3A%20null%7D",
            HTTP_TRIGGERING_EVENT_URI_AUTOENCODED="true",
        )
        self.middleware(request)
        assert request.htmx.triggering_event == {"target": None}

    async def test_async(self):
        async def dummy_async_view(request):
            return HttpResponse("Hello!")

        middleware = HtmxMiddleware(dummy_async_view)
        request = self.request_factory.get("/", HTTP_HX_REQUEST="true")

        result = middleware(request)
        assert not isinstance(result, HttpResponseBase)  # type narrow
        response = await result

        assert isinstance(response, HttpResponse)
        assert bool(request.htmx) is True
