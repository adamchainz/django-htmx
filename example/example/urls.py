from django.urls import path

from example.core.views import index, middleware_tester, middleware_tester_table

urlpatterns = [
    path("", index),
    path("middleware-tester/", middleware_tester),
    path("middleware-tester/table/", middleware_tester_table),
]
