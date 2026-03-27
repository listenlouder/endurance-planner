from django.core.exceptions import ValidationError
from django.shortcuts import render, redirect
from django.http import HttpResponse

from .forms import EventCreateForm
from .models import Event


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
