import time
from django.views.generic import ListView
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from htmx.views import HTMXViewMixin

from example.core import models


@require_http_methods(["DELETE", "POST", "PUT"])
def attribute_test(request):
    return render(request, "attribute_test.html", {"timestamp": time.time()})


class IndexView(HTMXViewMixin, ListView):
    model = models.Person
    paginate_by = 10
    template_name = 'index.html'
    partial_name = 'partials/person_list.html'
    ordering = ('first_name', 'last_name')

index = IndexView.as_view()
