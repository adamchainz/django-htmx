import json
from urllib.parse import unquote

from django.utils.functional import cached_property


class HtmxMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.htmx = HtmxDetails(request)
        return self.get_response(request)


class HtmxDetails:
    def __init__(self, request):
        self.request = request

    def __bool__(self):
        return self.request.headers.get("HX-Request", "") == "true"

    @cached_property
    def current_url(self):
        return self.request.headers.get("HX-Current-URL") or None

    @cached_property
    def history_restore_request(self):
        return self.request.headers.get("HX-History-Restore-Request", "") == "true"

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

    @cached_property
    def triggering_event(self):
        if "Triggering-Event" in self.request.headers:
            value = unquote(self.request.headers["Triggering-Event"])
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass
        return None
