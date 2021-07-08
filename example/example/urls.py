from django.urls import path

from example.core.views import (
    csrf_demo,
    csrf_demo_checker,
    error_demo,
    error_demo_trigger,
    index,
    middleware_tester,
    middleware_tester_table,
    partial_rendering,
)

urlpatterns = [
    path("", index),
    path("csrf-demo/", csrf_demo),
    path("csrf-demo/checker/", csrf_demo_checker),
    path("error-demo/", error_demo),
    path("error-demo/trigger/", error_demo_trigger),
    path("middleware-tester/", middleware_tester),
    path("middleware-tester/table/", middleware_tester_table),
    path("partial-rendering/", partial_rendering),
]
