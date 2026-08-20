"""
Naming and enrichment for ActivityLog rows.

The dashboard is only as useful as its labels, so action names are declared
here rather than derived from whatever a URL happens to be called this week.
A renamed URL pattern would otherwise split one feature's history into two
unrelated buckets and quietly break every month-over-month comparison.
"""

from django.db.models import Q

# (url_name, method) -> stable action label.
#
# Read the labels as a namespace: the prefix groups a feature, the suffix says
# what was done to it. Anything not listed falls back to '<url_name>.<method>',
# which is fine for pages but not for actions worth trending.
ACTION_NAMES = {
    ('home', 'GET'): 'page.home',
    ('event_create', 'GET'): 'page.event_create',
    ('event_create', 'POST'): 'event.create',
    ('view_event', 'GET'): 'page.view_event',

    ('signup', 'GET'): 'page.signup',
    ('signup', 'POST'): 'signup.submit',
    ('signup_edit', 'GET'): 'page.signup_edit',
    ('signup_edit', 'POST'): 'signup.edit',
    ('signup_success', 'GET'): 'page.signup_success',
    ('driver_delete', 'DELETE'): 'signup.delete',
    ('my_availability', 'GET'): 'page.my_availability',
    ('my_availability', 'POST'): 'signup.edit',

    ('admin_page', 'GET'): 'admin.enter_with_key',
    ('admin_dashboard', 'GET'): 'page.admin',
    ('admin_save_details', 'POST'): 'admin.save_details',
    ('admin_save_calc', 'POST'): 'admin.save_calc',
    ('admin_save_assignments', 'POST'): 'admin.save_assignments',
    ('admin_add_driver', 'POST'): 'admin.add_driver',
    ('admin_remove_driver', 'DELETE'): 'admin.remove_driver',
    ('admin_edit_driver_name', 'GET'): 'admin.edit_driver_form',
    ('admin_edit_driver_name', 'POST'): 'admin.edit_driver',
    ('admin_delete_event', 'POST'): 'admin.delete_event',

    ('set_stint_start', 'POST'): 'stint.set_start',
    ('reset_stint_start', 'POST'): 'stint.reset_start',

    ('set_timezone', 'POST'): 'prefs.set_timezone',
    ('feedback_submit', 'POST'): 'feedback.submit',
    ('client_error_report', 'POST'): 'client.error',
}

# Routes that write no row.
#
# event_search fires on every keystroke, so logging it would make the table
# mostly search noise and skew every per-action count. The two operator
# consoles are excluded so that reading the data does not change it.
EXCLUDED_URL_NAMES = frozenset({
    'event_search',
    'feedback_view',
    'activity_view',
    'healthz',
})

CLIENT_ERROR_ACTION = 'client.error'


def action_name(url_name, method):
    """Stable label for a request, or '' when the URL did not resolve."""
    if not url_name:
        return ''
    return ACTION_NAMES.get((url_name, method), f'{url_name}.{method.lower()}')


def log_detail(request, **fields):
    """
    Attach extra fields to this request's ActivityLog row.

    Call from a view when the fact worth recording is not visible from the
    outside — which fields failed validation, how many stints were saved.
    The middleware merges whatever has accumulated here when it writes the
    row, so several calls across one request are fine.
    """
    existing = getattr(request, 'activity_detail', None)
    if existing is None:
        existing = {}
        request.activity_detail = existing
    existing.update(fields)


CLIENT_ERROR_STATUS = 599

# What the dashboard counts as an error.
#
# A client error arrives as a successful POST to the reporting endpoint, so
# its row carries status 200 and a status-only filter would miss every
# JavaScript failure on the site — the errors least likely to be noticed
# any other way.
ERROR_FILTER = Q(status_code__gte=400) | Q(action=CLIENT_ERROR_ACTION)


def skip_activity(request):
    """
    Suppress this request's ActivityLog row.

    Used by rate-limited client error reports: without it a visitor who
    tripped the limit would keep writing rows that keep them over it, and
    they could never report again.
    """
    request.activity_skip = True
