from django import forms
from django.core.exceptions import ValidationError
from datetime import datetime, timezone as dt_timezone


class EventCreateForm(forms.Form):
    name = forms.CharField(max_length=255, label='Event name')
    team_name = forms.CharField(max_length=255, required=False, label='Team name')
    game = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={
            'placeholder': 'e.g. iRacing, LMU, ACC'
        })
    )
    car = forms.CharField(max_length=255, required=False, label='Car')
    track = forms.CharField(max_length=255, required=False, label='Track')
    date = forms.DateField(
        widget=forms.DateInput(attrs={'type': 'date'}),
        label='Race date',
    )
    start_time_utc = forms.TimeField(
        widget=forms.TimeInput(attrs={'type': 'time'}),
        label='Start time (UTC)',
    )
    length_hours = forms.IntegerField(
        min_value=0,
        max_value=168,
        initial=0,
        label='Race Length',
        required=True,
        widget=forms.NumberInput(attrs={
            'placeholder': '0',
            'min': '0',
            'max': '168',
            'style': 'width: 80px;',
            'class': 'length-input',
        })
    )
    length_minutes = forms.IntegerField(
        min_value=0,
        max_value=59,
        initial=0,
        required=False,
        widget=forms.NumberInput(attrs={
            'placeholder': '0',
            'min': '0',
            'max': '59',
            'style': 'width: 80px;',
            'class': 'length-input',
        })
    )

    recruiting = forms.BooleanField(
        required=False,
        initial=False,
        label='List this event as recruiting drivers',
        help_text=(
            'Shows your event on the home page so drivers '
            'can find and sign up for it.'
        ),
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

        hours = cleaned_data.get('length_hours') or 0
        minutes = cleaned_data.get('length_minutes') or 0

        if hours == 0 and minutes == 0:
            raise ValidationError('Race length must be greater than zero.')
        if minutes > 59:
            self.add_error('length_minutes', 'Minutes must be between 0 and 59.')

        cleaned_data['length_seconds'] = (hours * 3600) + (minutes * 60)
        return cleaned_data
