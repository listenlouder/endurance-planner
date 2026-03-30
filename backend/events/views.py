import hmac
import json
from datetime import datetime
from zoneinfo import available_timezones

from django.core.exceptions import ValidationError
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render, redirect

from .forms import EventCreateForm
from .models import Availability, Driver, Event
from .utils import get_availability_slots


# ---------------------------------------------------------------------------
# Phase 4: Admin constants and helpers
# ---------------------------------------------------------------------------

EDITABLE_FIELDS = {
    'name':               {'type': 'text',     'label': 'Event Name',                 'required': True},
    'date':               {'type': 'date',     'label': 'Date',                       'required': True},
    'start_time_utc':     {'type': 'time',     'label': 'Start Time (UTC)',            'required': True},
    'length_hours':       {'type': 'number',   'label': 'Race Length (hours)',         'required': True,  'min': 1,    'max': 168},
    'car':                {'type': 'text',     'label': 'Car',                        'required': False},
    'track':              {'type': 'text',     'label': 'Track',                      'required': False},
    'setup':              {'type': 'textarea', 'label': 'Setup Notes',                'required': False},
    'avg_lap_seconds':    {'type': 'number',   'label': 'Average Lap Time (s)',       'required': False, 'min': 1},
    'in_lap_seconds':     {'type': 'number',   'label': 'In Lap Time (s)',            'required': False, 'min': 1},
    'out_lap_seconds':    {'type': 'number',   'label': 'Out Lap Time (s)',           'required': False, 'min': 1},
    'target_laps':        {'type': 'number',   'label': 'Target Laps per Stint',      'required': False, 'min': 1},
    'fuel_capacity':      {'type': 'number',   'label': 'Fuel Capacity (L)',          'required': False, 'min': 0.1},
    'fuel_per_lap':       {'type': 'number',   'label': 'Fuel Use per Lap (L)',       'required': False, 'min': 0.01},
    'tire_change_fuel_min': {'type': 'number', 'label': 'Min Fuel for Tyre Change (L)', 'required': False, 'min': 0},
}

_REQUIRED_FOR_STINTS = [
    'avg_lap_seconds', 'in_lap_seconds', 'out_lap_seconds',
    'target_laps', 'fuel_capacity', 'fuel_per_lap',
]


def _check_admin_key(event, admin_key):
    return hmac.compare_digest(str(admin_key), str(event.admin_key))


def _get_field_display_value(event, field_name):
    """Return the display value for a field (handles length_hours conversion)."""
    if field_name == 'length_hours':
        return event.length_seconds / 3600
    value = getattr(event, field_name)
    return '' if value is None else value


def _build_availability_matrix(drivers, slots):
    matrix = {}
    all_covered = set()
    for driver in drivers:
        driver_slots = set(a.slot_utc for a in driver.availability.all())
        matrix[driver.id] = driver_slots
        all_covered |= driver_slots
    uncovered = set(slots) - all_covered
    return matrix, uncovered


def _make_field_ctx(event, field_name):
    """Build the partial context dict for a single field."""
    config = EDITABLE_FIELDS[field_name]
    return {
        'event': event,
        'field_name': field_name,
        'field_label': config['label'],
        'field_type': config['type'],
        'field_min': str(config['min']) if 'min' in config else '',
        'field_max': str(config['max']) if 'max' in config else '',
        'current_value': _get_field_display_value(event, field_name),
    }


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
            from datetime import date
            event.date = date.fromisoformat(value)
        except ValueError:
            return "Invalid date. Use YYYY-MM-DD format."
        event.save(update_fields=['date'])
        return None

    if ftype == 'time':
        if not value:
            return f"{config['label']} is required."
        try:
            from datetime import time as time_type
            event.start_time_utc = time_type.fromisoformat(value)
        except ValueError:
            return "Invalid time. Use HH:MM format."
        event.save(update_fields=['start_time_utc'])
        return None

    if ftype == 'number':
        if not value:
            if required:
                return f"{config['label']} is required."
            # Optional number — clear it
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
            event.length_seconds = int(num * 3600)
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
        'slot_timestamps_json': json.dumps([s.isoformat() for s in slots]),
        'timezone_list': sorted(available_timezones()),
    }


def _save_availability(driver, slots_raw, event):
    """Create Availability records for a driver from a list of ISO timestamp strings."""
    valid_slot_set = {s.isoformat() for s in get_availability_slots(event)}
    objects = []
    for slot_str in slots_raw:
        if slot_str in valid_slot_set:
            objects.append(Availability(driver=driver, slot_utc=datetime.fromisoformat(slot_str)))
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
    if request.method == 'POST':
        form = EventCreateForm(request.POST)
        if form.is_valid():
            event = Event(
                name=form.cleaned_data['name'],
                date=form.cleaned_data['date'],
                start_time_utc=form.cleaned_data['start_time_utc'],
                length_seconds=form.cleaned_data['length_hours'] * 3600,
            )
            event.save()
            return render(request, 'event_create.html', {
                'form': form,
                'success': True,
                'event': event,
                'base_url': request.build_absolute_uri('/'),
            })
    else:
        form = EventCreateForm()
    return render(request, 'event_create.html', {'form': form})


def event_lookup_by_id(request):
    """HTMX endpoint. POST with 'event_id' field."""
    if not request.htmx:
        return redirect('home')

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


def event_lookup(request, event_id):
    # Stub — detailed implementation in a future phase
    return redirect('home')


# ---------------------------------------------------------------------------
# Phase 3: signup views
# ---------------------------------------------------------------------------

def signup(request, event_id):
    """GET: render signup form. POST: validate and save driver + availability."""
    event = get_object_or_404(Event, id=event_id)

    if request.method == 'POST':
        cleaned, errors = _validate_signup_post(request.POST)

        if not errors:
            driver = Driver.objects.create(
                event=event,
                name=cleaned['driver_name'],
                timezone=cleaned['timezone'],
            )
            _save_availability(driver, cleaned['slots_raw'], event)

            ctx = get_signup_context(event)
            ctx.update({
                'event': event,
                'success': True,
                'driver': driver,
                'base_url': request.build_absolute_uri('/'),
            })
            return render(request, 'signup.html', ctx)

        # Re-render with submitted slots preserved so user doesn't lose selections
        ctx = get_signup_context(event)
        ctx.update({
            'event': event,
            'errors': errors,
            'submitted_name': cleaned['driver_name'],
            'submitted_slot_timestamps': json.dumps(cleaned['slots_raw']),
        })
        return render(request, 'signup.html', ctx)

    ctx = get_signup_context(event)
    ctx['event'] = event
    return render(request, 'signup.html', ctx)


def signup_edit(request, event_id, driver_id):
    """GET: edit form pre-populated. POST: replace availability."""
    event = get_object_or_404(Event, id=event_id)
    driver = get_object_or_404(Driver, id=driver_id, event=event)

    def _existing_timestamps():
        return json.dumps([a.slot_utc.isoformat() for a in driver.availability.all()])

    if request.method == 'POST':
        cleaned, errors = _validate_signup_post(request.POST)

        if not errors:
            driver.name = cleaned['driver_name']
            driver.timezone = cleaned['timezone']
            driver.save()

            driver.availability.all().delete()
            _save_availability(driver, cleaned['slots_raw'], event)

            ctx = get_signup_context(event)
            ctx.update({
                'event': event,
                'driver': driver,
                'success': True,
                'base_url': request.build_absolute_uri('/'),
                'existing_slot_timestamps': _existing_timestamps(),
            })
            return render(request, 'signup_edit.html', ctx)

        # On error, restore submitted selections so user doesn't lose changes
        ctx = get_signup_context(event)
        ctx.update({
            'event': event,
            'driver': driver,
            'errors': errors,
            'submitted_name': cleaned['driver_name'],
            'existing_slot_timestamps': json.dumps(cleaned['slots_raw']),
        })
        return render(request, 'signup_edit.html', ctx)

    ctx = get_signup_context(event)
    ctx.update({
        'event': event,
        'driver': driver,
        'existing_slot_timestamps': _existing_timestamps(),
    })
    return render(request, 'signup_edit.html', ctx)


def driver_delete(request, event_id, driver_id):
    if request.method == 'DELETE':
        driver = get_object_or_404(Driver, id=driver_id, event__id=event_id)
        driver.delete()
        response = HttpResponse()
        response['HX-Redirect'] = '/'
        return response
    return HttpResponse(status=405)


# ---------------------------------------------------------------------------
# Phase 4: Admin views
# ---------------------------------------------------------------------------

def set_timezone(request):
    tz = request.GET.get('timezone', 'UTC')
    current_tz = request.COOKIES.get('admin_timezone', 'UTC')
    response = HttpResponse()
    response.set_cookie('admin_timezone', tz, samesite='Lax')
    if current_tz != tz:
        response['HX-Refresh'] = 'true'
    return response


def admin_page(request, event_id, admin_key):
    event = get_object_or_404(Event, id=event_id)

    if not _check_admin_key(event, admin_key):
        return render(request, 'admin_error.html', {'message': 'Invalid admin key supplied.'})

    drivers = (
        Driver.objects.filter(event=event)
        .prefetch_related('availability')
        .annotate(stint_count=Count('stint_assignments'))
        .order_by('signed_up_at')
    )
    slots = get_availability_slots(event)
    availability_matrix, uncovered_slots = _build_availability_matrix(drivers, slots)
    admin_tz = request.COOKIES.get('admin_timezone', 'UTC')

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
                for name in ['car', 'track', 'setup']
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

    table_data = [
        {
            'slot_utc': slot,
            'is_uncovered': slot in uncovered_slots,
            'driver_availability': {
                driver.id: slot in availability_matrix[driver.id]
                for driver in drivers
            },
        }
        for slot in slots
    ]

    return render(request, 'admin.html', {
        'event': event,
        'admin_key': admin_key,
        'admin_tz': admin_tz,
        'drivers': drivers,
        'field_groups': field_groups,
        'missing_required_fields': missing_required_fields,
        'table_data': table_data,
    })


def admin_edit_field(request, event_id, admin_key):
    event = get_object_or_404(Event, id=event_id)
    if not _check_admin_key(event, admin_key):
        return HttpResponse(status=403)

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
        value_str = request.POST.get('value', '')
        error = _validate_and_save_field(event, field_name, value_str)
        ctx = _make_field_ctx(event, field_name)
        if error:
            ctx['error'] = error
            return render(request, 'partials/field_edit_form.html', ctx)
        return render(request, 'partials/field_display.html', ctx)

    return HttpResponse(status=405)


def admin_remove_driver(request, event_id, admin_key, driver_id):
    if request.method != 'DELETE':
        return HttpResponse(status=405)
    event = get_object_or_404(Event, id=event_id)
    if not _check_admin_key(event, admin_key):
        return HttpResponse(status=403)
    driver = get_object_or_404(Driver, id=driver_id, event__id=event_id)
    driver.delete()
    return HttpResponse('')
