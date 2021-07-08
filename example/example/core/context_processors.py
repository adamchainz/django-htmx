from django.conf import settings


def debug(request):
    return {"DEBUG": settings.DEBUG}
