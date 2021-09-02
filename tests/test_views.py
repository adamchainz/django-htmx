from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, SimpleTestCase

from django_htmx.views import HtmxListView


class MyTestView(HtmxListView):
    object_list = []
    queryset = []


class TestHtmxListView(SimpleTestCase):
    factory = RequestFactory()

    def test_missing_partial_template_name(self):
        # If don't provide a partial_template_name the view
        # should give an ImproperlyConfigured error
        request = self.factory.get("/")
        view = MyTestView()
        with self.assertRaises(ImproperlyConfigured):
            view.setup(request)
            view.get_template_names()

    def test_uses_correct_template_on_htmx_request(self):
        # Test that is uses the partial_template_name assigned
        request = self.factory.get("/")
        request.htmx = True
        view = MyTestView.as_view(
            template_name="django_htmx/full_template.html",
            partial_template_name="django_htmx/partial_template.html",
        )(request)
        self.assertEqual(view.template_name, ["django_htmx/partial_template.html"])

    def test_uses_correct_template_on_normal_request(self):
        # Test that is uses the template_name assigned
        request = self.factory.get("/")
        request.htmx = False
        view = MyTestView.as_view(
            template_name="django_htmx/full_template.html",
            partial_template_name="django_htmx/partial_template.html",
        )(request)
        self.assertEqual(view.template_name, ["django_htmx/full_template.html"])
