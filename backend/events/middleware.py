"""
Records each handled request as an ActivityLog row.

Lives in the events app rather than config because it writes an app model.
Sits immediately inside config.middleware.RequestLogMiddleware and reuses the
request ID and start time that middleware already established, so one request
produces one log line and one row bearing the same ID.
"""

import json
import logging
import re
import time
import uuid

from django.conf import settings
from django.core.exceptions import MiddlewareNotUsed

from config.logging import redact_admin_key

from .activity import EXCLUDED_URL_NAMES, action_name
from .models import ActivityLog

logger = logging.getLogger(__name__)

VISITOR_COOKIE = 'wac_vid'
VISITOR_COOKIE_MAX_AGE = 60 * 60 * 24 * 365

# A visitor ID is only ever a uuid4 hex we generated. Anything else arrived
# from a hand-edited or corrupted cookie and is replaced rather than stored.
_VISITOR_ID_RE = re.compile(r'^[0-9a-f]{32}$')

NOT_FOUND_ACTION = 'page.not_found'


class ActivityLogMiddleware:
    """
    Writes one ActivityLog row per non-excluded request.

    IMPORTANT: every database interaction here is wrapped. Analytics is the
    least important thing this application does, and it must never be the
    reason a page fails to render — a broken usage table taking the whole
    site down would be an absurd trade.

    Reads request.user during the response phase, which is when inner
    middleware has finished populating it. Reading it on the way in would
    always yield anonymous.
    """

    def __init__(self, get_response):
        if not getattr(settings, 'ACTIVITY_LOG_ENABLED', True):
            raise MiddlewareNotUsed
        self.get_response = get_response
        self.healthcheck_path = getattr(settings, 'HEALTHCHECK_PATH', '/healthz/')
        self.static_url = getattr(settings, 'STATIC_URL', '/static/') or '/static/'

    def __call__(self, request):
        if request.path == self.healthcheck_path or request.path.startswith(self.static_url):
            return self.get_response(request)

        visitor_id, is_new = self._resolve_visitor_id(request)
        request.visitor_id = visitor_id

        response = self.get_response(request)

        if is_new:
            response.set_cookie(
                VISITOR_COOKIE,
                visitor_id,
                max_age=VISITOR_COOKIE_MAX_AGE,
                httponly=True,
                samesite='Lax',
                secure=not settings.DEBUG,
            )

        self._record(request, response)
        return response

    def _resolve_visitor_id(self, request):
        existing = request.COOKIES.get(VISITOR_COOKIE, '')
        if _VISITOR_ID_RE.match(existing):
            return existing, False
        return uuid.uuid4().hex, True

    def _record(self, request, response):
        try:
            match = getattr(request, 'resolver_match', None)
            url_name = getattr(match, 'url_name', '') if match else ''

            if url_name in EXCLUDED_URL_NAMES:
                return
            if getattr(request, 'activity_skip', False):
                return

            action = action_name(url_name, request.method)
            if not action:
                # Unresolved URL. Worth keeping rather than dropping: a spike
                # here is a dead link somewhere, which is exactly the kind of
                # thing this table exists to surface.
                action = NOT_FOUND_ACTION

            ActivityLog.objects.create(
                request_id=getattr(request, 'request_id', '')[:32],
                action=action[:64],
                method=request.method[:8],
                status_code=response.status_code,
                duration_ms=self._duration_ms(request),
                user=self._user(request),
                visitor_id=getattr(request, 'visitor_id', '')[:32],
                event_id_ref=self._event_id(match),
                # An admin key is a credential; the dashboard does not need
                # it and this table should not become a place it is stored.
                path=redact_admin_key(request.path)[:300],
                is_htmx=bool(getattr(request, 'htmx', False)),
                detail=self._detail(request),
            )
        except Exception:
            logger.warning(
                "activity log write failed for %s %s", request.method,
                request.path, exc_info=True,
            )

    @staticmethod
    def _duration_ms(request):
        started = getattr(request, 'log_started_at', None)
        if started is None:
            return 0
        return int((time.perf_counter() - started) * 1000)

    @staticmethod
    def _user(request):
        try:
            user = getattr(request, 'user', None)
            if user is not None and user.is_authenticated:
                return user
        except Exception:
            return None
        return None

    @staticmethod
    def _event_id(match):
        if not match:
            return None
        return (match.kwargs or {}).get('event_id')

    @staticmethod
    def _detail(request):
        raw = getattr(request, 'activity_detail', None)
        if not raw:
            return {}
        # Round-trip through JSON so one unserialisable value degrades to its
        # repr instead of losing the entire row at INSERT time.
        return json.loads(json.dumps(raw, default=str))
