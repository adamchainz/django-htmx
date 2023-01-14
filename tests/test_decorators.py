from __future__ import annotations

from typing import Any
from typing import cast
from unittest import mock

from django.core.handlers.wsgi import WSGIRequest
from django.http import HttpResponse
from django.test import RequestFactory as BaseRequestFactory
from django.test import SimpleTestCase

from django_htmx.decorators import htmx_view
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


class HTMXDecoratorsTests(SimpleTestCase):
    request_factory = RequestFactory()
    middleware = HtmxMiddleware(dummy_view)

    def test_htmx_request(self):
        request = self.request_factory.get("/", HTTP_HX_REQUEST="true")
        self.middleware(request)
        func = mock.Mock()
        decorated_func = htmx_view(func)
        response = decorated_func(request)  # noqa: F841
        assert func.called

    def test_non_htmx_request(self):
        request = self.request_factory.get("/", HTTP_HX_REQUEST="false")
        self.middleware(request)
        func = mock.Mock()
        decorated_func = htmx_view(func)
        response = decorated_func(request)  # noqa: F841
        assert not func.called
