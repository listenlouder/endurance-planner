import logging

from django.conf import settings
from django.core.exceptions import MiddlewareNotUsed
from django.http import HttpResponsePermanentRedirect

logger = logging.getLogger(__name__)


class CanonicalHostMiddleware:
    """
    Redirects requests arriving on a non-canonical hostname to CANONICAL_HOST.

    Session and CSRF cookies are host-only — SESSION_COOKIE_DOMAIN is
    deliberately unset — so a site reachable at both the apex and the www
    domain issues cookies that only work on whichever host the user happened
    to log in through. Navigating between the two silently drops the session
    and the user appears logged out. Funnelling every request onto one host
    keeps a single session valid across the whole site.

    Disabled (removed from the middleware chain) when CANONICAL_HOST is unset,
    or when it is not present in ALLOWED_HOSTS — a canonical host Django would
    reject can only produce a redirect loop.

    IMPORTANT: every hostname you want redirected must ALSO be in ALLOWED_HOSTS.
    request.get_host() validates against it and raises DisallowedHost, which
    Django turns into a bare 400 before this middleware can issue a redirect.
    Listing only CANONICAL_HOST therefore leaves www — the very host this
    exists to rescue — returning 400 rather than a 301.
    """

    def __init__(self, get_response):
        canonical = (getattr(settings, 'CANONICAL_HOST', '') or '').strip()
        if not canonical:
            raise MiddlewareNotUsed
        if canonical not in settings.ALLOWED_HOSTS and '*' not in settings.ALLOWED_HOSTS:
            logger.error(
                "CANONICAL_HOST %r is not in ALLOWED_HOSTS %r — canonical host "
                "redirects are disabled to avoid a redirect loop.",
                canonical, settings.ALLOWED_HOSTS,
            )
            raise MiddlewareNotUsed
        self.get_response = get_response
        self.canonical_host = canonical

    def __call__(self, request):
        if request.get_host() != self.canonical_host:
            return HttpResponsePermanentRedirect(
                f"{request.scheme}://{self.canonical_host}{request.get_full_path()}"
            )
        return self.get_response(request)
