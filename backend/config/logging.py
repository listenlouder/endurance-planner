"""
Logging helpers: a JSON formatter, a request-ID filter, and the Sentry
scrubber.

The application emits every log line to stdout, which is what Railway
captures. Structured JSON is the production format because Railway's log
browser can only search text — a line that already carries `status`,
`route` and `request_id` as discrete keys is searchable, whereas a prose
sentence is not.

Admin keys are stripped on the way out. An event admin key is a credential
that sits in the URL path, so it turns up in request lines and in the repr
of the request object Django attaches to its own log records. Redacting in
the formatter catches both without every call site having to remember.

IMPORTANT: the request ID is carried in a ContextVar rather than being
threaded through call sites. That is what lets Django's own `django.request`
logger — which knows nothing about this project — emit lines that correlate
with ours. A ContextVar rather than a thread-local so this keeps working if
the app ever moves to ASGI, where one thread serves many requests.
"""

import contextvars
import json
import logging
import re
from datetime import datetime, timezone

request_id_var = contextvars.ContextVar('request_id', default='')

# The logger RequestLogMiddleware writes its per-request line to. Kept
# separate from config.middleware so Sentry can be told to ignore it
# without also silencing deliberate logger.error() calls in that module.
REQUEST_LOG_LOGGER = 'config.middleware.requests'


# Attributes present on every LogRecord. Anything outside this set was passed
# by the caller via `extra=` and is therefore part of the payload we want.
_STANDARD_RECORD_ATTRS = frozenset({
    'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename',
    'funcName', 'levelname', 'levelno', 'lineno', 'message', 'module',
    'msecs', 'msg', 'name', 'pathname', 'process', 'processName',
    'relativeCreated', 'stack_info', 'taskName', 'thread', 'threadName',
    'request_id',
})


class RequestIdFilter(logging.Filter):
    """Stamps the current request's ID onto every record passing through.

    Falls back to the record's request object because Django logs a 4xx/5xx
    response from BaseHandler.get_response, which runs *after* the middleware
    chain has unwound and reset the ContextVar. Without the fallback the one
    line carrying the traceback would be the one line missing the ID needed
    to find it.
    """

    def filter(self, record):
        if hasattr(record, 'request_id'):
            return True

        request_id = request_id_var.get('')
        if not request_id:
            request_id = getattr(getattr(record, 'request', None), 'request_id', '')
        record.request_id = request_id
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with `extra=` keys promoted to top level."""

    def format(self, record):
        payload = {
            'ts': datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            'level': record.levelname,
            'logger': record.name,
            'msg': record.getMessage(),
        }

        request_id = getattr(record, 'request_id', '')
        if request_id:
            payload['request_id'] = request_id

        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith('_'):
                payload[key] = value

        if record.exc_info:
            payload['traceback'] = self.formatException(record.exc_info)
        if record.stack_info:
            payload['stack'] = self.formatStack(record.stack_info)

        # default=str so an unserialisable value in extra= degrades to its
        # repr instead of throwing inside the logging machinery, where the
        # exception would be swallowed and the line lost entirely.
        #
        # Redacting the serialised line rather than each value is deliberate:
        # Django attaches the request *object* to its own records, and its
        # repr carries the raw URL. Only default=str turns that into text, so
        # a per-value pass runs too early to see it. json.dumps does not
        # escape forward slashes, so the path survives intact for the regex.
        return redact_admin_key(json.dumps(payload, default=str))


class ConsoleFormatter(logging.Formatter):
    """Human-readable local-dev format. Renders a missing request ID as '-'.

    Restores the record afterwards: a LogRecord is shared by every handler
    it reaches, so a formatter that leaves its placeholder behind would put
    a literal '-' into the JSON output of any handler formatting it next.
    """

    def format(self, record):
        original = getattr(record, 'request_id', '')
        record.request_id = original or '-'
        try:
            return super().format(record)
        finally:
            record.request_id = original


# ---------------------------------------------------------------------------
# Sentry scrubbing
# ---------------------------------------------------------------------------

REDACTED = '[redacted]'

# Keys whose value is a credential. Matched case-insensitively at any depth,
# which covers POST data, query params, `extra`, and — the one that actually
# matters — the local variables Sentry captures from stack frames.
_SENSITIVE_KEYS = frozenset({
    'admin_key', 'password', 'csrfmiddlewaretoken', 'secret',
    'token', 'api_key', 'sessionid',
})

# Dropped outright rather than redacted; nothing in them aids debugging.
_DROP_KEYS = frozenset({
    'cookie', 'cookies', 'set-cookie', 'authorization', 'x-csrftoken',
})

# Literal segments that follow /admin/ in events/urls.py. Anything else in
# that position is an event's admin key.
_ADMIN_SUBROUTES = frozenset({
    'edit-driver', 'remove-driver', 'add-driver', 'create-stints',
    'save-details', 'save-calc', 'save-assignments', 'delete-event',
})

_ADMIN_PATH_RE = re.compile(r'(/admin/)([^/?#\s\'"]+)')

_MAX_SCRUB_DEPTH = 12


def _replace_admin_segment(match):
    prefix, segment = match.group(1), match.group(2)
    if segment in _ADMIN_SUBROUTES or segment.startswith('<'):
        return prefix + segment
    return prefix + REDACTED


def redact_admin_key(text):
    """
    Strip the admin key out of any URL embedded in `text`.

    An event's admin key is a live credential that grants full control of that
    event — /<event_id>/admin/<admin_key>/ is the entry point. It already has
    one documented residual exposure in Railway's access logs; it must not
    gain a second one in Sentry, where it would sit in issue titles, request
    URLs, breadcrumbs and the repr of any captured request object.
    """
    if not text or '/admin/' not in text:
        return text
    return _ADMIN_PATH_RE.sub(_replace_admin_segment, text)


# Used for EVENTS only. sentry_sdk serialises an event before calling
# before_send -- _prepare_event runs serialize() first, commented there as
# "Postprocess the event here so that annotated types do generally not
# surface in before_send" -- so every value reaching this function is
# already a primitive and handling strings is sufficient.
#
# Logs get no such treatment and must NOT use this function; see
# _scrub_attributes. Do not merge the two: that difference is the leak
# this module exists to prevent.
def _scrub(value, depth=0):
    if depth > _MAX_SCRUB_DEPTH:
        return value
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _DROP_KEYS:
                continue
            if lowered in _SENSITIVE_KEYS:
                cleaned[key] = REDACTED
            else:
                cleaned[key] = _scrub(item, depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_scrub(item, depth + 1) for item in value]
    if isinstance(value, str):
        return redact_admin_key(value)
    return value


def scrub_event(event, hint=None):
    """
    Sentry `before_send` hook.

    Walks the whole event rather than naming known fields. Sentry's payload
    shape changes between SDK versions and secrets turn up in places a field
    list would miss — stack-frame locals, breadcrumb data, the repr of a
    request object. A whole-event walk cannot be outgrown that way.

    Deliberately does NOT catch exceptions. sentry_sdk calls this inside
    capture_internal_exceptions(), so a hook that raises leaves new_event
    as None and the event is dropped. Catching here would replace that
    safe default with an unsafe one: the single case where redaction did
    not complete would become the case where the payload is sent anyway.
    """
    return _scrub(event)


def scrub_log(log, hint=None):
    """
    Sentry `before_send_log` hook.

    Sentry Logs are NOT covered by before_send. They arrive as a separate
    payload with its own callback, so scrub_event does nothing for them and
    the two hooks have to be registered independently.

    Both halves of the payload need it. `body` is the formatted message, and
    the raw message arguments are shipped individually as
    `sentry.message.parameter.N` attributes without passing through any
    logging formatter -- which is exactly where django.request puts the URL
    of a failing request, admin key and all.

    Deliberately does NOT catch exceptions, for the same reason as
    scrub_event: the SDK drops a log whose hook raises, and losing one log
    line costs far less than shipping a credential because the scrubber
    half-finished.
    """
    log['body'] = redact_admin_key(log.get('body') or '')
    attributes = log.get('attributes')
    if attributes:
        log['attributes'] = _scrub_attributes(attributes)
    return log


# Dropped from logs specifically. send_default_pii=False governs only what
# the SDK collects itself; custom attributes bypass it entirely, so the
# client IP RequestLogMiddleware records would otherwise be sent to Sentry
# on every forwarded line. Railway already sees the IP as the host --
# Sentry would be a new recipient, and this project deliberately keeps IPs
# out of Feedback for the same reason.
#
# `user` is deliberately kept: a Discord username is what makes 'someone
# said signup is broken' actionable, and ActivityLog already stores it.
_LOG_DROP_KEYS = frozenset({'ip'})


def _scrub_attributes(attributes):
    """
    Redact a Sentry log's attributes, flattening anything that is not a
    primitive first.

    The flattening is the security-relevant half. Sentry serialises attribute
    values with safe_repr *after* before_send_log has run, so a live object
    reaching this function unchanged gets stringified downstream where nothing
    can redact it -- which is precisely how django.request leaks a URL, by
    passing the request object itself as an attribute. Sentry only stores
    primitives anyway, so coercing here loses nothing.

    Two known limitations, neither reachable from this codebase today: a
    list of primitives would be stored by Sentry as a real array but is
    flattened to a repr here, and a sensitive key nested inside a
    flattened value survives as text instead of being redacted by name.
    Nothing emits list or nested attributes; revisit if anything starts to.
    """
    cleaned = {}
    for key, value in attributes.items():
        lowered = str(key).lower()
        if lowered in _DROP_KEYS or lowered in _LOG_DROP_KEYS:
            continue
        if lowered in _SENSITIVE_KEYS:
            cleaned[key] = REDACTED
        elif isinstance(value, str):
            cleaned[key] = redact_admin_key(value)
        elif isinstance(value, (bool, int, float)):
            cleaned[key] = value
        else:
            cleaned[key] = redact_admin_key(_safe_str(value))
    return cleaned


def _safe_str(value):
    """str() that cannot raise -- a __str__ that throws must not lose the log."""
    try:
        return str(value)
    except Exception:  # pragma: no cover - defensive
        return '<unrepresentable>'


def build_sentry_options(dsn, environment, release, traces_sample_rate,
                         logs_level):
    """
    The keyword arguments for sentry_sdk.init().

    Built here rather than inline in settings.py so the wiring is testable --
    the scrubbers are a security control, and a security control nobody can
    assert on is one that quietly stops being applied.
    """
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import (
        LoggingIntegration,
        ignore_logger,
    )

    level = _resolve_log_level(logs_level)

    # Stops the per-request line raising an Issue, without changing its
    # severity anywhere else. DjangoIntegration already reports the
    # exception behind a 5xx, so that line was only ever a duplicate of it.
    #
    # ignore_logger() covers events and breadcrumbs only -- the SDK keeps
    # _IGNORED_LOGGERS and _IGNORED_LOGGERS_SENTRY_LOGS as separate sets --
    # so the line still reaches Sentry Logs, still at ERROR for a 5xx.
    ignore_logger(REQUEST_LOG_LOGGER)

    logging_integration = LoggingIntegration(
        # Forward records to Sentry Logs at this level and above. A level of
        # None builds no handler at all, so "off" means off rather than
        # meaning "a handler exists but a second flag suppresses it".
        capture_sentry_logs=level is not None,
        sentry_logs_level=level,
        # Left at the SDK default rather than disabled. Disabling it also
        # killed every deliberate logger.error() in the codebase -- the
        # CANONICAL_HOST misconfiguration being the live example -- and
        # under SENTRY_LOGS_LEVEL=OFF those errors would then have reached
        # Sentry by no path at all. The duplicate issue is solved above, at
        # the one logger that actually caused it.
        event_level=logging.ERROR,
    )

    return {
        'dsn': dsn,
        'integrations': [DjangoIntegration(), logging_integration],
        'environment': environment,
        'release': release or None,
        'send_default_pii': False,
        'traces_sample_rate': traces_sample_rate,
        'before_send': scrub_event,
        'before_send_log': scrub_log,
    }


def _resolve_log_level(name):
    """Level number for a name, or None to disable log forwarding entirely."""
    cleaned = (name or '').strip().upper()
    if cleaned in ('OFF', 'NONE', 'DISABLED'):
        return None
    level = logging.getLevelNamesMapping().get(cleaned)
    # NOTSET is a real level name resolving to 0, which a handler reads as
    # 'forward everything' -- the whole request stream, and
    # django.db.backends too if it were ever turned up. It reaches the
    # loudest possible outcome through a correctly spelled value, so it
    # falls back exactly as a typo does.
    if not level:
        return logging.WARNING
    return level
