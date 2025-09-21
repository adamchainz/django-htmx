from __future__ import annotations

import time
from dataclasses import dataclass

from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse, HttpResponseServerError
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from faker import Faker

from django_htmx.middleware import HtmxDetails
from example.forms import OddNumberForm


# Typing pattern recommended by django-stubs:
# https://github.com/typeddjango/django-stubs#how-can-i-create-a-httprequest-thats-guaranteed-to-have-an-authenticated-user
class HtmxHttpRequest(HttpRequest):
    htmx: HtmxDetails


@require_GET
def index(request: HtmxHttpRequest) -> HttpResponse:
    return render(request, "index.html")


@require_GET
def favicon(request: HtmxHttpRequest) -> HttpResponse:
    return HttpResponse(
        (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            + '<text y=".9em" font-size="90">🦊</text>'
            + "</svg>"
        ),
        content_type="image/svg+xml",
    )


# CSRF Demo


@require_GET
def csrf_demo(request: HtmxHttpRequest) -> HttpResponse:
    return render(request, "csrf-demo.html")


@require_POST
def csrf_demo_checker(request: HtmxHttpRequest) -> HttpResponse:
    form = OddNumberForm(request.POST)
    if form.is_valid():
        number = form.cleaned_data["number"]
        number_is_odd = number % 2 == 1
    else:
        number_is_odd = False
    return render(
        request,
        "csrf-demo-checker.html",
        {"form": form, "number_is_odd": number_is_odd},
    )


# Error demo


@require_GET
def error_demo(request: HtmxHttpRequest) -> HttpResponse:
    return render(request, "error-demo.html")


@require_GET
def error_demo_400(request: HtmxHttpRequest) -> HttpResponse:
    raise SuspiciousOperation("What are you doing??")


@require_GET
def error_demo_403(request: HtmxHttpRequest) -> HttpResponse:
    raise PermissionDenied("Access denied!")


@require_GET
def error_demo_500(request: HtmxHttpRequest) -> HttpResponse:
    _ = 1 / 0
    return render(request, "error-demo.html")  # unreachable


@require_GET
def error_demo_500_custom(request: HtmxHttpRequest) -> HttpResponse:
    return HttpResponseServerError(
        "<h1>😱 Woops</h1><p>This is our fancy custom 500 page.</p>"
    )


# Middleware tester

# This uses two views - one to render the form, and the second to render the
# table of attributes.


@require_GET
def middleware_tester(request: HtmxHttpRequest) -> HttpResponse:
    return render(request, "middleware-tester.html")


@require_http_methods(["DELETE", "POST", "PUT"])
def middleware_tester_table(request: HtmxHttpRequest) -> HttpResponse:
    return render(
        request,
        "middleware-tester-table.html",
        {"timestamp": time.time()},
    )


# Partial rendering example


# This dataclass acts as a stand-in for a database model - the example app
# avoids having a database for simplicity.


@dataclass
class Person:
    id: int
    name: str


faker = Faker()
people = [Person(id=i, name=faker.name()) for i in range(1, 235)]


@require_GET
def partial_rendering(request: HtmxHttpRequest) -> HttpResponse:
    # Standard Django pagination
    page_num = request.GET.get("page", "1")
    page = Paginator(object_list=people, per_page=10).get_page(page_num)

    # The htmx magic - render just the `#table-section` partial for htmx
    # requests, allowing us to skip rendering the unchanging parts of the
    # template.
    template_name = "partial-rendering.html"
    if request.htmx:
        template_name += "#table-section"

    return render(
        request,
        template_name,
        {
            "page": page,
        },
    )
