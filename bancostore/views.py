from django.http import HttpResponse
from django.shortcuts import render


def ratelimited_view(request, exception):
    """django_ratelimit.middleware.RatelimitMiddleware calls this (via
    RATELIMIT_VIEW) whenever a rate-limited view raises Ratelimited. Kept
    deliberately plain — this is meant to be rare and isn't a normal user
    flow worth a Stitch-designed page."""
    return HttpResponse(
        "Too many attempts. Please wait a while and try again.",
        status=429,
    )


def csrf_failure(request, reason=""):
    """CSRF_FAILURE_VIEW: CsrfViewMiddleware calls this directly (not via
    Http404/PermissionDenied), so it needs its own settings.py wiring,
    unlike 404/500/403/400 which Django's default handlers pick up
    automatically just by a template of that name existing. Renders the
    themed "Session Expired" page instead of Django's raw technical CSRF
    error page — a CSRF failure here is almost always a genuinely expired
    session (stale form, long-idle tab), not an attack, so the same
    reassuring copy fits."""
    return render(request, "session_expired.html", status=403)
