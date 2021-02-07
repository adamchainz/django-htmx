import time

from django.shortcuts import render
from django.views.decorators.http import require_http_methods


def index(request):
    return render(request, "index.html")


def middleware_tester(request):
    return render(request, "middleware-tester.html")


@require_http_methods(["DELETE", "POST", "PUT"])
def middleware_tester_table(request):
    return render(
        request,
        "middleware-tester-table.html",
        {
            "timestamp": time.time(),
        },
    )
