import json
from datetime import datetime
from zoneinfo import available_timezones

from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render, redirect

from .forms import EventCreateForm
from .models import Availability, Driver, Event
from .utils import get_availability_slots


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
