from __future__ import annotations

import json
from uuid import UUID

import pytest
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse
from django.http import StreamingHttpResponse
from django.test import SimpleTestCase

from django_htmx.http import HttpResponseClientRedirect
from django_htmx.http import HttpResponseClientRefresh
from django_htmx.http import HttpResponseLocation
from django_htmx.http import HttpResponseStopPolling
from django_htmx.http import push_url
from django_htmx.http import replace_url
from django_htmx.http import reswap
from django_htmx.http import retarget
from django_htmx.http import trigger_client_event


class HttpResponseStopPollingTests(SimpleTestCase):
    def test_success(self):
        response = HttpResponseStopPolling()

        assert response.status_code == 286
        assert response.reason_phrase == "Stop Polling"


class HttpResponseClientRedirectTests(SimpleTestCase):
    def test_success(self):
        response = HttpResponseClientRedirect("https://example.com")

        assert response.status_code == 200
        assert response["HX-Redirect"] == "https://example.com"
        assert "Location" not in response

    def test_repr(self):
        response = HttpResponseClientRedirect("https://example.com")

        assert repr(response) == (
            '<HttpResponseClientRedirect status_code=200, "text/html; '
            + 'charset=utf-8", url="https://example.com">'
        )


class HttpResponseLocationTests(SimpleTestCase):
    def test_success(self):
        response = HttpResponseLocation("/home/")

        assert response.status_code == 200
        assert "Location" not in response
        spec = json.loads(response["HX-Location"])
        assert spec == {"path": "/home/"}

    def test_success_complete(self):
        response = HttpResponseLocation(
            "/home/",
            source="#button",
            event="doubleclick",
            target="#main",
            swap="innerHTML",
            select="#content",
            headers={"year": "2022"},
            values={"banner": "true"},
        )

        assert response.status_code == 200
        assert "Location" not in response
        spec = json.loads(response["HX-Location"])
        assert spec == {
            "path": "/home/",
            "source": "#button",
            "event": "doubleclick",
            "target": "#main",
            "swap": "innerHTML",
            "select": "#content",
            "headers": {"year": "2022"},
            "values": {"banner": "true"},
        }


class HttpResponseClientRefreshTests(SimpleTestCase):
    def test_success(self):
        response = HttpResponseClientRefresh()

        assert response.status_code == 200
        assert response["Content-Type"] == "text/html; charset=utf-8"
        assert response["HX-Refresh"] == "true"


class PushUrlTests(SimpleTestCase):
    def test_success(self):
        response = HttpResponse()

        response2 = push_url(response, "/index.html")

        assert response2 is response
        assert response["HX-Push-Url"] == "/index.html"

    def test_success_false(self):
        response = HttpResponse()

        response2 = push_url(response, False)

        assert response2 is response
        assert response["HX-Push-Url"] == "false"


class ReplaceUrlTests(SimpleTestCase):
    def test_success(self):
        response = HttpResponse()

        response2 = replace_url(response, "/index.html")

        assert response2 is response
        assert response["HX-Replace-Url"] == "/index.html"

    def test_success_false(self):
        response = HttpResponse()

        response2 = replace_url(response, False)

        assert response2 is response
        assert response["HX-Replace-Url"] == "false"


class ReswapTests(SimpleTestCase):
    def test_success(self):
        response = HttpResponse()

        response2 = reswap(response, "outerHTML")

        assert response2 is response
        assert response["HX-Reswap"] == "outerHTML"


class RetargetTests(SimpleTestCase):
    def test_success(self):
        response = HttpResponse()

        response2 = retarget(response, "#heading")

        assert response2 is response
        assert response["HX-Retarget"] == "#heading"


class TriggerClientEventTests(SimpleTestCase):
    def test_fail_bad_after_value(self):
        response = HttpResponse()

        with pytest.raises(ValueError) as exinfo:
            trigger_client_event(
                response,
                "custom-event",
                {},
                after="bad-value",  # type: ignore [arg-type]
            )

        assert exinfo.value.args == (
            "Value for 'after' must be one of: 'receive', 'settle', or 'swap'.",
        )

    def test_fail_header_there_not_json(self):
        response = HttpResponse()
        response["HX-Trigger"] = "broken{"

        with pytest.raises(ValueError) as exinfo:
            trigger_client_event(response, "custom-event", {})

        assert exinfo.value.args == ("'HX-Trigger' value should be valid JSON.",)

    def test_success(self):
        response = HttpResponse()

        result = trigger_client_event(
            response, "showConfetti", {"colours": ["purple", "red"]}
        )

        assert result is response
        assert (
            response["HX-Trigger"] == '{"showConfetti": {"colours": ["purple", "red"]}}'
        )

    def test_success_no_params(self):
        response = HttpResponse()

        result = trigger_client_event(response, "showConfetti")

        assert result is response
        assert response["HX-Trigger"] == '{"showConfetti": {}}'

    def test_success_streaming(self):
        response = StreamingHttpResponse(iter((b"hello",)))

        result = trigger_client_event(
            response, "showConfetti", {"colours": ["purple", "red"]}
        )

        assert result is response
        assert (
            response["HX-Trigger"] == '{"showConfetti": {"colours": ["purple", "red"]}}'
        )

    def test_success_multiple_events(self):
        response = HttpResponse()

        result1 = trigger_client_event(
            response, "showConfetti", {"colours": ["purple"]}
        )
        result2 = trigger_client_event(response, "showMessage", {"value": "Well done!"})

        assert result1 is response
        assert result2 is response
        assert response["HX-Trigger"] == (
            '{"showConfetti": {"colours": ["purple"]},'
            + ' "showMessage": {"value": "Well done!"}}'
        )

    def test_success_override(self):
        response = HttpResponse()

        trigger_client_event(response, "showMessage", {"value": "That was okay."})
        trigger_client_event(response, "showMessage", {"value": "Well done!"})

        assert response["HX-Trigger"] == '{"showMessage": {"value": "Well done!"}}'

    def test_success_after_settle(self):
        response = HttpResponse()

        trigger_client_event(
            response, "showMessage", {"value": "Great!"}, after="settle"
        )

        assert (
            response["HX-Trigger-After-Settle"]
            == '{"showMessage": {"value": "Great!"}}'
        )

    def test_success_after_swap(self):
        response = HttpResponse()

        trigger_client_event(response, "showMessage", {"value": "Great!"}, after="swap")

        assert (
            response["HX-Trigger-After-Swap"] == '{"showMessage": {"value": "Great!"}}'
        )

    def test_django_json_encoder(self):
        response = HttpResponse()
        uuid_value = UUID("{12345678-1234-5678-1234-567812345678}")

        trigger_client_event(response, "showMessage", {"uuid": uuid_value})

        assert (
            response["HX-Trigger"]
            == '{"showMessage": {"uuid": "12345678-1234-5678-1234-567812345678"}}'
        )

    def test_custom_json_encoder(self):
        class Bean:
            pass

        class BeanEncoder(DjangoJSONEncoder):
            def default(self, o):
                if isinstance(o, Bean):
                    return "bean"

                return super().default(o)

        response = HttpResponse()

        trigger_client_event(
            response,
            "showMessage",
            {
                "a": UUID("{12345678-1234-5678-1234-567812345678}"),
                "b": Bean(),
            },
            encoder=BeanEncoder,
        )

        assert response["HX-Trigger"] == (
            '{"showMessage": {'
            + '"a": "12345678-1234-5678-1234-567812345678",'
            + ' "b": "bean"'
            + "}}"
        )
