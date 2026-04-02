from django import forms
from django.core.exceptions import ValidationError
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
        min_value=0, max_value=168,
        label='Race length',
        widget=forms.Select(choices=[(i, str(i)) for i in range(0, 169)])
    )
    length_minutes = forms.IntegerField(
        min_value=0, max_value=59,
        initial=0,
        widget=forms.Select(
            choices=[(0, '00'), (15, '15'), (30, '30'), (45, '45')]
        )
    )

    def clean(self):
        cleaned_data = super().clean()
        date = cleaned_data.get('date')
        start_time = cleaned_data.get('start_time_utc')

        now_utc = datetime.now(tz=dt_timezone.utc)

        if date and date < now_utc.date():
            self.add_error('date', 'Event date cannot be in the past.')

        if date and start_time and date == now_utc.date():
            event_start = datetime.combine(date, start_time, tzinfo=dt_timezone.utc)
            if event_start <= now_utc:
                self.add_error(
                    'start_time_utc',
                    'Start time is in the past. Please choose a future start time.'
                )

        hours = cleaned_data.get('length_hours')
        minutes = cleaned_data.get('length_minutes')
        if hours is not None and minutes is not None:
            if hours == 0 and minutes == 0:
                raise ValidationError('Race length must be greater than zero.')
            cleaned_data['length_seconds'] = (hours * 3600) + (minutes * 60)

        return cleaned_data
