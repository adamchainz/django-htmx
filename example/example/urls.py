from django.urls import path

from example.core.views import attribute_test, index

urlpatterns = [
    path("", index),
    path("attribute-test", attribute_test),
]
