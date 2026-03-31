from django import forms
from datetime import datetime, timezone as dt_timezone


class EventCreateForm(forms.Form):
    name = forms.CharField(max_length=255, label='Event name')
    date = forms.DateField(
        widget=forms.DateInput(attrs={'type': 'date'}),
        label='Race date',
    )
    start_time_utc = forms.TimeField(
        widget=forms.TimeInput(attrs={'type': 'time'}),
        label='Start time (UTC)',
    )
    length_hours = forms.IntegerField(
        min_value=1,
        max_value=168,
        label='Race length (hours)',
    )

    def clean(self):
        cleaned_data = super().clean()
        event_date = cleaned_data.get('date')
        if event_date and event_date < datetime.now(tz=dt_timezone.utc).date():
            self.add_error('date', 'Event date cannot be in the past.')
        return cleaned_data
