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

    Never raises: an exception here silently drops the event, which would
    hide the very error it was called to report.
    """
    try:
        return _scrub(event)
    except Exception:  # pragma: no cover - defensive
        return event
