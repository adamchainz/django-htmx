from django.utils.functional import cached_property
from django.core.exceptions import ImproperlyConfigured
from django.views.generic.base import TemplateResponseMixin


class HtmxMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        method = request.headers.get("X-HTTP-Method-Override", None)
        if method:
            request.method = method
        request.htmx = HtmxDetails(request)
        return self.get_response(request)


class HtmxDetails:
    def __init__(self, request):
        self.request = request

    def __bool__(self):
        return self.request.headers.get("HX-Request", "") == "true"

    @cached_property
    def active_element(self):
        return self.request.headers.get("HX-Active-Element") or None

    @cached_property
    def active_element_name(self):
        return self.request.headers.get("HX-Active-Element-Name") or None

    @cached_property
    def active_element_value(self):
        return self.request.headers.get("HX-Active-Element-Value") or None

    @cached_property
    def current_url(self):
        return self.request.headers.get("HX-Current-URL") or None

    @cached_property
    def event_target(self):
        return self.request.headers.get("HX-Event-Target") or None

    @cached_property
    def prompt(self):
        return self.request.headers.get("HX-Prompt") or None

    @cached_property
    def target(self):
        return self.request.headers.get("HX-Target") or None

    @cached_property
    def trigger(self):
        return self.request.headers.get("HX-Trigger") or None

    @cached_property
    def trigger_name(self):
        return self.request.headers.get("HX-Trigger-Name") or None


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
