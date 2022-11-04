from __future__ import annotations

from django.urls import path

from example.core.views import csrf_demo
from example.core.views import csrf_demo_checker
from example.core.views import error_demo
from example.core.views import error_demo_trigger
from example.core.views import favicon
from example.core.views import index
from example.core.views import middleware_tester
from example.core.views import middleware_tester_table
from example.core.views import partial_rendering

urlpatterns = [
    path("", index),
    path("favicon.ico", favicon),
    path("csrf-demo/", csrf_demo),
    path("csrf-demo/checker/", csrf_demo_checker),
    path("error-demo/", error_demo),
    path("error-demo/trigger/", error_demo_trigger),
    path("middleware-tester/", middleware_tester),
    path("middleware-tester/table/", middleware_tester_table),
    path("partial-rendering/", partial_rendering),
]
