import uuid
from datetime import datetime
from decimal import Decimal

import pytest
from django.http import HttpResponse
from django.test import SimpleTestCase

from django_htmx.http import (
    HttpResponseClientRedirect,
    HttpResponseStopPolling,
    trigger_client_event,
)


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

        trigger_client_event(response, "showConfetti", {"colours": ["purple", "red"]})

        assert (
            response["HX-Trigger"] == '{"showConfetti": {"colours": ["purple", "red"]}}'
        )

    def test_success_multiple_events(self):
        response = HttpResponse()

        trigger_client_event(response, "showConfetti", {"colours": ["purple"]})
        trigger_client_event(response, "showMessage", {"value": "Well done!"})

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
        uuid_value = uuid.uuid4()
        dt = datetime(2022, 1, 1, 10, 0, 0)

        params = {
            "decimal": Decimal("9.99"),
            "uuid": uuid_value,
            "datetime": dt,
            "date": dt.date(),
        }
        trigger_client_event(response, "showMessage", params)

        expected = (
            '{"decimal": "%s", "uuid": "%s", "datetime": "%s", "date": "%s"}'
        ) % ("9.99", uuid_value, "2022-01-01T10:00:00", "2022-01-01")

        assert expected in response["HX-Trigger"]
