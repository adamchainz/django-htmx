from __future__ import annotations
from typing import Any
from django.test.client import Client


class HtmxClientMixin:
    def request(self, **request: Any) -> Any:
        request.setdefault("HTTP_HX_REQUEST", "true")
        return super().request(**request)  # type: ignore [misc]


class HtmxClient(HtmxClientMixin, Client):
    pass


class HtmxTestCaseAssertions:
    def assertHtmxClientRedirect(
        self, response: Any, expected_url: str, msg_prefix: str = ""
    ) -> None:
        self.assertEqual(  # type: ignore [attr-defined]
            response.status_code,
            200,
            msg_prefix + f"Expected status code 200, got {response.status_code}.",
        )
        self.assertEqual(  # type: ignore [attr-defined]
            response.headers.get("HX-Redirect"),
            expected_url,
            msg_prefix + f"Expected HX-Redirect to '{expected_url}'.",
        )

    def assertHtmxStopPolling(self, response: Any, msg_prefix: str = "") -> None:
        self.assertEqual(  # type: ignore [attr-defined]
            response.status_code,
            286,
            msg_prefix + f"Expected HTTP 286, got {response.status_code}.",
        )
