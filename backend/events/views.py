import hmac
import json
from datetime import date, datetime, time as time_type, timezone as dt_utc
from zoneinfo import available_timezones, ZoneInfo

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render, redirect
from django.template.loader import render_to_string
from django.urls import reverse

from .forms import EventCreateForm
from .models import Availability, Driver, Event, StintAssignment
from .utils import (
    get_availability_slots,
    get_stint_windows,
    seconds_to_mmss,
    stint_length_seconds,
    total_stints,
    validate_stint_sanity,
)

# Computed once at module load — available_timezones() is expensive
VALID_TIMEZONES = frozenset(available_timezones())
SORTED_TIMEZONES = sorted(VALID_TIMEZONES)


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
        total_secs = event.length_seconds
        raw_hours = total_secs // 3600
        raw_minutes = (total_secs % 3600) // 60
        # Snap minutes to nearest quarter-hour choice
        snapped = min((0, 15, 30, 45), key=lambda x: abs(x - raw_minutes))
        ctx['current_hours'] = raw_hours
        ctx['current_minutes'] = snapped
        ctx['hours_range'] = range(0, 169)
        ctx['minutes_choices'] = _MINUTES_CHOICES
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
        'timezone_list': SORTED_TIMEZONES,
        'timezone_list_json': json.dumps(SORTED_TIMEZONES),
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
    return render(request, 'home.html')


def event_create(request):
    def _create_ctx(form):
        try:
            current_hours = int(form.data.get('length_hours', 1)) if form.is_bound else 1
        except (TypeError, ValueError):
            current_hours = 1
        try:
            current_minutes = int(form.data.get('length_minutes', 0)) if form.is_bound else 0
        except (TypeError, ValueError):
            current_minutes = 0
        return {
            'form': form,
            'hours_range': range(0, 169),
            'minutes_choices': _MINUTES_CHOICES,
            'current_length_hours': current_hours,
            'current_length_minutes': current_minutes,
        }

    if request.method == 'POST':
        form = EventCreateForm(request.POST)
        if form.is_valid():
            event = Event(
                name=form.cleaned_data['name'],
                date=form.cleaned_data['date'],
                start_time_utc=form.cleaned_data['start_time_utc'],
                length_seconds=form.cleaned_data['length_seconds'],
            )
            event.save()
            success_ctx = {
                'success': True,
                'event': event,
                'base_url': request.build_absolute_uri('/'),
            }
            if request.htmx:
                return render(request, 'partials/event_create_success.html', success_ctx)
            return render(request, 'event_create.html', success_ctx)
        if request.htmx:
            return render(request, 'partials/event_create_form.html', _create_ctx(form))
    else:
        form = EventCreateForm()
    return render(request, 'event_create.html', _create_ctx(form))


def event_lookup_by_id(request):
    """HTMX endpoint. POST with 'event_id' field."""
    if not request.htmx:
        return HttpResponseBadRequest("This endpoint requires HTMX.")

    event_id = request.POST.get('event_id', '').strip()
    error_html = (
        '<p class="text-red-500 text-sm mt-2">'
        'No event found with that ID. Please check and try again.'
        '</p>'
    )

    try:
        event = Event.objects.get(id=event_id)
    except (Event.DoesNotExist, ValueError, ValidationError):
        return HttpResponse(error_html)

    response = HttpResponse()
    response['HX-Redirect'] = f'/{event.id}/view/'
    return response


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

    has_unassigned = StintAssignment.objects.filter(event=event, driver=None).exists()

    return render(request, 'view.html', {
        'event': event,
        'stint_rows': stint_rows,
        'stint_rows_json': stint_rows_json,
        'has_stints': bool(assignments),
        'stints_ready': event.has_required_stint_fields,
        'length_hours_display': lh_display,
        'has_unassigned': has_unassigned,
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
        })
        if request.htmx:
            return render(request, 'partials/signup_form.html', ctx)
        return render(request, 'signup.html', ctx)

    ctx = get_signup_context(event)
    ctx.update({'event': event, 'submitted_slot_timestamps': []})
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
    return render(request, 'signup_success.html', {
        'event': event,
        'driver': driver,
        'updated': request.GET.get('updated') == '1',
        'base_url': request.build_absolute_uri('/'),
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
                for name in ['car', 'track', 'team_name', 'setup']
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

    return {
        'event': event,
        'admin_tz': admin_tz,
        'drivers': drivers,
        'field_groups': field_groups,
        'required_fields': _REQUIRED_FOR_STINTS,
        'missing_required_fields': missing_required_fields,
        'table_data': table_data,
        'sanity_warnings': validate_stint_sanity(event),
        'timezone_list_json': json.dumps(SORTED_TIMEZONES),
        'slot_timestamps_json': slot_timestamps_json,
        'slots': slots,
    }


def admin_page(request, event_id, admin_key):
    event = get_object_or_404(Event, id=event_id)

    if not _check_admin_key(event, admin_key):
        return render(request, 'admin_error.html', {'error': 'Invalid admin key supplied.'})

    # Rotate session ID on promotion to prevent session fixation
    request.session.cycle_key()
    request.session[f'admin_{event_id}'] = True

    return render(request, 'admin.html', _build_admin_context(request, event))


def admin_dashboard(request, event_id):
    """Session-authenticated admin page — no key in the URL."""
    event = require_admin_session(request, event_id)
    return render(request, 'admin.html', _build_admin_context(request, event))


def admin_edit_field(request, event_id):
    event = require_admin_session(request, event_id)

    if request.method == 'GET':
        field_name = request.GET.get('field', '')
        if field_name not in EDITABLE_FIELDS:
            return HttpResponse(status=400)
        ctx = _make_field_ctx(event, field_name)
        if request.GET.get('cancel'):
            return render(request, 'partials/field_display.html', ctx)
        return render(request, 'partials/field_edit_form.html', ctx)

    if request.method == 'POST':
        field_name = request.POST.get('field', '')
        if field_name not in EDITABLE_FIELDS:
            return HttpResponse(status=400)

        # Special handling for length_hours: also reads length_minutes
        if field_name == 'length_hours':
            try:
                hours = int(request.POST.get('value', '0'))
            except (ValueError, TypeError):
                hours = 0
            try:
                minutes = int(request.POST.get('length_minutes', '0'))
            except (ValueError, TypeError):
                minutes = 0

            ctx = _make_field_ctx(event, field_name)
            if hours < 0 or hours > 168 or minutes not in (0, 15, 30, 45):
                ctx['error'] = 'Invalid race length values.'
                return render(request, 'partials/field_edit_form.html', ctx)
            total = hours * 3600 + minutes * 60
            if total == 0:
                ctx['error'] = 'Race length must be greater than zero.'
                return render(request, 'partials/field_edit_form.html', ctx)
            event.length_seconds = total
            event.save(update_fields=['length_seconds'])
            ctx = _make_field_ctx(event, field_name)
            return render(request, 'partials/field_display.html', ctx)

        value_str = request.POST.get('value', '')
        error = _validate_and_save_field(event, field_name, value_str)
        ctx = _make_field_ctx(event, field_name)
        if error:
            ctx['error'] = error
            return render(request, 'partials/field_edit_form.html', ctx)
        return render(request, 'partials/field_display.html', ctx)

    return HttpResponse(status=405)


def admin_remove_driver(request, event_id, driver_id):
    if request.method != 'DELETE':
        return HttpResponse(status=405)
    event = require_admin_session(request, event_id)
    Driver.objects.filter(id=driver_id, event=event).delete()

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

    table_html = render_to_string(
        'partials/availability_table.html',
        {'table_data': table_data, 'drivers': drivers, 'admin_tz': admin_tz},
        request=request,
    )
    response = HttpResponse(
        f'<div hx-swap-oob="innerHTML:#availability-table-container">{table_html}</div>'
    )
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
        driver_list_html = render_to_string(
            'partials/driver_list.html',
            {'drivers': drivers_qs, 'event': event, 'admin_tz': admin_tz},
            request=request,
        )
        error_html = f'<div class="text-red-400 text-sm mb-3 px-2">⚠ {error_msg}</div>'
        return HttpResponse(error_html + driver_list_html)

    driver = Driver.objects.create(event=event, name=driver_name, timezone=timezone)
    if slots_raw:
        _save_availability(driver, slots_raw, event)

    # Refresh driver list after creation
    drivers_qs = (
        Driver.objects.filter(event=event)
        .prefetch_related('availability')
        .annotate(stint_count=Count('stint_assignments'))
        .order_by('signed_up_at')
    )
    slots = get_availability_slots(event)
    availability_matrix, uncovered_slots = _build_availability_matrix(drivers_qs, slots)
    table_data = _build_table_data(slots, uncovered_slots, availability_matrix, drivers_qs, admin_tz)

    driver_list_html = render_to_string(
        'partials/driver_list.html',
        {'drivers': drivers_qs, 'event': event, 'admin_tz': admin_tz},
        request=request,
    )
    table_html = render_to_string(
        'partials/availability_table.html',
        {'table_data': table_data, 'drivers': drivers_qs, 'admin_tz': admin_tz},
        request=request,
    )
    return HttpResponse(
        driver_list_html
        + f'<div hx-swap-oob="innerHTML:#availability-table-container">{table_html}</div>'
    )


def create_stints(request, event_id):
    """
    GET: render the create stints page with stint table and availability table.
    POST: save stint assignments, re-render with updated state.
    """
    event = require_admin_session(request, event_id)

    if not event.has_required_stint_fields:
        messages.error(
            request,
            "All required timing and fuel fields must be set before creating stints.",
        )
        return redirect('admin_dashboard', event_id=event_id)

    stint_windows = get_stint_windows(event)

    drivers = Driver.objects.filter(event=event).prefetch_related('availability')

    if request.method == 'POST':
        StintAssignment.objects.filter(event=event).delete()

        assignments_to_create = []
        for sw in stint_windows:
            n = sw['stint_number']
            driver_id = request.POST.get(f'stint_{n}', '').strip()
            driver = None
            if driver_id:
                try:
                    driver = Driver.objects.get(id=driver_id, event=event)
                except Driver.DoesNotExist:
                    pass
            assignments_to_create.append(
                StintAssignment(event=event, stint_number=n, driver=driver)
            )
        StintAssignment.objects.bulk_create(assignments_to_create)
        messages.success(request, "Stint assignments saved.")

    assignments = {
        sa.stint_number: sa.driver_id
        for sa in StintAssignment.objects.filter(event=event)
    }

    availability_data = {
        str(driver.id): [normalize_iso(a.slot_utc) for a in driver.availability.all()]
        for driver in drivers
    }

    admin_tz = request.COOKIES.get('admin_timezone', 'UTC')
    if admin_tz not in VALID_TIMEZONES:
        admin_tz = 'UTC'
    admin_tz_zone = ZoneInfo(admin_tz)
    for sw in stint_windows:
        sw['start_local'] = sw['start_utc'].astimezone(admin_tz_zone).strftime('%H:%M')

    slots = get_availability_slots(event)
    availability_matrix, uncovered_slots = _build_availability_matrix(drivers, slots)
    table_data = _build_table_data(slots, uncovered_slots, availability_matrix, drivers, admin_tz)

    sl = stint_length_seconds(event)
    stint_length_display = f"{int(sl // 60)}m {int(sl % 60)}s"

    context = {
        'event': event,
        'stint_windows': stint_windows,
        'drivers': drivers,
        'admin_tz': admin_tz,
        'stint_length_display': stint_length_display,
        'total_stints_count': len(stint_windows),
        'table_data': table_data,
        'stint_windows_json': json.dumps([
            {
                'stint_number': sw['stint_number'],
                'start_utc': normalize_iso(sw['start_utc']),
                'end_utc': normalize_iso(sw['end_utc']),
            }
            for sw in stint_windows
        ]),
        'availability_json': json.dumps(availability_data),
        'existing_assignments_json': json.dumps({
            str(k): str(v) for k, v in assignments.items() if v
        }),
        'drivers_json': json.dumps([
            {'id': str(d.id), 'name': d.name}
            for d in drivers
        ]),
    }

    return render(request, 'create_stints.html', context)
