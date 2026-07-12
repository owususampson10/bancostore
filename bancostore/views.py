from django.http import HttpResponse


def ratelimited_view(request, exception):
    """django_ratelimit.middleware.RatelimitMiddleware calls this (via
    RATELIMIT_VIEW) whenever a rate-limited view raises Ratelimited. Kept
    deliberately plain — this is meant to be rare and isn't a normal user
    flow worth a Stitch-designed page."""
    return HttpResponse(
        "Too many attempts. Please wait a while and try again.",
        status=429,
    )
