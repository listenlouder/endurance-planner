import hmac
import json
import logging
import time
from collections import Counter
from datetime import date, datetime, time as time_type, timedelta, timezone as dt_utc
from zoneinfo import ZoneInfo

from django.conf import settings as django_settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q, Subquery, OuterRef
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.template.loader import render_to_string
from django.urls import reverse

from .activity import CLIENT_ERROR_ACTION, ERROR_FILTER, log_detail, skip_activity
from .forms import EventCreateForm
from .models import ActivityLog, Availability, Driver, Event, Feedback, StintAssignment
from .timezones import CURATED_TIMEZONES, VALID_TIMEZONES
from .utils import (
    build_stint_availability_matrix,
    collapse_slot_ranges,
    format_stint_duration,
    get_availability_slots,
    get_stint_windows,
    laps_remaining_after_stint,
    slot_grid_anchor,
    stint_length_seconds,
    total_race_laps,
    total_stints,
    validate_stint_sanity,
)

logger = logging.getLogger(__name__)

VALID_CONDITIONS = {c[0] for c in StintAssignment.CONDITION_CHOICES}


def _driver_has_conflict(driver, start_utc, driver_availability):
    """True if the driver has no availability slot covering start_utc's 30-min block."""
    slot_minute = (start_utc.minute // 30) * 30
    slot_key = start_utc.replace(minute=slot_minute, second=0, microsecond=0).astimezone(dt_utc.utc)
    return slot_key not in driver_availability.get(driver.id, set())


def _drivers_with_stale_availability(event):
    """
    Drivers whose recorded availability no longer fits the event's slot grid,
    and how much of it was stranded.

    Availability rows hold absolute UTC datetimes, so moving the event's date,
    start time or length can push them outside the new window.

    Reports any driver who lost at least one slot, not only those who lost
    everything: a driver left with one usable slot out of forty needs to
    re-enter just as much as one left with none. No threshold is applied —
    losing a single edge slot to a 30-minute nudge and losing the lot to a date
    change are both real, and only the admin knows which matters, so the counts
    are reported and the judgement left to them.

    Drivers who never entered availability are skipped; they have nothing to
    lose. Ordered by most lost first.
    """
    grid = {s.astimezone(dt_utc.utc) for s in get_availability_slots(event)}
    stale = []
    for driver in Driver.objects.filter(event=event).prefetch_related('availability'):
        stored = {a.slot_utc.astimezone(dt_utc.utc) for a in driver.availability.all()}
        if not stored:
            continue
        lost = stored - grid
        if lost:
            stale.append({
                'name': driver.name,
                'lost': len(lost),
                'total': len(stored),
            })
    stale.sort(key=lambda entry: (-entry['lost'], entry['name']))
    return stale


def _safe_json(data, **kwargs):
    """json.dumps safe for embedding in <script> blocks; prevents XSS via </script> injection."""
    return (
        json.dumps(data, **kwargs)
        .replace('&', '\\u0026')
        .replace('<', '\\u003c')
        .replace('>', '\\u003e')
    )


_TIMEZONE_LIST_JSON = _safe_json([
    {
        'region': group['region'],
        'zones': [{'label': z[0], 'value': z[1]} for z in group['zones']],
    }
    for group in CURATED_TIMEZONES
])


def normalize_iso(dt):
    """Convert a UTC-aware datetime to ISO 8601 string with Z suffix."""
    return dt.astimezone(dt_utc.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------------------------------------------------------------------------
# Admin auth and error pages
# ---------------------------------------------------------------------------

def _check_admin_key(event, admin_key):
    return hmac.compare_digest(str(admin_key), str(event.admin_key))


def require_admin_session(request, event_id):
    """
    Returns the Event if the session is valid, raises PermissionDenied
    if not. Use at the top of every admin sub-view.
    """
    if not request.session.get(f'admin_{event_id}'):
        raise PermissionDenied
    return get_object_or_404(Event, id=event_id)


def permission_denied_view(request, exception=None):
    return render(request, 'admin_error.html',
                  {'error': 'You do not have permission to access this page.',
                   'request_id': getattr(request, 'request_id', '')},
                  status=403)


def not_found_view(request, exception=None):
    return render(request, '404.html',
                  {'request_id': getattr(request, 'request_id', '')},
                  status=404)


def server_error_view(request):
    # The ID is printed on the page so a user reporting "it broke" hands over
    # the one string that finds the exact log line and Sentry issue. The
    # feedback widget picks the same value up automatically.
    return render(request, '500.html',
                  {'request_id': getattr(request, 'request_id', '')},
                  status=500)


def _build_availability_matrix(drivers, slots):
    """drivers must be prefetched with .prefetch_related('availability') by the caller."""
    matrix = {}
    all_covered = set()
    for driver in drivers:
        driver_slots = {a.slot_utc.astimezone(dt_utc.utc) for a in driver.availability.all()}
        matrix[driver.id] = driver_slots
        all_covered |= driver_slots
    normalized_slots = {s.astimezone(dt_utc.utc) for s in slots}
    uncovered = normalized_slots - all_covered
    return matrix, uncovered


MAX_UNCOVERED_WINDOWS_SHOWN = 6


def _uncovered_race_windows(event, slots, uncovered_slots, admin_tz):
    """
    Stretches of the race that no driver has marked themselves available for.

    Only the race itself counts. The slot grid also spans warmup and qualifying
    before the green flag, and an hour of buffer past it for races that run
    long — nobody has to be signed up for those, so gaps there are not gaps.

    Returns (windows, total_seconds). Each window is a dict with a `label`
    already rendered in admin_tz.
    """
    race_start = event.effective_start_datetime_utc
    race_end = event.effective_end_datetime_utc

    gaps = [
        slot for slot in slots
        if slot in uncovered_slots and race_start <= slot < race_end
    ]
    if not gaps:
        return [], 0

    safe_tz = admin_tz if admin_tz in VALID_TIMEZONES else 'UTC'
    zone = ZoneInfo(safe_tz)

    def _fmt(moment, with_day):
        local = moment.astimezone(zone)
        if with_day:
            return f"{local.strftime('%a')} {local.month}/{local.day} {local.strftime('%H:%M')}"
        return local.strftime('%H:%M')

    windows = []
    total_seconds = 0
    for start, end in collapse_slot_ranges(gaps):
        # Repeat the date on the end only when the window crosses midnight.
        crosses_day = start.astimezone(zone).date() != end.astimezone(zone).date()
        windows.append({
            'label': f"{_fmt(start, True)} – {_fmt(end, crosses_day)}",
            'seconds': int((end - start).total_seconds()),
        })
        total_seconds += int((end - start).total_seconds())

    return windows, total_seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hx_redirect(url):
    """Tell HTMX to navigate the whole page to url."""
    response = HttpResponse()
    response['HX-Redirect'] = url
    return response


def _redirect(request, url):
    """HX-Redirect for HTMX requests, a normal 302 for plain form posts."""
    return _hx_redirect(url) if request.htmx else redirect(url)


def _hx_refresh():
    """Tell HTMX to reload the page."""
    response = HttpResponse()
    response['HX-Refresh'] = 'true'
    return response


def _with_toast(response, message, error=False):
    """Fire the shared toast in base.html when this response lands."""
    toast = {'message': message}
    if error:
        toast['error'] = True
    response['HX-Trigger'] = json.dumps({'show-toast': toast})
    return response


def get_signup_context(event):
    """Returns shared context dict for signup and signup_edit views."""
    slots = get_availability_slots(event)
    return {
        'slots': slots,
        'slot_timestamps_json': _safe_json([normalize_iso(s) for s in slots]),
        'timezone_list_json': _TIMEZONE_LIST_JSON,
    }


def _save_availability(driver, slots_raw, event):
    """Create Availability records for a driver from a list of ISO timestamp strings."""
    valid_slot_set = {normalize_iso(s) for s in get_availability_slots(event)}
    objects = []
    for slot_str in slots_raw:
        if slot_str in valid_slot_set:
            objects.append(Availability(driver=driver, slot_utc=datetime.fromisoformat(slot_str.replace('Z', '+00:00'))))
    Availability.objects.bulk_create(objects)


def _validate_signup_post(post_data):
    """Validate the signup / edit-availability form. Returns (cleaned, errors)."""
    driver_name = post_data.get('driver_name', '').strip()
    timezone = post_data.get('timezone', '').strip()
    slots_raw = post_data.getlist('slots')

    errors = {}
    if not driver_name:
        errors['driver_name'] = 'Your name is required.'
    elif len(driver_name) > 50:
        errors['driver_name'] = 'Name must be 50 characters or fewer.'
    if not timezone:
        errors['timezone'] = 'Timezone is required.'
    if not slots_raw:
        errors['slots'] = 'Please select at least one availability slot.'
    # Checked last, and only when the form is otherwise valid, so a tampered
    # timezone does not crowd out the errors a real user needs to see.
    if not errors and timezone not in VALID_TIMEZONES:
        errors['timezone'] = 'Invalid timezone selected. Please try again.'

    return {'driver_name': driver_name, 'timezone': timezone, 'slots_raw': slots_raw}, errors


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

UPCOMING_EVENTS_SHOWN = 8


def _upcoming(events, now_utc):
    """
    The first UPCOMING_EVENTS_SHOWN events that have not started yet.

    `events` must already be filtered to date >= today and ordered by start.
    The date filter alone still lets through events earlier today, so the
    start time is checked here.
    """
    upcoming = []
    for event in events:
        if datetime.combine(event.date, event.start_time_utc, tzinfo=dt_utc.utc) > now_utc:
            upcoming.append(event)
            if len(upcoming) == UPCOMING_EVENTS_SHOWN:
                break
    return upcoming


def home(request):
    now_utc = datetime.now(tz=dt_utc.utc)

    recruiting_qs = Event.objects.filter(
        recruiting=True,
        date__gte=now_utc.date(),
    ).annotate(
        driver_count=Count('drivers'),
    ).order_by('date', 'start_time_utc')[:50]

    context = {'recruiting_events': _upcoming(recruiting_qs, now_utc)}

    if request.user.is_authenticated:
        driver_entries = Driver.objects.filter(
            event=OuterRef('pk'), user=request.user
        ).values('name')[:1]

        cutoff_date = (now_utc - timedelta(weeks=2)).date()

        context['admin_events'] = Event.objects.filter(
            created_by=request.user,
            date__gte=cutoff_date,
        ).order_by('-date')[:10]

        # Exclude events the user created — those already appear in admin_events
        context['driver_events'] = Event.objects.filter(
            drivers__user=request.user,
            date__gte=cutoff_date,
        ).exclude(
            created_by=request.user
        ).annotate(
            my_driver_name=Subquery(driver_entries)
        ).order_by('-date')[:10]

    return render(request, 'home.html', context)


def event_create(request):
    if request.method == 'POST':
        form = EventCreateForm(request.POST)
        if form.is_valid():
            event = Event(
                name=form.cleaned_data['name'],
                team_name=form.cleaned_data.get('team_name', ''),
                game=form.cleaned_data.get('game', ''),
                car=form.cleaned_data.get('car', ''),
                track=form.cleaned_data.get('track', ''),
                date=form.cleaned_data['date'],
                start_time_utc=form.cleaned_data['start_time_utc'],
                race_start_time_utc=form.cleaned_data.get('race_start_time_utc') or None,
                length_seconds=form.cleaned_data['length_seconds'],
                recruiting=form.cleaned_data.get('recruiting', False),
            )
            if request.user.is_authenticated:
                event.created_by = request.user
            event.save()
            log_detail(request, created_event=str(event.id),
                       authenticated=request.user.is_authenticated)
            base = request.build_absolute_uri('/').rstrip('/')
            success_ctx = {
                'success': True,
                'event': event,
                'base_url': request.build_absolute_uri('/'),
                'admin_url': f"{base}/{event.id}/admin/{event.admin_key}/",
                'signup_url': f"{base}/{event.id}/signup/",
                'view_url': f"{base}/{event.id}/view/",
            }
            if request.htmx:
                return render(request, 'partials/event_create_success.html', success_ctx)
            return render(request, 'event_create.html', success_ctx)
        log_detail(request, invalid_fields=sorted(form.errors))
        if request.htmx:
            return render(request, 'partials/event_create_form.html', {'form': form})
    else:
        form = EventCreateForm()
    return render(request, 'event_create.html', {'form': form})


def event_search(request):
    """
    HTMX endpoint. GET with 'q' parameter.
    Returns a dropdown partial of matching future events.
    Returns empty response if query is too short.
    """
    if not request.htmx:
        return HttpResponseBadRequest("HTMX requests only.")

    query = request.GET.get('q', '').strip()

    if len(query) < 2:
        return HttpResponse('')

    now_utc = datetime.now(tz=dt_utc.utc)

    results = Event.objects.filter(
        Q(name__icontains=query) |
        Q(track__icontains=query) |
        Q(car__icontains=query),
        date__gte=now_utc.date()
    ).order_by('date', 'start_time_utc')

    return render(request,
        'partials/search_results.html',
        {'results': _upcoming(results, now_utc), 'query': query})


def _stint_assignments(event):
    """{stint_number: StintAssignment} for an event, with drivers loaded."""
    return {
        sa.stint_number: sa
        for sa in StintAssignment.objects.filter(event=event).select_related('driver')
    }


def _availability_by_driver(drivers):
    """{driver_id: set of slot datetimes}. Drivers must have availability prefetched."""
    return {d.id: {a.slot_utc for a in d.availability.all()} for d in drivers}


def _stint_rows(event, assignments, driver_availability=None):
    """
    One dict per stint: the timing from get_stint_windows(), plus who drives
    it, the track condition, and whether a manually moved start now falls
    outside that driver's availability.

    This is the single place the view page, the live stint-edit endpoints and
    the admin page get stint rows from, so the conflict rule cannot drift
    between them.

    driver_availability is optional. When omitted it is loaded only if some
    stint has been moved, because only a moved stint can conflict.
    """
    if driver_availability is None:
        driver_availability = {}
        if any(sa.actual_start_utc for sa in assignments.values()):
            driver_availability = _availability_by_driver(
                Driver.objects.filter(event=event).prefetch_related('availability')
            )

    rows = []
    for window in get_stint_windows(event, assignment_overrides=assignments):
        assignment = assignments.get(window['stint_number'])
        driver = assignment.driver if assignment else None
        rows.append({
            **window,
            'driver': driver,
            'condition': assignment.condition if assignment else 'dry',
            'duration_display': format_stint_duration(window['duration_seconds']),
            'has_conflict': (
                driver and window['is_overridden']
                and _driver_has_conflict(driver, window['start_utc'], driver_availability)
            ),
        })
    return rows


def _stint_row_json(event, row):
    """The shape the view page's Alpine stint table consumes."""
    return {
        'stint_number':     row['stint_number'],
        'start_utc':        normalize_iso(row['start_utc']),
        'end_utc':          normalize_iso(row['end_utc']),
        'driver_name':      row['driver'].name if row['driver'] else None,
        'duration_display': row['duration_display'],
        'is_last_stint':    row['is_last'],
        'laps_remaining':   (
            None if row['is_last']
            else laps_remaining_after_stint(event, row['stint_number'])
        ),
        'condition':        row['condition'],
        'is_overridden':    row['is_overridden'],
        'has_conflict':     row['has_conflict'],
    }


def _length_display(length_seconds):
    """6h, 6h 30m."""
    hours = length_seconds // 3600
    minutes = (length_seconds % 3600) // 60
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def view_event(request, event_id):
    """Public view-only page. No authentication required."""
    event = get_object_or_404(Event, id=event_id)
    stints_ready = event.has_required_stint_fields

    assignments = _stint_assignments(event)
    stint_rows = _stint_rows(event, assignments)

    user_driver = None
    if request.user.is_authenticated:
        try:
            user_driver = Driver.objects.get(event=event, user=request.user)
        except Driver.DoesNotExist:
            pass

    stint_duration_display = None
    target_lap_time = None
    if stints_ready:
        stint_duration_display = format_stint_duration(stint_length_seconds(event))
        if event.avg_lap_seconds:
            avg = int(round(event.avg_lap_seconds))
            target_lap_time = f"{avg // 60}:{avg % 60:02d}"

    # Stints per driver, for the summary pills.
    driver_counts = dict(Counter(
        row['driver'].name for row in stint_rows if row['driver']
    ))

    max_laps_on_fuel = None
    if stints_ready and event.fuel_capacity and event.fuel_per_lap:
        max_laps_on_fuel = int(event.fuel_capacity / event.fuel_per_lap)

    return render(request, 'view.html', {
        'event': event,
        'stint_rows': stint_rows,
        'stint_rows_json': _safe_json([_stint_row_json(event, row) for row in stint_rows]),
        'has_stints': bool(assignments),
        'stints_ready': stints_ready,
        'can_edit_stints': request.user.is_authenticated,
        'length_display': _length_display(event.length_seconds),
        'has_unassigned': StintAssignment.objects.filter(event=event, driver=None).exists(),
        'user_driver': user_driver,
        'show_signup_link': request.GET.get('from') == 'recruiting',
        'stint_duration_display': stint_duration_display,
        'target_lap_time': target_lap_time,
        'total_stints_display': total_stints(event) if stints_ready else None,
        'max_laps_on_fuel': max_laps_on_fuel,
        'driver_counts': driver_counts,
        'total_race_laps': total_race_laps(event),
        'has_separate_race_start': bool(event.race_start_time_utc),
        'effective_start_datetime_utc': event.effective_start_datetime_utc,
    })


# ---------------------------------------------------------------------------
# Live stint time editing
# ---------------------------------------------------------------------------

def _stint_rows_response(event):
    """Recomputed stint rows after a start-time override changed."""
    rows = _stint_rows(event, _stint_assignments(event))
    return JsonResponse({'stint_rows': [_stint_row_json(event, row) for row in rows]})


def set_stint_start(request, event_id, stint_number):
    """POST. Sets actual_start_utc on a StintAssignment. Requires Discord auth."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    if not request.user.is_authenticated:
        return HttpResponseForbidden()

    event = get_object_or_404(Event, id=event_id)
    if not event.has_required_stint_fields:
        return HttpResponseBadRequest('Stint fields not configured.')

    raw = request.POST.get('actual_start_utc', '').strip()
    if not raw:
        return HttpResponseBadRequest('actual_start_utc is required.')

    try:
        parsed_dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=dt_utc.utc)
    except ValueError:
        return HttpResponseBadRequest('Invalid datetime format.')

    assignment, _ = StintAssignment.objects.get_or_create(
        event=event,
        stint_number=stint_number,
        defaults={'driver': None, 'condition': 'dry'},
    )
    assignment.actual_start_utc = parsed_dt
    assignment.save(update_fields=['actual_start_utc'])

    return _stint_rows_response(event)


def reset_stint_start(request, event_id, stint_number):
    """POST. Clears actual_start_utc override. Requires Discord auth."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    if not request.user.is_authenticated:
        return HttpResponseForbidden()

    event = get_object_or_404(Event, id=event_id)

    try:
        assignment = StintAssignment.objects.get(event=event, stint_number=stint_number)
        assignment.actual_start_utc = None
        assignment.save(update_fields=['actual_start_utc'])
    except StintAssignment.DoesNotExist:
        pass

    return _stint_rows_response(event)


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------

def _render_signup(request, page_template, ctx):
    """The whole page, or just the form partial when HTMX swaps it in place."""
    if request.htmx:
        return render(request, 'partials/signup_form.html', ctx)
    return render(request, page_template, ctx)


def signup(request, event_id):
    """GET: render signup form. POST: validate and save driver + availability."""
    event = get_object_or_404(Event, id=event_id)
    prefill_name = ''
    if request.user.is_authenticated:
        prefill_name = request.user.discord_username or request.user.username
    ctx = {**get_signup_context(event), 'event': event, 'prefill_name': prefill_name}

    if request.method != 'POST':
        return render(request, 'signup.html', {**ctx, 'initial_slot_timestamps': []})

    cleaned, errors = _validate_signup_post(request.POST)
    if errors:
        # The single most useful friction signal on the site: which field
        # turns people away from the form they came here to fill in.
        log_detail(request, invalid_fields=sorted(errors))
        return _render_signup(request, 'signup.html', {
            **ctx,
            'errors': errors,
            'submitted_name': cleaned['driver_name'],
            'initial_slot_timestamps': cleaned['slots_raw'],
            'submitted_timezone': cleaned['timezone'],
        })

    driver = Driver.objects.create(
        event=event,
        name=cleaned['driver_name'],
        timezone=cleaned['timezone'],
    )
    if request.user.is_authenticated:
        driver.user = request.user
        driver.save(update_fields=['user'])
    _save_availability(driver, cleaned['slots_raw'], event)
    log_detail(request, slots=len(cleaned['slots_raw']),
               authenticated=request.user.is_authenticated)
    return _redirect(request, reverse(
        'signup_success', kwargs={'event_id': event_id, 'driver_id': driver.id}
    ))


def signup_edit(request, event_id, driver_id):
    """GET: edit form pre-populated. POST: replace availability."""
    event = get_object_or_404(Event, id=event_id)
    driver = get_object_or_404(Driver.objects.prefetch_related('availability'), id=driver_id, event=event)
    from_admin = request.GET.get('from') == 'admin'
    ctx = {
        **get_signup_context(event),
        'event': event,
        'driver': driver,
        'prefill_name': driver.name,
        'from_admin': from_admin,
    }

    if request.method != 'POST':
        return render(request, 'signup_edit.html', {
            **ctx,
            'initial_slot_timestamps': [normalize_iso(a.slot_utc) for a in driver.availability.all()],
        })

    cleaned, errors = _validate_signup_post(request.POST)
    if errors:
        log_detail(request, invalid_fields=sorted(errors))
        return _render_signup(request, 'signup_edit.html', {
            **ctx,
            'errors': errors,
            'submitted_name': cleaned['driver_name'],
            'initial_slot_timestamps': cleaned['slots_raw'],
            'submitted_timezone': cleaned['timezone'],
        })

    with transaction.atomic():
        driver.name = cleaned['driver_name']
        driver.timezone = cleaned['timezone']
        driver.save()
        driver.availability.all().delete()
        _save_availability(driver, cleaned['slots_raw'], event)
    log_detail(request, slots=len(cleaned['slots_raw']), from_admin=from_admin)

    if from_admin:
        return _redirect(request, reverse('admin_dashboard', kwargs={'event_id': event_id}))
    return _redirect(request, reverse(
        'signup_success', kwargs={'event_id': event_id, 'driver_id': driver_id}
    ))


def signup_success(request, event_id, driver_id):
    event = get_object_or_404(Event, id=event_id)
    driver = get_object_or_404(Driver, id=driver_id, event=event)
    base = request.build_absolute_uri('/').rstrip('/')
    return render(request, 'signup_success.html', {
        'event': event,
        'driver': driver,
        'updated': request.GET.get('updated') == '1',
        'view_url': f"{base}/{event.id}/view/",
        'edit_url': f"{base}/{event.id}/signup/{driver.id}/edit/",
    })


def driver_delete(request, event_id, driver_id):
    """DELETE. Allowed for the driver (via Discord) or an event admin."""
    if request.method != 'DELETE':
        return HttpResponseNotAllowed(['DELETE'])

    event = get_object_or_404(Event, id=event_id)
    driver = get_object_or_404(Driver, id=driver_id, event=event)

    is_owner = request.user.is_authenticated and driver.user == request.user
    is_admin = request.session.get(f'admin_{event_id}')
    if not is_owner and not is_admin:
        raise PermissionDenied

    driver.delete()
    return _hx_redirect('/')


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

def set_timezone(request):
    """POST. Stores the admin's display timezone; refreshes the page if it changed."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    tz = request.POST.get('timezone', 'UTC')
    if tz not in VALID_TIMEZONES:
        tz = 'UTC'
    current_tz = request.COOKIES.get('admin_timezone', 'UTC')
    response = HttpResponse()
    # httponly=False required: Alpine.js reads this cookie client-side
    response.set_cookie('admin_timezone', tz, samesite='Lax', httponly=False)
    if current_tz != tz:
        response['HX-Refresh'] = 'true'
    return response


def _admin_timezone(request):
    """The admin's chosen display timezone, from the set-timezone cookie."""
    admin_tz = request.COOKIES.get('admin_timezone', 'UTC')
    return admin_tz if admin_tz in VALID_TIMEZONES else 'UTC'


def _admin_drivers(event):
    """Drivers as the admin driver list shows them, availability prefetched."""
    return (
        Driver.objects.filter(event=event)
        .prefetch_related('availability')
        .annotate(stint_count=Count('stint_assignments'))
        .order_by('signed_up_at')
    )


def _build_admin_context(request, event):
    """Build the context dict for the admin page. Caller must have already verified auth."""
    drivers = _admin_drivers(event)
    slots = get_availability_slots(event)
    availability_matrix, uncovered_slots = _build_availability_matrix(drivers, slots)
    admin_tz = _admin_timezone(request)
    uncovered_windows, uncovered_seconds = _uncovered_race_windows(
        event, slots, uncovered_slots, admin_tz
    )
    stints_ready = event.has_required_stint_fields

    # Everything stint-related is empty until the stint calc fields are filled
    # in — including has_assignments, even if assignment rows exist.
    assignments = _stint_assignments(event) if stints_ready else {}
    stint_rows = _stint_rows(event, assignments, _availability_by_driver(drivers))
    zone = ZoneInfo(admin_tz)
    for row in stint_rows:
        row['start_local'] = row['start_utc'].astimezone(zone).strftime('%H:%M')

    stint_availability_matrix = {}
    availability_json = {}
    if stints_ready:
        stint_availability_matrix = build_stint_availability_matrix(
            drivers, stint_rows, slot_grid_anchor(event)
        )
        availability_json = {
            str(driver.id): [normalize_iso(a.slot_utc) for a in driver.availability.all()]
            for driver in drivers
        }

    conditions = {n: sa.condition for n, sa in assignments.items()}
    total_seconds = event.length_seconds or 0
    base = request.build_absolute_uri('/').rstrip('/')

    return {
        'event': event,
        'admin_tz': admin_tz,
        'drivers': drivers,
        'availability_matrix': availability_matrix,
        'sanity_warnings': validate_stint_sanity(event),
        # Drives the pre-save warning when the admin edits date or start time.
        # `drivers` is already prefetched, so this costs no extra query.
        'drivers_with_availability_count': sum(1 for d in drivers if d.availability.all()),
        # The client-side conflict and dimming helpers snap stint times onto the
        # availability grid themselves, so they must start from the same floored
        # origin the server uses — not event.start_datetime_utc.
        'slot_grid_anchor': slot_grid_anchor(event),
        'uncovered_race_windows': uncovered_windows[:MAX_UNCOVERED_WINDOWS_SHOWN],
        'uncovered_race_windows_extra': max(0, len(uncovered_windows) - MAX_UNCOVERED_WINDOWS_SHOWN),
        'uncovered_race_seconds': uncovered_seconds,
        'timezone_list_json': _TIMEZONE_LIST_JSON,
        'slot_timestamps_json': _safe_json([normalize_iso(s) for s in slots]),
        'slots': slots,
        'signup_url': f"{base}/{event.id}/signup/",
        'length_hours_display': total_seconds // 3600,
        'length_minutes_display': (total_seconds % 3600) // 60,
        'has_required_stint_fields': stints_ready,
        'stint_windows': stint_rows,
        'stint_availability': stint_availability_matrix,
        'stint_duration_map': {row['stint_number']: row['duration_display'] for row in stint_rows},
        'stint_windows_json': _safe_json([{
            'stint_number': row['stint_number'],
            'start_utc': normalize_iso(row['start_utc']),
            'end_utc': normalize_iso(row['end_utc']),
            'is_last_stint': row['is_last'],
            'is_overridden': row['is_overridden'],
            'laps_remaining': (
                None
                if row['is_last']
                else laps_remaining_after_stint(event, row['stint_number'])
            ),
        } for row in stint_rows]),
        'conditions': conditions,
        'existing_assignments_json': _safe_json({
            str(n): str(sa.driver_id) for n, sa in assignments.items() if sa.driver_id
        }),
        'existing_conditions_json': _safe_json({str(n): c for n, c in conditions.items()}),
        'drivers_json': _safe_json([{'id': str(d.id), 'name': d.name, 'timezone': d.timezone or 'UTC'} for d in drivers]),
        'availability_json': _safe_json(availability_json),
        'stint_meta': {
            row['stint_number']: {
                'is_overridden': row['is_overridden'],
                'has_conflict': row['has_conflict'],
            }
            for row in stint_rows
        },
        'has_assignments': bool(assignments),
        'stint_availability_json': _safe_json({
            str(driver_id): {str(stint_num): status for stint_num, status in stints.items()}
            for driver_id, stints in stint_availability_matrix.items()
        }),
    }


def _grant_admin_session(request, event_id):
    """
    Mark the session as admin for this event, rotating the session ID on the
    transition to guard against session fixation.

    Only rotates when the flag is not already set. cycle_key() deletes the old
    session row, so calling it on every request breaks any other request or tab
    still holding the previous key — which reads to the user as being randomly
    logged out.
    """
    session_key = f'admin_{event_id}'
    if request.session.get(session_key):
        return
    request.session.cycle_key()
    request.session[session_key] = True


def admin_page(request, event_id, admin_key):
    event = get_object_or_404(Event, id=event_id)

    if not _check_admin_key(event, admin_key):
        return render(request, 'admin_error.html', {'error': 'Invalid admin key supplied.'})

    _grant_admin_session(request, event_id)

    # Redirect to key-less URL so the admin key only appears in logs
    # once (as this 302) rather than on every subsequent page visit.
    return redirect('admin_dashboard', event_id=event_id)


def admin_dashboard(request, event_id):
    """Admin page — Discord owner bypass or session-authenticated."""
    if request.user.is_authenticated:
        event = get_object_or_404(Event, id=event_id)
        if event.created_by == request.user:
            _grant_admin_session(request, event_id)
            return render(request, 'admin.html', _build_admin_context(request, event))
        # Authenticated as non-owner — fall back to session
        if request.session.get(f'admin_{event_id}'):
            return render(request, 'admin.html', _build_admin_context(request, event))
        return render(request, 'admin_error.html',
                      {'error': 'You do not have admin access to this event.'})

    # Not authenticated — require session or redirect to Discord login
    if not request.session.get(f'admin_{event_id}'):
        next_url = reverse('admin_dashboard', kwargs={'event_id': event_id})
        return redirect(f"{reverse('discord_login')}?next={next_url}")
    event = get_object_or_404(Event, id=event_id)
    return render(request, 'admin.html', _build_admin_context(request, event))


def my_availability(request, event_id):
    """Driver edit page for Discord-authenticated users — no edit URL needed."""
    if not request.user.is_authenticated:
        next_url = reverse('my_availability', kwargs={'event_id': event_id})
        return redirect(f"{reverse('discord_login')}?next={next_url}")

    event = get_object_or_404(Event, id=event_id)

    driver = Driver.objects.filter(event=event, user=request.user).first()
    if driver is None:
        return redirect('signup', event_id=event_id)

    return signup_edit(request, event_id, driver.id)


def admin_edit_driver_name(request, event_id, driver_id):
    """
    GET: return an inline edit form for the driver's name
    GET with cancel=1: return the display partial
    POST: validate and save the new name, return display partial
    """
    event = require_admin_session(request, event_id)
    driver = get_object_or_404(Driver, id=driver_id, event=event)

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        error = None
        if not name:
            error = 'Driver name cannot be empty.'
        elif len(name) > 50:
            error = 'Name must be 50 characters or fewer.'
        if error:
            return render(request,
                'partials/driver_name_edit_form.html',
                {'driver': driver, 'event': event, 'error': error})
        driver.name = name
        driver.save(update_fields=['name'])
        return render(request,
            'partials/driver_name_display.html',
            {'driver': driver, 'event': event})

    # GET
    if request.GET.get('cancel'):
        return render(request,
            'partials/driver_name_display.html',
            {'driver': driver, 'event': event})

    return render(request,
        'partials/driver_name_edit_form.html',
        {'driver': driver, 'event': event})


def admin_remove_driver(request, event_id, driver_id):
    """DELETE. Removes the row in place, or reloads if the stint table depends on it."""
    if request.method != 'DELETE':
        return HttpResponseNotAllowed(['DELETE'])
    event = require_admin_session(request, event_id)
    Driver.objects.filter(id=driver_id, event=event).delete()
    if event.has_required_stint_fields:
        return _hx_refresh()
    response = HttpResponse()
    response['HX-Reswap'] = 'delete'
    return response


def admin_add_driver(request, event_id):
    """POST only. Creates a driver with name, timezone, and optional availability."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    event = require_admin_session(request, event_id)

    driver_name = request.POST.get('driver_name', '').strip()
    timezone = request.POST.get('timezone', '').strip()
    slots_raw = request.POST.getlist('slots')

    errors = {}
    if not driver_name:
        errors['driver_name'] = 'Driver name is required.'
    elif len(driver_name) > 50:
        errors['driver_name'] = 'Name must be 50 characters or fewer.'
    if not timezone or timezone not in VALID_TIMEZONES:
        errors['timezone'] = 'A valid timezone is required.'

    if errors:
        # The form's success target is the driver list, so retarget the error
        # swap at the form's own error slot instead of replacing that list.
        response = render(request, 'partials/form_errors.html', {'errors': errors}, status=422)
        response['HX-Retarget'] = '#add-driver-errors'
        response['HX-Reswap'] = 'innerHTML'
        return response

    driver = Driver.objects.create(event=event, name=driver_name, timezone=timezone)
    if slots_raw:
        _save_availability(driver, slots_raw, event)

    if event.has_required_stint_fields:
        return _hx_refresh()

    # Stints not configured yet, so only the driver list needs redrawing.
    drivers = _admin_drivers(event)
    slots = get_availability_slots(event)
    availability_matrix, _ = _build_availability_matrix(drivers, slots)
    driver_list_html = render_to_string(
        'partials/driver_list.html',
        {
            'drivers': drivers,
            'event': event,
            'admin_tz': _admin_timezone(request),
            'availability_matrix': availability_matrix,
            'slots': slots,
        },
        request=request,
    )
    return _with_toast(HttpResponse(driver_list_html), 'Driver added.')


def admin_save_details(request, event_id):
    """POST. Saves all event detail fields at once."""
    event = require_admin_session(request, event_id)
    if request.method != 'POST':
        return HttpResponseBadRequest()

    original_date = event.date
    original_start_time = event.start_time_utc
    original_length_seconds = event.length_seconds

    errors = {}

    name = request.POST.get('name', '').strip()
    if not name:
        errors['name'] = 'Event name is required.'

    date_str = request.POST.get('date', '').strip()
    try:
        parsed_date = date.fromisoformat(date_str)
    except ValueError:
        errors['date'] = 'Invalid date.'
        parsed_date = None

    start_time_str = request.POST.get('start_time_utc', '').strip()
    try:
        parsed_time = time_type.fromisoformat(start_time_str)
    except ValueError:
        errors['start_time_utc'] = 'Invalid time.'
        parsed_time = None

    try:
        length_hours = int(request.POST.get('length_hours', 0) or 0)
        length_minutes = int(request.POST.get('length_minutes', 0) or 0)
        if length_hours == 0 and length_minutes == 0:
            errors['length'] = 'Race length must be greater than zero.'
        length_seconds = (length_hours * 3600) + (length_minutes * 60)
    except (ValueError, TypeError):
        errors['length'] = 'Invalid race length.'
        length_seconds = None

    if errors:
        log_detail(request, invalid_fields=sorted(errors))
        return render(request, 'partials/form_errors.html', {'errors': errors}, status=422)

    event.name = name
    if parsed_date:
        event.date = parsed_date
    if parsed_time:
        event.start_time_utc = parsed_time
    if length_seconds:
        event.length_seconds = length_seconds
    event.team_name = request.POST.get('team_name', '').strip()
    event.game = request.POST.get('game', '').strip()
    event.car = request.POST.get('car', '').strip()
    event.track = request.POST.get('track', '').strip()
    event.setup = request.POST.get('setup', '').strip()
    event.recruiting = (request.POST.get('recruiting') == 'on')

    # Length counts too: shortening a race truncates the tail of the slot grid
    # and strands availability just as surely as moving the start.
    schedule_moved = (
        event.date != original_date
        or event.start_time_utc != original_start_time
        or event.length_seconds != original_length_seconds
    )
    event.save()

    # Availability is stored as absolute UTC slots, so moving the event's date
    # or start time can leave a driver's answers outside the new window
    # entirely. That is intended — they need to re-enter it — but it must not
    # happen silently.
    stale_drivers = _drivers_with_stale_availability(event) if schedule_moved else []

    log_detail(request, schedule_moved=schedule_moved,
               stranded_drivers=len(stale_drivers))

    if stale_drivers:
        response = render(
            request,
            'partials/availability_warning.html',
            {'stale_drivers': stale_drivers, 'event': event},
        )
        return _with_toast(
            response,
            f'Saved — {len(stale_drivers)} driver(s) lost availability slots.',
            error=True,
        )

    return _with_toast(HttpResponse(), 'Event details saved.')


def admin_save_calc(request, event_id):
    """POST. Saves stint calculation fields."""
    event = require_admin_session(request, event_id)
    if request.method != 'POST':
        return HttpResponseBadRequest()

    errors = {}

    # model field -> form field name
    lap_fields = {
        'avg_lap_seconds': 'avg_lap',
        'in_lap_seconds': 'in_lap',
        'out_lap_seconds': 'out_lap',
    }
    numeric_fields = {
        'fuel_capacity': 'fuel_capacity',
        'fuel_per_lap': 'fuel_burn',
    }

    def parse_mmss(val):
        parts = val.strip().split(':')
        if len(parts) != 2:
            raise ValueError
        m, s = int(parts[0]), int(parts[1])
        if not (0 <= s < 60):
            raise ValueError
        return (m * 60) + s

    for model_field, post_name in lap_fields.items():
        raw = request.POST.get(post_name, '').strip()
        if raw:
            try:
                setattr(event, model_field, parse_mmss(raw))
            except (ValueError, IndexError):
                errors[model_field] = 'Use MM:SS format (e.g. 2:18)'

    for model_field, post_name in numeric_fields.items():
        raw = request.POST.get(post_name, '').strip()
        if raw:
            try:
                setattr(event, model_field, float(raw))
            except ValueError:
                errors[model_field] = 'Invalid number.'

    raw_laps = request.POST.get('target_laps', '').strip()
    if raw_laps:
        try:
            laps_val = float(raw_laps)
            if laps_val != int(laps_val):
                errors['target_laps'] = 'Target laps must be a whole number.'
            else:
                event.target_laps = int(laps_val)
        except ValueError:
            errors['target_laps'] = 'Invalid number.'

    raw_race_start = request.POST.get('race_start_time_utc', '').strip()
    race_start_submitted = 'race_start_time_utc' in request.POST
    parsed_race_start = None
    if raw_race_start:
        try:
            parsed_race_start = time_type.fromisoformat(raw_race_start)
        except ValueError:
            errors['race_start_time_utc'] = 'Invalid race start time.'

    if errors:
        log_detail(request, invalid_fields=sorted(errors))
        return render(request, 'partials/form_errors.html', {'errors': errors}, status=422)

    if race_start_submitted:
        event.race_start_time_utc = parsed_race_start
    event.save()
    log_detail(request, stint_fields_complete=event.has_required_stint_fields)
    if event.has_required_stint_fields:
        # The whole stint table appears once the calc is complete.
        return _hx_refresh()
    return _with_toast(HttpResponse(), 'Stint calc saved.')


def create_stints_redirect(request, event_id):
    """Legacy URL: stint assignment now lives on the admin dashboard."""
    require_admin_session(request, event_id)
    return redirect('admin_dashboard', event_id=event_id)


def admin_delete_event(request, event_id):
    """POST only. Validates admin session, confirms 'DELETE' typed, deletes event."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    event = require_admin_session(request, event_id)

    submitted_name = request.POST.get('confirm_name', '').strip()
    if submitted_name != 'DELETE':
        log_detail(request, confirmed=False)
        messages.error(request, 'Confirmation did not match. Deletion cancelled.')
        return redirect('admin_dashboard', event_id=event_id)

    session_key = f'admin_{event_id}'
    if session_key in request.session:
        del request.session[session_key]

    event_name = event.name
    # Recorded before the delete: event_id_ref is a bare UUID precisely so
    # this row outlives the event it describes.
    log_detail(request, confirmed=True, event_name=event_name)
    event.delete()

    messages.success(request, f'"{event_name}" has been permanently deleted.')
    return redirect('home')


def admin_save_assignments(request, event_id):
    """POST. Saves all stint driver assignments and conditions at once."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    event = require_admin_session(request, event_id)

    if not event.has_required_stint_fields:
        return HttpResponseBadRequest()

    # Preserve existing overrides (actual_start_utc) before bulk delete
    existing = {
        sa.stint_number: sa
        for sa in StintAssignment.objects.filter(event=event)
    }

    stint_windows = get_stint_windows(event)

    assignments_to_create = []
    for sw in stint_windows:
        n = sw['stint_number']
        prior = existing.get(n)

        driver_id = request.POST.get(f'stint_{n}', '').strip()
        driver = None
        if driver_id:
            try:
                driver = Driver.objects.get(id=driver_id, event=event)
            except (Driver.DoesNotExist, ValueError):
                logger.warning(
                    "admin_save_assignments: driver_id %r not found for event %s — stint %d unassigned",
                    driver_id, event_id, n,
                )
        raw_condition = request.POST.get(f'condition_{n}', '').strip()
        condition = raw_condition if raw_condition in VALID_CONDITIONS else (
            prior.condition if prior else 'dry'
        )
        assignments_to_create.append(
            StintAssignment(
                event=event,
                stint_number=n,
                driver=driver,
                condition=condition,
                actual_start_utc=prior.actual_start_utc if prior else None,
            )
        )

    with transaction.atomic():
        StintAssignment.objects.filter(event=event).delete()
        StintAssignment.objects.bulk_create(assignments_to_create)

    log_detail(
        request,
        stints=len(assignments_to_create),
        unassigned=sum(1 for a in assignments_to_create if a.driver_id is None),
    )

    return _with_toast(HttpResponse(), 'Assignments saved.')


# ---------------------------------------------------------------------------
# Feedback views
# ---------------------------------------------------------------------------


def feedback_submit(request):
    """
    HTMX POST only. Saves feedback to the database.
    Returns a success or error partial.
    """
    if request.method != 'POST':
        return HttpResponseBadRequest()

    text = request.POST.get('text', '').strip()
    page_url = request.POST.get('page_url', '').strip()[:500]
    user_agent = request.POST.get('user_agent', '').strip()[:500]
    # base.html sends the ID of the last request this browser saw answered,
    # so a report filed right after a failure points at that failure.
    last_request_id = request.META.get('HTTP_X_LAST_REQUEST_ID', '').strip()[:32]

    if not text:
        return HttpResponse(
            '<p class="text-red-500 text-xs mt-1">'
            'Please enter some feedback before submitting.'
            '</p>'
        )

    if len(text) > 1000:
        return HttpResponse(
            '<p class="text-red-500 text-xs mt-1">'
            'Feedback must be under 1000 characters.'
            '</p>'
        )

    Feedback.objects.create(
        text=text,
        page_url=page_url,
        user_agent=user_agent,
        request_id=last_request_id,
    )

    response = HttpResponse('')
    # Kebab-case to match the listener. HTML attribute names are lowercased by
    # the parser, so an Alpine @feedbackSuccess.window listener could never
    # match a camelCase event name.
    response['HX-Trigger'] = 'feedback-success'
    return response


def _password_gate(request, session_key, template, url_name):
    """
    The sign-in shared by the two operator consoles, /feedback/view/ and
    /activity/view/. Both use FEEDBACK_PASSWORD; each keeps its own session
    flag so signing in to one does not sign in to the other.

    Returns the response to send while the visitor is not signed in — the
    password prompt, a failed attempt, or the redirect after logging out —
    and None once they are.
    """
    if request.GET.get('logout'):
        request.session.pop(session_key, None)
        return redirect(url_name)

    if request.method == 'POST':
        password = request.POST.get('password', '')
        if (django_settings.FEEDBACK_PASSWORD
                and hmac.compare_digest(password, django_settings.FEEDBACK_PASSWORD)):
            request.session[session_key] = True
        else:
            time.sleep(1)
            return render(request, template, {
                'authenticated': False,
                'error': 'Incorrect password.',
            })

    if not request.session.get(session_key):
        return render(request, template, {'authenticated': False})
    return None


def feedback_view(request):
    """Password-protected feedback viewer. See _password_gate for sign-in."""
    gate = _password_gate(request, 'feedback_authenticated', 'feedback_view.html', 'feedback_view')
    if gate is not None:
        return gate

    feedback_items = list(Feedback.objects.all()[:200])
    return render(request, 'feedback_view.html', {
        'authenticated': True,
        'feedback_items': feedback_items,
        'total': len(feedback_items),
    })


# ---------------------------------------------------------------------------
# Activity dashboard
# ---------------------------------------------------------------------------

ACTIVITY_WINDOWS = (1, 7, 30, 90)
ACTIVITY_DEFAULT_DAYS = 7
ACTIVITY_TRAIL_LIMIT = 200
ACTIVITY_ERROR_LIMIT = 50


def _activity_window_days(request):
    """Requested window in days, clamped to the offered set."""
    try:
        days = int(request.GET.get('days', ACTIVITY_DEFAULT_DAYS))
    except (TypeError, ValueError):
        return ACTIVITY_DEFAULT_DAYS
    return days if days in ACTIVITY_WINDOWS else ACTIVITY_DEFAULT_DAYS


def _activity_trail(request):
    """
    The visitor trail to display, if one was asked for.

    Accepts either a visitor ID or a request ID — the latter because a request
    ID is what a user quotes from an error page or a feedback report, and the
    first thing worth seeing is everything else that visitor did around it.
    """
    visitor_id = request.GET.get('visitor', '').strip()[:32]
    request_id = request.GET.get('request', '').strip()[:32]

    if request_id and not visitor_id:
        anchor = ActivityLog.objects.filter(request_id=request_id).first()
        if anchor is None:
            return '', request_id, []
        visitor_id = anchor.visitor_id

    if not visitor_id:
        return '', request_id, []

    recent = list(
        ActivityLog.objects
        .filter(visitor_id=visitor_id)
        .select_related('user')[:ACTIVITY_TRAIL_LIMIT]
    )
    # Sliced newest-first so the limit keeps the most recent activity, then
    # reversed: a trail only reads as a story in the order it happened.
    return visitor_id, request_id, recent[::-1]


def activity_view(request):
    """
    Password-protected usage dashboard.

    Shares FEEDBACK_PASSWORD with the feedback viewer deliberately — same
    operator, same trust level, and a second secret to rotate would be a
    second secret to leak.
    """
    gate = _password_gate(request, 'activity_authenticated', 'activity_view.html', 'activity_view')
    if gate is not None:
        return gate

    days = _activity_window_days(request)
    since = datetime.now(dt_utc.utc) - timedelta(days=days)
    window = ActivityLog.objects.filter(occurred_at__gte=since)

    totals = window.aggregate(
        requests=Count('id'),
        visitors=Count('visitor_id', distinct=True),
        users=Count('user', distinct=True),
        errors=Count('id', filter=ERROR_FILTER),
    )

    actions = list(
        window.values('action')
        .annotate(total=Count('id'), errors=Count('id', filter=ERROR_FILTER))
        .order_by('-total')
    )
    for row in actions:
        row['error_rate'] = round(100 * row['errors'] / row['total'], 1) if row['total'] else 0.0

    recent_errors = list(
        window.filter(ERROR_FILTER)
        .select_related('user')[:ACTIVITY_ERROR_LIMIT]
    )

    trail_visitor, trail_request, trail = _activity_trail(request)

    return render(request, 'activity_view.html', {
        'authenticated': True,
        'days': days,
        'windows': ACTIVITY_WINDOWS,
        'totals': totals,
        'actions': actions,
        'recent_errors': recent_errors,
        'trail': trail,
        'trail_visitor': trail_visitor,
        'trail_request': trail_request,
    })


# ---------------------------------------------------------------------------
# Client-side error reporting
# ---------------------------------------------------------------------------

# Per visitor, and therefore advisory: visitor_id comes from a cookie the
# client sets, so dropping it between requests defeats this. It is here to
# stop one stuck browser from filling the table, which is the realistic
# case.
CLIENT_ERROR_HOURLY_LIMIT = 20

# The backstop that cannot be bypassed. This is a public, unauthenticated
# write endpoint; without a ceiling that ignores client-supplied identity,
# anyone willing to discard a cookie can grow the table without limit.
# Set well above what a real incident produces, so it only ever trips on
# abuse or on a bug reporting in a loop.
CLIENT_ERROR_GLOBAL_HOURLY_LIMIT = 500
CLIENT_ERROR_FIELD_LIMITS = {
    'message': 300,
    'source': 300,
    'stack': 2000,
    'page_url': 300,
    'kind': 30,
}


def client_error_report(request):
    """
    Records a failure the browser saw: an HTMX response error, a network
    failure, an uncaught exception, or a rejected promise.

    Server-side logging cannot see any of these. A JavaScript error breaks
    the page for the user while every server-side signal reads 200 OK, which
    is the widest blind spot the site has.

    Always answers 204, including when it drops the report. A reporting
    endpoint that returns an error to an error handler is how a page ends up
    in a reporting loop.
    """
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    visitor_id = getattr(request, 'visitor_id', '')

    over_global = _client_errors_this_hour() >= CLIENT_ERROR_GLOBAL_HOURLY_LIMIT
    over_visitor = (
        bool(visitor_id)
        and _client_errors_this_hour(visitor_id) >= CLIENT_ERROR_HOURLY_LIMIT
    )
    if over_global or over_visitor:
        skip_activity(request)
        return HttpResponse(status=204)

    payload = {
        key: request.POST.get(key, '').strip()[:limit]
        for key, limit in CLIENT_ERROR_FIELD_LIMITS.items()
    }
    if not payload['message']:
        skip_activity(request)
        return HttpResponse(status=204)

    try:
        payload['status'] = int(request.POST.get('status', 0) or 0)
    except (TypeError, ValueError):
        payload['status'] = 0

    log_detail(request, **payload)
    # Flat dotted keys rather than a nested dict. Sentry stores only
    # primitive attribute values, so a dict is flattened to a repr blob and
    # its fields stop being queryable -- and 'which JavaScript error, on
    # which page' is one of the more useful things to search by. The
    # ActivityLog row still gets the real structure via log_detail above,
    # where a JSONField can hold it.
    logger.warning(
        "client error (%s): %s", payload['kind'] or 'unknown', payload['message'],
        extra={
            'visitor_id': visitor_id,
            **{f'client_error.{key}': value for key, value in payload.items()},
        },
    )
    return HttpResponse(status=204)


def _client_errors_this_hour(visitor_id=None):
    """Client errors reported in the last hour, site-wide or for one visitor."""
    since = datetime.now(dt_utc.utc) - timedelta(hours=1)
    reports = ActivityLog.objects.filter(
        action=CLIENT_ERROR_ACTION,
        occurred_at__gte=since,
    )
    if visitor_id:
        reports = reports.filter(visitor_id=visitor_id)
    return reports.count()
