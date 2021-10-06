from django.test import SimpleTestCase

from django_htmx.http import HttpResponseClientRedirect, HttpResponseStopPolling


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
