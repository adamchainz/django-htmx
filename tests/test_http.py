from django.test import SimpleTestCase

from django_htmx.http import HttpResponseStopPolling


class HttpResponseStopPollingTests(SimpleTestCase):
    def test_success(self):
        response = HttpResponseStopPolling()

        assert response.status_code == 286
        assert response.reason_phrase == "Stop Polling"
