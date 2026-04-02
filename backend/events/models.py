import uuid
from datetime import datetime, timedelta, timezone as dt_utc
from django.db import models
from django.utils.crypto import get_random_string


class Event(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    admin_key = models.CharField(max_length=20, editable=False)
    name = models.CharField(max_length=255)
    date = models.DateField()
    start_time_utc = models.TimeField()
    length_seconds = models.PositiveIntegerField()
    car = models.CharField(max_length=255, blank=True)
    track = models.CharField(max_length=255, blank=True)
    setup = models.TextField(blank=True)
    fuel_capacity = models.FloatField(null=True, blank=True)
    fuel_per_lap = models.FloatField(null=True, blank=True)
    tire_change_fuel_min = models.FloatField(null=True, blank=True)
    target_laps = models.PositiveIntegerField(null=True, blank=True)
    avg_lap_seconds = models.FloatField(null=True, blank=True)
    in_lap_seconds = models.FloatField(null=True, blank=True)
    out_lap_seconds = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.admin_key:
            self.admin_key = get_random_string(20)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    @property
    def start_datetime_utc(self):
        """Returns a timezone-aware datetime combining date and start_time_utc."""
        return datetime.combine(self.date, self.start_time_utc).replace(tzinfo=dt_utc.utc)

    @property
    def end_datetime_utc(self):
        return self.start_datetime_utc + timedelta(seconds=self.length_seconds)

    @property
    def has_required_stint_fields(self):
        """Returns True if all fields needed for stint calculation are set."""
        return all([
            self.fuel_capacity is not None,
            self.fuel_per_lap is not None,
            self.target_laps is not None,
            self.avg_lap_seconds is not None,
            self.in_lap_seconds is not None,
            self.out_lap_seconds is not None,
        ])


class Driver(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='drivers')
    name = models.CharField(max_length=255)
    timezone = models.CharField(max_length=100, default='UTC')
    signed_up_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.event.name})"


class Availability(models.Model):
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name='availability')
    slot_utc = models.DateTimeField()

    class Meta:
        unique_together = ('driver', 'slot_utc')
        ordering = ['slot_utc']

    def __str__(self):
        return f"{self.driver.name} available at {self.slot_utc}"


class StintAssignment(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='stint_assignments')
    stint_number = models.PositiveIntegerField()
    driver = models.ForeignKey(
        Driver,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stint_assignments',
    )

    class Meta:
        unique_together = ('event', 'stint_number')
        ordering = ['stint_number']

    def __str__(self):
        return f"Stint {self.stint_number} - {self.driver or 'Unassigned'}"
