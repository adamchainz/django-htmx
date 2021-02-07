import time
from dataclasses import dataclass

from django.core.paginator import Paginator
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from faker import Faker


@require_http_methods(("GET",))
def index(request):
    return render(request, "index.html")


# Middleware tester

# This uses two views - one to render the form, and the second to render the
# table of attributes.


@require_http_methods(("GET",))
def middleware_tester(request):
    return render(request, "middleware-tester.html")


@require_http_methods(["DELETE", "POST", "PUT"])
def middleware_tester_table(request):
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


@require_http_methods(("GET",))
def partial_rendering(request):
    # Standard Django pagination
    page_num = request.GET.get("page", "1")
    page = Paginator(object_list=people, per_page=10).get_page(page_num)

    # The htmx magic - use a different, minimal base template for htmx
    # requests, allowing us to skip rendering the unchanging parts of the
    # template.
    if request.htmx:
        base_template = "_partial.html"
    else:
        base_template = "_base.html"

    return render(
        request,
        "partial-rendering.html",
        {
            "base_template": base_template,
            "page": page,
        },
    )
