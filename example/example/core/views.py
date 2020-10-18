import time

from django.views.decorators.http import require_http_methods
from django.shortcuts import render


def index(request):
    return render(request, "index.html")


@require_http_methods(["DELETE", "POST", "PUT"])
def attribute_test(request):
    return render(request, "attribute_test.html", {"timestamp": time.time()})
