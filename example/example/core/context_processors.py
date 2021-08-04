from typing import Dict

from django.conf import settings
from django.http import HttpRequest


def debug(request: HttpRequest) -> Dict[str, str]:
    return {"DEBUG": settings.DEBUG}
