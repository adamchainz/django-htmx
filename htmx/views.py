from django.core.exceptions import ImproperlyConfigured
from django.views.generic.base import TemplateResponseMixin


class HTMXViewMixin(TemplateResponseMixin):
    '''
    Override TemplateResponseMixin to allow to specify a `partial_name` which will be rendered
    instead of the complete template when this view is called via HTMX
    '''

    replace_id = 'htmx-replace-id'

    def get_replace_id(self):
        return self.replace_id

    def get_partial_name(self):
        if not self.partial_name:
            raise ImproperlyConfigured('must specify a partial_name or override get_partial_name')
        return self.partial_name

    def get_template_names(self):
        templates = super().get_template_names()

        if self.request.htmx:
            return [self.get_partial_name()]
        else:
            return templates

    def get_context_data(self, **kwargs):
        context = super(HTMXViewMixin, self).get_context_data(**kwargs)
        context.update(dict(replace_id=self.get_replace_id()))
        return context
