from django.urls import path

from example.core.views import (
    index,
    csrf_demo,
    csrf_demo_checker,
    middleware_tester,
    middleware_tester_table,
    partial_rendering,
)

urlpatterns = [
    path("", index),
    path("csrf-demo/", csrf_demo),
    path("csrf-demo/checker/", csrf_demo_checker),
    path("middleware-tester/", middleware_tester),
    path("middleware-tester/table/", middleware_tester_table),
    path("partial-rendering/", partial_rendering),
]
