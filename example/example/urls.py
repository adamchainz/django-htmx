from __future__ import annotations

from django.urls import path

from example import views

urlpatterns = [
    path("", views.index),
    path("favicon.ico", views.favicon),
    path("csrf-demo/", views.csrf_demo),
    path("csrf-demo/checker/", views.csrf_demo_checker),
    path("error-demo/", views.error_demo),
    path("error-demo/400/", views.error_demo_400),
    path("error-demo/403/", views.error_demo_403),
    path("error-demo/500/", views.error_demo_500),
    path("error-demo/500-custom/", views.error_demo_500_custom),
    path("middleware-tester/", views.middleware_tester),
    path("middleware-tester/table/", views.middleware_tester_table),
    path("partial-rendering/", views.partial_rendering),
]
