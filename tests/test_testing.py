from __future__ import annotations

import pytest
from django.test import SimpleTestCase, override_settings

from django_htmx.testing import HtmxClient


@override_settings(ROOT_URLCONF="tests.urls")
class HtmxClientTests(SimpleTestCase):
    client_class = HtmxClient

    def test_get_htmx_true(self) -> None:
        response = self.client.get("/htmx-test/", htmx=True)
        assert response.status_code == 200
        data = response.json()
        assert data.get("Hx-Request") == "true"

    def test_get_htmx_false(self) -> None:
        response = self.client.get("/htmx-test/", htmx=None)
        assert response.status_code == 200
        data = response.json()
        assert "Hx-Request" not in data

    def test_get_htmx_dict(self) -> None:
        response = self.client.get(
            "/htmx-test/",
            htmx={
                "boosted": True,
                "current_url": "https://example.com",
                "history_restore_request": True,
                "prompt": "Enter name",
                "target": "#dogs",
                "trigger": "click",
                "trigger_name": "submit-btn",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("Hx-Request") == "true"
        assert data.get("Hx-Boosted") == "True"
        assert data.get("Hx-Current-Url") == "https://example.com"
        assert data.get("Hx-History-Restore-Request") == "True"
        assert data.get("Hx-Prompt") == "Enter name"
        assert data.get("Hx-Target") == "#dogs"
        assert data.get("Hx-Trigger") == "click"
        assert data.get("Hx-Trigger-Name") == "submit-btn"

    def test_get_htmx_invalid_kwarg(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            self.client.get("/htmx-test/", htmx={"invalid": "value"})

        assert "Unknown htmx kwarg 'invalid'" in str(excinfo.value)
        assert "Valid keys are: [" in str(excinfo.value)

    def test_direct_headers(self) -> None:
        from django.test import Client

        client = Client()
        response = client.get("/htmx-test/", headers={"HX-Request": "true"})
        data = response.json()
        assert data.get("Hx-Request") == "true"
