from django.urls import path

from example.core.views import (
    index,
    middleware_tester,
    middleware_tester_table,
    partial_rendering,
)

urlpatterns = [
    path("", index),
    path("middleware-tester/", middleware_tester),
    path("middleware-tester/table/", middleware_tester_table),
    path("partial-rendering/", partial_rendering),
]
