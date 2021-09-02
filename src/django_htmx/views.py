from django.core.exceptions import ImproperlyConfigured
from django.views.generic.list import BaseListView, MultipleObjectTemplateResponseMixin


class HtmxMixin(MultipleObjectTemplateResponseMixin):
    partial_template_name = None

    def get_template_names(self):
        names = super().get_template_names()
        if self.request.htmx:
            if self.partial_template_name is None:
                raise ImproperlyConfigured(
                    "HtmxMixin requires a definition of 'partial_template_name'"
                )
            return [self.partial_template_name]
        return names


class HtmxListView(HtmxMixin, BaseListView):
    pass
