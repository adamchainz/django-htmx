from __future__ import annotations

import json

from django.http import HttpResponse


def htmx_test_view(request):
    # Reflect all headers back for verification
    return HttpResponse(
        json.dumps(dict(request.headers)), content_type="application/json"
    )
