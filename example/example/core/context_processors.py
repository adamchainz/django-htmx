from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest


def debug(request: HttpRequest) -> dict[str, str]:
    return {"DEBUG": settings.DEBUG}
