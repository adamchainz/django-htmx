from django.utils.functional import cached_property


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
    def is_htmx(self):
        return self.request.headers.get("HX-Request", "") == "true"

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
