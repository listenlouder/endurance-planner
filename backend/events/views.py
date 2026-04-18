import hmac
import json
import time
import logging
from collections import Counter
from datetime import date, datetime, time as time_type, timezone as dt_utc
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q, Subquery, OuterRef

logger = logging.getLogger(__name__)
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render, redirect
from django.template.loader import render_to_string
from django.urls import reverse

from .forms import EventCreateForm
from django.conf import settings as django_settings

from .models import Availability, Driver, Event, Feedback, StintAssignment
from .utils import (
    build_stint_availability_matrix,
    get_availability_slots,
    get_stint_windows,
    seconds_to_mmss,
    stint_length_seconds,
    total_stints,
    validate_stint_sanity,
)

CURATED_TIMEZONES = [
    {
        'region': 'UTC',
        'zones': [('UTC', 'UTC')],
    },
    {
        'region': 'United States & Canada',
        'zones': [
            ('Hawaii (HST)', 'Pacific/Honolulu'),
            ('Alaska (AKST/AKDT)', 'America/Anchorage'),
            ('Pacific (PST/PDT)', 'America/Los_Angeles'),
            ('Mountain (MST/MDT)', 'America/Denver'),
            ('Mountain - no DST (MST)', 'America/Phoenix'),
            ('Central (CST/CDT)', 'America/Chicago'),
            ('Eastern (EST/EDT)', 'America/New_York'),
            ('Atlantic (AST/ADT)', 'America/Halifax'),
            ('Newfoundland (NST/NDT)', 'America/St_Johns'),
        ],
    },
    {
        'region': 'Central & South America',
        'zones': [
            ('Mexico City', 'America/Mexico_City'),
            ('Colombia & Peru', 'America/Bogota'),
            ('Brazil - Brasilia', 'America/Sao_Paulo'),
            ('Argentina', 'America/Argentina/Buenos_Aires'),
            ('Chile', 'America/Santiago'),
        ],
    },
    {
        'region': 'Europe',
        'zones': [
            ('UK & Ireland (GMT/BST)', 'Europe/London'),
            ('Portugal (WET/WEST)', 'Europe/Lisbon'),
            ('Western Europe (CET/CEST)', 'Europe/Paris'),
            ('Central Europe (CET/CEST)', 'Europe/Berlin'),
            ('Eastern Europe (EET/EEST)', 'Europe/Helsinki'),
            ('Greece & Romania (EET/EEST)', 'Europe/Athens'),
            ('Turkey (TRT)', 'Europe/Istanbul'),
            ('Russia - Moscow (MSK)', 'Europe/Moscow'),
        ],
    },
    {
        'region': 'Middle East & Africa',
        'zones': [
            ('South Africa (SAST)', 'Africa/Johannesburg'),
            ('East Africa (EAT)', 'Africa/Nairobi'),
            ('Egypt (EET)', 'Africa/Cairo'),
            ('UAE & Oman (GST)', 'Asia/Dubai'),
            ('Saudi Arabia (AST)', 'Asia/Riyadh'),
            ('Israel (IST/IDT)', 'Asia/Jerusalem'),
        ],
    },
    {
        'region': 'Asia',
        'zones': [
            ('India (IST)', 'Asia/Kolkata'),
            ('Pakistan (PKT)', 'Asia/Karachi'),
            ('Bangladesh (BST)', 'Asia/Dhaka'),
            ('Thailand & Vietnam (ICT)', 'Asia/Bangkok'),
            ('China, HK & Taiwan (CST)', 'Asia/Shanghai'),
            ('Singapore & Malaysia (SGT)', 'Asia/Singapore'),
            ('South Korea (KST)', 'Asia/Seoul'),
            ('Japan (JST)', 'Asia/Tokyo'),
            ('Philippines (PST)', 'Asia/Manila'),
        ],
    },
    {
        'region': 'Australia & Pacific',
        'zones': [
            ('Australia - Perth (AWST)', 'Australia/Perth'),
            ('Australia - Darwin (ACST)', 'Australia/Darwin'),
            ('Australia - Adelaide (ACST/ACDT)', 'Australia/Adelaide'),
            ('Australia - Brisbane (AEST)', 'Australia/Brisbane'),
            ('Australia - Sydney (AEST/AEDT)', 'Australia/Sydney'),
            ('New Zealand (NZST/NZDT)', 'Pacific/Auckland'),
        ],
    },
]

VALID_TIMEZONES = frozenset(
    zone[1]
    for group in CURATED_TIMEZONES
    for zone in group['zones']
)

SORTED_TIMEZONES = [
    zone[1]
    for group in CURATED_TIMEZONES
    for zone in group['zones']
]

_TIMEZONE_LIST_JSON = json.dumps([
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
# Phase 4: Admin constants and helpers
# ---------------------------------------------------------------------------

EDITABLE_FIELDS = {
    'name':               {'type': 'text',     'label': 'Event Name',                    'required': True},
    'date':               {'type': 'date',     'label': 'Date',                          'required': True},
    'start_time_utc':     {'type': 'time',     'label': 'Start Time (UTC)',               'required': True},
    'length_hours':       {'type': 'number',   'label': 'Race Length (hours)',            'required': True,  'min': 0,    'max': 168},
    'car':                {'type': 'text',     'label': 'Car',                           'required': False},
    'track':              {'type': 'text',     'label': 'Track',                         'required': False},
    'team_name':          {'type': 'text',     'label': 'Team Name',                     'required': False},
    'setup':              {'type': 'textarea', 'label': 'Setup Notes',                   'required': False},
    'recruiting':         {'type': 'checkbox', 'label': 'Recruiting Drivers',            'required': False},
    'avg_lap_seconds':    {'type': 'mmss',     'label': 'Average Lap Time',              'required': False},
    'in_lap_seconds':     {'type': 'mmss',     'label': 'In Lap Time',                   'required': False},
    'out_lap_seconds':    {'type': 'mmss',     'label': 'Out Lap Time',                  'required': False},
    'target_laps':        {'type': 'number',   'label': 'Target Laps per Stint',         'required': False, 'min': 1,    'max': 500},
    'fuel_capacity':      {'type': 'number',   'label': 'Fuel Capacity (L)',             'required': False, 'min': 0.1},
    'fuel_per_lap':       {'type': 'number',   'label': 'Fuel Use per Lap (L)',          'required': False, 'min': 0.01},
    'tire_change_fuel_min': {'type': 'number', 'label': 'Min Fuel for Tyre Change (L)',  'required': False, 'min': 0},
}

_REQUIRED_FOR_STINTS = [
    'avg_lap_seconds', 'in_lap_seconds', 'out_lap_seconds',
    'target_laps', 'fuel_capacity', 'fuel_per_lap',
]

_MINUTES_CHOICES = [(0, '00'), (15, '15'), (30, '30'), (45, '45')]


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
                  {'error': 'You do not have permission to access this page.'},
                  status=403)


def not_found_view(request, exception=None):
    return render(request, '404.html', status=404)


def server_error_view(request):
    return render(request, '500.html', status=500)


def _get_field_display_value(event, field_name):
    """Return the display value for a field (handles conversions for special fields)."""
    if field_name == 'length_hours':
        return event.length_seconds / 3600
    config = EDITABLE_FIELDS.get(field_name, {})
    if config.get('type') == 'mmss':
        value = getattr(event, field_name)
        return seconds_to_mmss(value) if value is not None else ''
    value = getattr(event, field_name)
    return '' if value is None else value


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


def _build_table_data(slots, uncovered_slots, availability_matrix, drivers, admin_tz):
    """Build the table_data list for the availability table partial."""
    safe_tz = admin_tz if admin_tz in VALID_TIMEZONES else 'UTC'
    admin_tz_zone = ZoneInfo(safe_tz)

    def _fmt_slot(slot):
        local = slot.astimezone(admin_tz_zone)
        return f"{local.strftime('%a')} {local.month}/{local.day} {local.strftime('%H:%M')}"

    return [
        {
            'slot_utc': slot,
            'slot_local_str': _fmt_slot(slot),
            'is_uncovered': slot in uncovered_slots,
            'driver_availability': {
                driver.id: slot in availability_matrix[driver.id]
                for driver in drivers
            },
        }
        for slot in slots
    ]


def _make_field_ctx(event, field_name):
    """Build the partial context dict for a single field."""
    config = EDITABLE_FIELDS[field_name]
    missing_required = [
        EDITABLE_FIELDS[f]['label']
        for f in _REQUIRED_FOR_STINTS
        if getattr(event, f) is None
    ]
    ctx = {
        'event': event,
        'field_name': field_name,
        'field_label': config['label'],
        'field_type': config['type'],
        'field_min': str(config['min']) if 'min' in config else '',
        'field_max': str(config['max']) if 'max' in config else '',
        'current_value': _get_field_display_value(event, field_name),
        'required_fields': _REQUIRED_FOR_STINTS,
        'missing_required_fields': missing_required,
        'sanity_warnings': validate_stint_sanity(event),
    }
    if field_name == 'length_hours':
        total_secs = event.length_seconds or 0
        ctx['current_hours'] = total_secs // 3600
        ctx['current_minutes'] = (total_secs % 3600) // 60
    return ctx


def _validate_and_save_field(event, field_name, value_str):
    """Validate value_str for field_name, save to event. Returns error string or None."""
    config = EDITABLE_FIELDS[field_name]
    ftype = config['type']
    required = config['required']
    value = value_str.strip()

    if ftype in ('text', 'textarea'):
        if required and not value:
            return f"{config['label']} is required."
        setattr(event, field_name, value)
        event.save(update_fields=[field_name])
        return None

    if ftype == 'date':
        if not value:
            return f"{config['label']} is required."
        try:
            event.date = date.fromisoformat(value)
        except ValueError:
            return "Invalid date. Use YYYY-MM-DD format."
        event.save(update_fields=['date'])
        return None

    if ftype == 'time':
        if not value:
            return f"{config['label']} is required."
        try:
            event.start_time_utc = time_type.fromisoformat(value)
        except ValueError:
            return "Invalid time. Use HH:MM format."
        event.save(update_fields=['start_time_utc'])
        return None

    if ftype == 'mmss':
        if not value:
            if required:
                return f"{config['label']} is required."
            setattr(event, field_name, None)
            event.save(update_fields=[field_name])
            return None
        try:
            parts = value.split(':')
            if len(parts) != 2:
                raise ValueError
            mins = int(parts[0])
            secs = int(parts[1])
            if not (0 <= secs < 60) or mins < 0:
                raise ValueError
            total_secs = (mins * 60) + secs
            if total_secs == 0:
                return f"{config['label']} must be at least 1 second."
        except (ValueError, IndexError):
            return 'Please enter time in M:SS format (e.g. 1:45)'
        setattr(event, field_name, float(total_secs))
        event.save(update_fields=[field_name])
        return None

    if ftype == 'checkbox':
        setattr(event, field_name, value == 'true')
        event.save(update_fields=[field_name])
        return None

    if ftype == 'number':
        if not value:
            if required:
                return f"{config['label']} is required."
            setattr(event, field_name, None)
            event.save(update_fields=[field_name])
            return None

        try:
            num = float(value)
        except ValueError:
            return "Please enter a valid number."

        min_val = config.get('min')
        max_val = config.get('max')
        if min_val is not None and num < min_val:
            return f"Value must be at least {min_val}."
        if max_val is not None and num > max_val:
            return f"Value must be at most {max_val}."

        if field_name == 'length_hours':
            if num == 0:
                return 'Race length must be greater than zero.'
            event.length_seconds = round(num * 3600)
            event.save(update_fields=['length_seconds'])
        elif field_name == 'target_laps':
            event.target_laps = int(num)
            event.save(update_fields=['target_laps'])
        else:
            setattr(event, field_name, num)
            event.save(update_fields=[field_name])
        return None

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_signup_context(event):
    """Returns shared context dict for signup and signup_edit views."""
    slots = get_availability_slots(event)
    return {
        'slots': slots,
        'slot_timestamps_json': json.dumps([
            s.isoformat().replace('+00:00', 'Z') if s.tzinfo else s.isoformat() + 'Z'
            for s in slots
        ]),
        'timezone_list_json': _TIMEZONE_LIST_JSON,
    }


def _save_availability(driver, slots_raw, event):
    """Create Availability records for a driver from a list of ISO timestamp strings."""
    valid_slot_set = {
        s.isoformat().replace('+00:00', 'Z') if s.tzinfo else s.isoformat() + 'Z'
        for s in get_availability_slots(event)
    }
    objects = []
    for slot_str in slots_raw:
        if slot_str in valid_slot_set:
            objects.append(Availability(driver=driver, slot_utc=datetime.fromisoformat(slot_str.replace('Z', '+00:00'))))
    Availability.objects.bulk_create(objects)


def _validate_signup_post(post_data):
    """Validate common signup POST fields. Returns (cleaned, errors)."""
    driver_name = post_data.get('driver_name', '').strip()
    timezone = post_data.get('timezone', '').strip()
    slots_raw = post_data.getlist('slots')

    errors = {}
    if not driver_name:
        errors['driver_name'] = 'Your name is required.'
    if not timezone:
        errors['timezone'] = 'Timezone is required.'
    if not slots_raw:
        errors['slots'] = 'Please select at least one availability slot.'

    return {'driver_name': driver_name, 'timezone': timezone, 'slots_raw': slots_raw}, errors


# ---------------------------------------------------------------------------
# Existing views
# ---------------------------------------------------------------------------

def home(request):
    now_utc = datetime.now(tz=dt_utc.utc)

    recruiting_qs = Event.objects.filter(
        recruiting=True,
        date__gte=now_utc.date(),
    ).annotate(
        driver_count=Count('drivers'),
    ).order_by('date', 'start_time_utc')[:50]

    upcoming = []
    for event in recruiting_qs:
        event_start = datetime.combine(
            event.date,
            event.start_time_utc,
            tzinfo=dt_utc.utc,
        )
        if event_start > now_utc:
            upcoming.append(event)
        if len(upcoming) == 8:
            break

    context = {'recruiting_events': upcoming}

    if request.user.is_authenticated:
        driver_entries = Driver.objects.filter(
            event=OuterRef('pk'), user=request.user
        ).values('name')[:1]

        context['admin_events'] = Event.objects.filter(
            created_by=request.user
        ).order_by('-date')[:10]

        # Exclude events the user created — those already appear in admin_events
        context['driver_events'] = Event.objects.filter(
            drivers__user=request.user
        ).exclude(
            created_by=request.user
        ).annotate(
            my_driver_name=Subquery(driver_entries)
        ).order_by('-date')[:10]

    return render(request, 'home.html', context)


def event_create(request):
    def _create_ctx(form):
        return {'form': form}

    if request.method == 'POST':
        form = EventCreateForm(request.POST)
        if form.is_valid():
            event = Event(
                name=form.cleaned_data['name'],
                team_name=form.cleaned_data.get('team_name', ''),
                car=form.cleaned_data.get('car', ''),
                track=form.cleaned_data.get('track', ''),
                date=form.cleaned_data['date'],
                start_time_utc=form.cleaned_data['start_time_utc'],
                length_seconds=form.cleaned_data['length_seconds'],
                recruiting=form.cleaned_data.get('recruiting', False),
            )
            if request.user.is_authenticated:
                event.created_by = request.user
            event.save()
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
        if request.htmx:
            return render(request, 'partials/event_create_form.html', _create_ctx(form))
    else:
        form = EventCreateForm()
    return render(request, 'event_create.html', _create_ctx(form))


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

    upcoming = []
    for event in results:
        event_start = datetime.combine(
            event.date,
            event.start_time_utc,
            tzinfo=dt_utc.utc,
        )
        if event_start > now_utc:
            upcoming.append(event)
        if len(upcoming) == 8:
            break

    return render(request,
        'partials/search_results.html',
        {'results': upcoming, 'query': query})


def view_event(request, event_id):
    """Public view-only page. No authentication required."""
    event = get_object_or_404(Event, id=event_id)

    if event.has_required_stint_fields:
        stint_windows = get_stint_windows(event)
    else:
        stint_windows = []

    assignments = {
        sa.stint_number: sa.driver
        for sa in StintAssignment.objects.filter(
            event=event
        ).select_related('driver')
    }

    stint_rows = []
    for sw in stint_windows:
        n = sw['stint_number']
        stint_rows.append({
            'stint_number': n,
            'start_utc': sw['start_utc'],
            'end_utc': sw['end_utc'],
            'driver': assignments.get(n),
        })

    stint_rows_json = json.dumps([
        {
            'stint_number': row['stint_number'],
            'start_utc': normalize_iso(row['start_utc']),
            'end_utc': normalize_iso(row['end_utc']),
            'driver_name': row['driver'].name if row['driver'] else None,
        }
        for row in stint_rows
    ])

    lh = round(event.length_seconds / 3600, 1)
    lh_display = f"{int(lh)} hours" if event.length_seconds % 3600 == 0 else f"{lh} hours"

    total_seconds = event.length_seconds
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    length_display = f"{hours}h {minutes}m" if minutes else f"{hours}h"

    user_driver = None
    if request.user.is_authenticated:
        try:
            user_driver = Driver.objects.get(event=event, user=request.user)
        except Driver.DoesNotExist:
            pass

    has_unassigned = StintAssignment.objects.filter(event=event, driver=None).exists()

    # Stint duration display
    if event.has_required_stint_fields:
        sl = stint_length_seconds(event)
        sl_mins = int(sl // 60)
        sl_secs = int(sl % 60)
        stint_duration_display = (
            f"{sl_mins}m {sl_secs}s" if sl_secs
            else f"{sl_mins}m"
        )
    else:
        stint_duration_display = None

    # Driver stint counts for summary pills
    driver_counts = Counter()
    for row in stint_rows:
        if row['driver']:
            driver_counts[row['driver'].name] += 1
    driver_counts = dict(driver_counts)

    return render(request, 'view.html', {
        'event': event,
        'stint_rows': stint_rows,
        'stint_rows_json': stint_rows_json,
        'has_stints': bool(assignments),
        'stints_ready': event.has_required_stint_fields,
        'length_hours_display': lh_display,
        'length_display': length_display,
        'has_unassigned': has_unassigned,
        'user_driver': user_driver,
        'show_signup_link': request.GET.get('from') == 'recruiting',
        'stint_duration_display': stint_duration_display,
        'driver_counts': driver_counts,
    })


# ---------------------------------------------------------------------------
# Phase 3: signup views
# ---------------------------------------------------------------------------

def signup(request, event_id):
    """GET: render signup form. POST: validate and save driver + availability."""
    event = get_object_or_404(Event, id=event_id)

    if request.method == 'POST':
        cleaned, errors = _validate_signup_post(request.POST)

        if not errors and cleaned['timezone'] not in VALID_TIMEZONES:
            errors['timezone'] = 'Invalid timezone selected. Please try again.'

        if not errors:
            driver = Driver.objects.create(
                event=event,
                name=cleaned['driver_name'],
                timezone=cleaned['timezone'],
            )
            if request.user.is_authenticated:
                driver.user = request.user
                driver.save(update_fields=['user'])
            _save_availability(driver, cleaned['slots_raw'], event)
            success_url = reverse('signup_success', kwargs={'event_id': event_id, 'driver_id': driver.id})
            if request.htmx:
                response = HttpResponse()
                response['HX-Redirect'] = success_url
                return response
            return redirect('signup_success', event_id=event_id, driver_id=driver.id)

        ctx = get_signup_context(event)
        ctx.update({
            'event': event,
            'errors': errors,
            'submitted_name': cleaned['driver_name'],
            'submitted_slot_timestamps': cleaned['slots_raw'],
            'submitted_timezone': cleaned['timezone'],
        })
        if request.htmx:
            return render(request, 'partials/signup_form.html', ctx)
        return render(request, 'signup.html', ctx)

    ctx = get_signup_context(event)
    prefill_name = ''
    if request.user.is_authenticated:
        prefill_name = request.user.discord_username or request.user.username
    ctx.update({'event': event, 'submitted_slot_timestamps': [], 'prefill_name': prefill_name})
    return render(request, 'signup.html', ctx)


def signup_edit(request, event_id, driver_id):
    """GET: edit form pre-populated. POST: replace availability."""
    event = get_object_or_404(Event, id=event_id)
    driver = get_object_or_404(Driver.objects.prefetch_related('availability'), id=driver_id, event=event)

    def _existing_timestamps():
        return [
            a.slot_utc.isoformat().replace('+00:00', 'Z')
            if a.slot_utc.tzinfo else a.slot_utc.isoformat() + 'Z'
            for a in driver.availability.all()
        ]

    if request.method == 'POST':
        cleaned, errors = _validate_signup_post(request.POST)

        if not errors and cleaned['timezone'] not in VALID_TIMEZONES:
            errors['timezone'] = 'Invalid timezone selected. Please try again.'

        if not errors:
            with transaction.atomic():
                driver.name = cleaned['driver_name']
                driver.timezone = cleaned['timezone']
                driver.save()
                driver.availability.all().delete()
                _save_availability(driver, cleaned['slots_raw'], event)

            from_admin = request.GET.get('from') == 'admin'
            if from_admin:
                redirect_url = reverse('admin_dashboard', kwargs={'event_id': event_id})
            else:
                redirect_url = reverse('signup_success', kwargs={'event_id': event_id, 'driver_id': driver_id})
            if request.htmx:
                response = HttpResponse()
                response['HX-Redirect'] = redirect_url
                return response
            return redirect(redirect_url)

        from_admin = request.GET.get('from') == 'admin'
        ctx = get_signup_context(event)
        ctx.update({
            'event': event,
            'driver': driver,
            'errors': errors,
            'submitted_name': cleaned['driver_name'],
            'existing_slot_timestamps': cleaned['slots_raw'],
            'submitted_timezone': cleaned['timezone'],
            'from_admin': from_admin,
        })
        if request.htmx:
            return render(request, 'partials/signup_edit_form.html', ctx)
        return render(request, 'signup_edit.html', ctx)

    from_admin = request.GET.get('from') == 'admin'
    ctx = get_signup_context(event)
    ctx.update({
        'event': event,
        'driver': driver,
        'existing_slot_timestamps': _existing_timestamps(),
        'from_admin': from_admin,
    })
    return render(request, 'signup_edit.html', ctx)


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
    if request.method == 'DELETE':
        event = get_object_or_404(Event, id=event_id)
        driver = get_object_or_404(Driver, id=driver_id, event=event)
        driver.delete()
        response = HttpResponse()
        response['HX-Redirect'] = '/'
        return response
    response = HttpResponse(status=405)
    response['Allow'] = 'DELETE'
    return response


# ---------------------------------------------------------------------------
# Phase 4: Admin views
# ---------------------------------------------------------------------------

def set_timezone(request):
    if request.method != 'POST':
        response = HttpResponse(status=405)
        response['Allow'] = 'POST'
        return response
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


def _build_admin_context(request, event):
    """Build the context dict for the admin page. Caller must have already verified auth."""
    drivers = (
        Driver.objects.filter(event=event)
        .prefetch_related('availability')
        .annotate(stint_count=Count('stint_assignments'))
        .order_by('signed_up_at')
    )
    slots = get_availability_slots(event)
    availability_matrix, uncovered_slots = _build_availability_matrix(drivers, slots)
    admin_tz = request.COOKIES.get('admin_timezone', 'UTC')
    if admin_tz not in VALID_TIMEZONES:
        admin_tz = 'UTC'

    table_data = _build_table_data(slots, uncovered_slots, availability_matrix, drivers, admin_tz)

    field_groups = [
        {
            'title': 'Basic Info',
            'fields': [
                (name, EDITABLE_FIELDS[name], _get_field_display_value(event, name),
                 str(EDITABLE_FIELDS[name].get('min', '')), str(EDITABLE_FIELDS[name].get('max', '')))
                for name in ['name', 'date', 'start_time_utc', 'length_hours']
            ],
        },
        {
            'title': 'Race Details',
            'fields': [
                (name, EDITABLE_FIELDS[name], _get_field_display_value(event, name),
                 str(EDITABLE_FIELDS[name].get('min', '')), str(EDITABLE_FIELDS[name].get('max', '')))
                for name in ['car', 'track', 'team_name', 'setup', 'recruiting']
            ],
        },
        {
            'title': 'Timing & Fuel',
            'fields': [
                (name, EDITABLE_FIELDS[name], _get_field_display_value(event, name),
                 str(EDITABLE_FIELDS[name].get('min', '')), str(EDITABLE_FIELDS[name].get('max', '')))
                for name in [
                    'avg_lap_seconds', 'in_lap_seconds', 'out_lap_seconds',
                    'target_laps', 'fuel_capacity', 'fuel_per_lap', 'tire_change_fuel_min',
                ]
            ],
        },
    ]

    missing_required_fields = [
        EDITABLE_FIELDS[f]['label']
        for f in _REQUIRED_FOR_STINTS
        if getattr(event, f) is None
    ]

    slot_timestamps_json = json.dumps([
        s.isoformat().replace('+00:00', 'Z') if s.tzinfo else s.isoformat() + 'Z'
        for s in slots
    ])

    total_seconds = event.length_seconds or 0

    base = request.build_absolute_uri('/').rstrip('/')
    signup_url = f"{base}/{event.id}/signup/"

    # Stint assignment context for Section 4
    has_required_stint_fields = event.has_required_stint_fields
    stint_windows = []
    existing_assignments = {}
    stint_availability_matrix = {}
    availability_json = {}

    if has_required_stint_fields:
        stint_windows = get_stint_windows(event)
        admin_tz_zone = ZoneInfo(admin_tz)
        for sw in stint_windows:
            sw['start_local'] = sw['start_utc'].astimezone(admin_tz_zone).strftime('%H:%M')
        existing_assignments = {
            sa.stint_number: str(sa.driver_id)
            for sa in StintAssignment.objects.filter(event=event)
            if sa.driver_id
        }
        stint_availability_matrix = build_stint_availability_matrix(drivers, stint_windows)
        availability_json = {
            str(driver.id): [normalize_iso(a.slot_utc) for a in driver.availability.all()]
            for driver in drivers
        }

    if has_required_stint_fields:
        sl = stint_length_seconds(event)
        sl_mins = int(sl // 60)
        sl_secs = int(sl % 60)
        stint_duration_display = f"{sl_mins}m {sl_secs}s" if sl_secs else f"{sl_mins}m"
    else:
        stint_duration_display = None

    return {
        'event': event,
        'admin_tz': admin_tz,
        'drivers': drivers,
        'field_groups': field_groups,
        'required_fields': _REQUIRED_FOR_STINTS,
        'missing_required_fields': missing_required_fields,
        'availability_matrix': availability_matrix,
        'table_data': table_data,
        'sanity_warnings': validate_stint_sanity(event),
        'timezone_list_json': _TIMEZONE_LIST_JSON,
        'slot_timestamps_json': slot_timestamps_json,
        'slots': slots,
        'signup_url': signup_url,
        'length_hours_display': total_seconds // 3600,
        'length_minutes_display': (total_seconds % 3600) // 60,
        'has_required_stint_fields': has_required_stint_fields,
        'stint_windows': stint_windows,
        'stint_availability': stint_availability_matrix,
        'stint_duration_display': stint_duration_display,
        'stint_windows_json': json.dumps([{
            'stint_number': sw['stint_number'],
            'start_utc': normalize_iso(sw['start_utc']),
            'end_utc': normalize_iso(sw['end_utc']),
        } for sw in stint_windows]),
        'existing_assignments_json': json.dumps({str(k): str(v) for k, v in existing_assignments.items()}),
        'drivers_json': json.dumps([{'id': str(d.id), 'name': d.name} for d in drivers]),
        'availability_json': json.dumps(availability_json),
        'has_assignments': StintAssignment.objects.filter(event=event).exists(),
        'stint_availability_json': json.dumps({
            str(driver_id): {str(stint_num): status for stint_num, status in stints.items()}
            for driver_id, stints in stint_availability_matrix.items()
        }),
    }


def admin_page(request, event_id, admin_key):
    event = get_object_or_404(Event, id=event_id)

    if not _check_admin_key(event, admin_key):
        return render(request, 'admin_error.html', {'error': 'Invalid admin key supplied.'})

    # Rotate session ID on promotion to prevent session fixation
    request.session.cycle_key()
    request.session[f'admin_{event_id}'] = True

    ctx = _build_admin_context(request, event)
    return render(request, 'admin.html', ctx)


def admin_dashboard(request, event_id):
    """Admin page — Discord owner bypass or session-authenticated."""
    if request.user.is_authenticated:
        event = get_object_or_404(Event, id=event_id)
        if event.created_by == request.user:
            request.session.cycle_key()
            request.session[f'admin_{event_id}'] = True
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
        if not name:
            return render(request,
                'partials/driver_name_edit_form.html',
                {
                    'driver': driver,
                    'event': event,
                    'error': 'Driver name cannot be empty.'
                })
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
    if request.method != 'DELETE':
        return HttpResponse(status=405)
    event = require_admin_session(request, event_id)
    Driver.objects.filter(id=driver_id, event=event).delete()
    if event.has_required_stint_fields:
        response = HttpResponse(status=200)
        response['HX-Refresh'] = 'true'
        return response
    response = HttpResponse()
    response['HX-Reswap'] = 'delete'
    return response


def admin_add_driver(request, event_id):
    """POST only. Creates a driver with name, timezone, and optional availability."""
    if request.method != 'POST':
        return HttpResponse(status=405)
    event = require_admin_session(request, event_id)

    driver_name = request.POST.get('driver_name', '').strip()
    timezone = request.POST.get('timezone', '').strip()
    slots_raw = request.POST.getlist('slots')

    errors = {}
    if not driver_name:
        errors['driver_name'] = 'Driver name is required.'
    if not timezone or timezone not in VALID_TIMEZONES:
        errors['timezone'] = 'A valid timezone is required.'

    admin_tz = request.COOKIES.get('admin_timezone', 'UTC')
    if admin_tz not in VALID_TIMEZONES:
        admin_tz = 'UTC'

    drivers_qs = (
        Driver.objects.filter(event=event)
        .prefetch_related('availability')
        .annotate(stint_count=Count('stint_assignments'))
        .order_by('signed_up_at')
    )

    if errors:
        error_msg = ' '.join(errors.values())
        err_slots = get_availability_slots(event)
        err_matrix, _ = _build_availability_matrix(drivers_qs, err_slots)
        driver_list_html = render_to_string(
            'partials/driver_list.html',
            {
                'drivers': drivers_qs,
                'event': event,
                'admin_tz': admin_tz,
                'availability_matrix': err_matrix,
                'slots': err_slots,
            },
            request=request,
        )
        error_html = f'<div class="text-red-400 text-sm mb-3 px-2">⚠ {error_msg}</div>'
        return HttpResponse(error_html + driver_list_html)

    driver = Driver.objects.create(event=event, name=driver_name, timezone=timezone)
    if slots_raw:
        _save_availability(driver, slots_raw, event)

    if event.has_required_stint_fields:
        response = HttpResponse(status=200)
        response['HX-Refresh'] = 'true'
        return response

    # Refresh driver list after creation (stints not yet configured)
    drivers_qs = (
        Driver.objects.filter(event=event)
        .prefetch_related('availability')
        .annotate(stint_count=Count('stint_assignments'))
        .order_by('signed_up_at')
    )
    slots = get_availability_slots(event)
    availability_matrix, _ = _build_availability_matrix(drivers_qs, slots)

    driver_list_html = render_to_string(
        'partials/driver_list.html',
        {
            'drivers': drivers_qs,
            'event': event,
            'admin_tz': admin_tz,
            'availability_matrix': availability_matrix,
            'slots': slots,
        },
        request=request,
    )
    return HttpResponse(driver_list_html)


def admin_save_details(request, event_id):
    """POST. Saves all event detail fields at once."""
    event = require_admin_session(request, event_id)
    if request.method != 'POST':
        return HttpResponseBadRequest()

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
        return render(request, 'partials/admin_details_errors.html', {'errors': errors}, status=422)

    event.name = name
    if parsed_date:
        event.date = parsed_date
    if parsed_time:
        event.start_time_utc = parsed_time
    if length_seconds:
        event.length_seconds = length_seconds
    event.team_name = request.POST.get('team_name', '').strip()
    event.car = request.POST.get('car', '').strip()
    event.track = request.POST.get('track', '').strip()
    event.setup = request.POST.get('setup', '').strip()
    event.recruiting = (request.POST.get('recruiting') == 'on')
    event.save()

    response = HttpResponse(status=200)
    response['HX-Trigger'] = 'show-toast'
    return response


def admin_save_calc(request, event_id):
    """POST. Saves stint calculation fields."""
    event = require_admin_session(request, event_id)
    if request.method != 'POST':
        return HttpResponseBadRequest()

    errors = {}

    LAP_FIELDS = ['avg_lap_seconds', 'in_lap_seconds', 'out_lap_seconds']
    POST_NAMES = {
        'avg_lap_seconds': 'avg_lap',
        'in_lap_seconds': 'in_lap',
        'out_lap_seconds': 'out_lap',
    }

    def parse_mmss(val):
        parts = val.strip().split(':')
        if len(parts) != 2:
            raise ValueError
        m, s = int(parts[0]), int(parts[1])
        if not (0 <= s < 60):
            raise ValueError
        return (m * 60) + s

    for field in LAP_FIELDS:
        raw = request.POST.get(POST_NAMES[field], '').strip()
        if raw:
            try:
                setattr(event, field, parse_mmss(raw))
            except (ValueError, IndexError):
                errors[field] = 'Use MM:SS format (e.g. 2:18)'

    numeric_fields = {
        'fuel_capacity': 'fuel_capacity',
        'fuel_per_lap': 'fuel_burn',
    }
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

    if errors:
        return render(request, 'partials/admin_calc_errors.html', {'errors': errors})

    event.save()
    if event.has_required_stint_fields:
        response = HttpResponse(status=200)
        response['HX-Refresh'] = 'true'
        return response
    response = HttpResponse(status=200)
    response['HX-Trigger'] = 'show-toast'
    return response


def create_stints_redirect(request, event_id):
    """Stub — stint assignment is now on the admin dashboard."""
    event = require_admin_session(request, event_id)
    return redirect('admin_dashboard', event_id=event_id)


def admin_save_assignments(request, event_id):
    """POST. Saves all stint driver assignments at once."""
    event = require_admin_session(request, event_id)
    if request.method != 'POST':
        return HttpResponseBadRequest()

    if not event.has_required_stint_fields:
        return HttpResponseBadRequest()

    stint_windows = get_stint_windows(event)

    assignments_to_create = []
    for sw in stint_windows:
        n = sw['stint_number']
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
        assignments_to_create.append(
            StintAssignment(event=event, stint_number=n, driver=driver)
        )

    with transaction.atomic():
        StintAssignment.objects.filter(event=event).delete()
        StintAssignment.objects.bulk_create(assignments_to_create)

    response = HttpResponse(status=200)
    response['HX-Trigger'] = 'show-toast'
    return response


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
    )

    response = HttpResponse('')
    response['HX-Trigger'] = 'feedbackSuccess'
    return response


def feedback_view(request):
    """
    Password-protected feedback viewer.
    GET: show password prompt if not authenticated.
    POST: validate password, show feedback list on success.
    Session persists authentication for the browser session.
    """
    session_key = 'feedback_authenticated'

    # Handle logout
    if request.GET.get('logout'):
        request.session.pop(session_key, None)
        return redirect('feedback_view')

    if request.method == 'POST':
        password = request.POST.get('password', '')
        if (django_settings.FEEDBACK_PASSWORD
                and hmac.compare_digest(password, django_settings.FEEDBACK_PASSWORD)):
            request.session[session_key] = True
        else:
            time.sleep(1)
            return render(request, 'feedback_view.html', {
                'authenticated': False,
                'error': 'Incorrect password.'
            })

    if not request.session.get(session_key):
        return render(request, 'feedback_view.html', {
            'authenticated': False,
        })

    feedback_items = list(Feedback.objects.all()[:200])
    return render(request, 'feedback_view.html', {
        'authenticated': True,
        'feedback_items': feedback_items,
        'total': len(feedback_items),
    })
