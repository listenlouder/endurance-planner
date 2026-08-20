import logging
import time
import uuid

from django.conf import settings
from django.core.exceptions import MiddlewareNotUsed
from django.http import HttpResponsePermanentRedirect

from config.logging import redact_admin_key, request_id_var

try:
    import sentry_sdk
except ImportError:  # pragma: no cover - sentry_sdk is a pinned dependency
    sentry_sdk = None

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


class RequestLogMiddleware:
    """
    Emits exactly one structured log line per request, and gives every request
    an ID that ties its log lines, its Sentry issue, its ActivityLog row and
    the number printed on its error page together.

    Placed high in MIDDLEWARE rather than at the bottom — see the comment on
    the setting. The consequence worth knowing here is that `request.user` and
    `request.htmx` do not exist when this middleware sees the request on the
    way *in*; they are attached by inner middleware and are only readable on
    the way back out. Everything this class inspects is therefore read during
    the response phase, defensively, because a view that flushed the session
    can make `request.user` raise on access.

    The route pattern is logged alongside the raw path. Paths here embed event
    and driver UUIDs, so grouping by path yields one bucket per request and
    answers nothing; the pattern is what makes "how often does signup fail"
    a question the logs can answer.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.healthcheck_path = getattr(settings, 'HEALTHCHECK_PATH', '/healthz/')
        self.static_url = getattr(settings, 'STATIC_URL', '/static/') or '/static/'

    def __call__(self, request):
        if self._is_exempt(request.path):
            return self.get_response(request)

        request_id = uuid.uuid4().hex[:12]
        request.request_id = request_id
        request.log_started_at = time.perf_counter()

        if sentry_sdk is not None:
            # Tagging rather than logging: this is what makes a Sentry issue
            # and a Railway log line findable from one another.
            sentry_sdk.set_tag('request_id', request_id)

        token = request_id_var.set(request_id)
        try:
            response = self.get_response(request)
            response['X-Request-ID'] = request_id
            self._log(request, response)
            return response
        finally:
            request_id_var.reset(token)

    def _is_exempt(self, path):
        return path == self.healthcheck_path or path.startswith(self.static_url)

    def _log(self, request, response):
        status = response.status_code
        if status >= 500:
            level = logging.ERROR
        elif status >= 400:
            level = logging.WARNING
        else:
            level = logging.INFO

        # No process_exception hook here on purpose: Django converts a raised
        # exception into a 500 response before it reaches this middleware, and
        # django.request already logs the traceback. Adding one would put the
        # same traceback in the stream three times.
        logger.log(
            level,
            "%s %s -> %s",
            request.method, request.path, status,
            extra=request_log_context(request, response),
        )


def request_log_context(request, response):
    """
    The structured fields describing a finished request.

    Every lookup is defensive: this runs on the error path too, and a context
    builder that raises while describing a failure destroys the very record
    needed to diagnose it.
    """
    duration_ms = 0
    started = getattr(request, 'log_started_at', None)
    if started is not None:
        duration_ms = int((time.perf_counter() - started) * 1000)

    match = getattr(request, 'resolver_match', None)

    return {
        'method': request.method,
        'path': redact_admin_key(request.path)[:300],
        'route': getattr(match, 'route', '') if match else '',
        'url_name': getattr(match, 'url_name', '') if match else '',
        'status': response.status_code,
        'duration_ms': duration_ms,
        'user': describe_user(request),
        'visitor_id': getattr(request, 'visitor_id', ''),
        'htmx': bool(getattr(request, 'htmx', False)),
        'referer': redact_admin_key(request.META.get('HTTP_REFERER', ''))[:300],
        'ip': client_ip(request),
    }


def describe_user(request):
    """Discord username for an authenticated user, 'anon' otherwise."""
    try:
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            return user.discord_username or str(user.pk)
    except Exception:
        return 'unknown'
    return 'anon'


def client_ip(request):
    """Client address, preferring Railway's proxy header."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()[:45]
    return request.META.get('REMOTE_ADDR', '')[:45]
