from django.urls import path
from example.core.views import index, attribute_test

urlpatterns = [
    path("", index),
    path("attribute-test", attribute_test),
]
