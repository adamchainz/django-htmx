from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from django_htmx.middleware import HtmxMiddleware


def dummy_view(request):
    return HttpResponse("Hello!")


class HtmxMiddlewareTests(SimpleTestCase):
    request_factory = RequestFactory()
    middleware = HtmxMiddleware(dummy_view)

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
