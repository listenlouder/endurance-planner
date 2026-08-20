import uuid
from datetime import datetime, timedelta, timezone as dt_utc
from django.db import models
from django.utils.crypto import get_random_string
from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    discord_id = models.CharField(
        max_length=32, unique=True, null=True, blank=True
    )
    discord_username = models.CharField(
        max_length=100, blank=True
    )
    discord_avatar = models.CharField(
        max_length=200, blank=True
    )

    def __str__(self):
        return self.discord_username or self.username


class Event(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    admin_key = models.CharField(max_length=20, editable=False)
    name = models.CharField(max_length=255)
    date = models.DateField()
    start_time_utc = models.TimeField()
    race_start_time_utc = models.TimeField(null=True, blank=True)
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
    team_name = models.CharField(max_length=255, blank=True, default='')
    game = models.CharField(max_length=100, blank=True)
    recruiting = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        'User',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='admin_events'
    )

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
        """Race end time — alias for effective_end_datetime_utc. Both anchor to the
        effective (race) start, not the session start. Use effective_end_datetime_utc
        in new code; this property exists for backwards compatibility with templates
        and tests that predate the effective_* naming convention."""
        return self.effective_end_datetime_utc

    @property
    def effective_start_time_utc(self):
        """race_start_time_utc when set, otherwise falls back to start_time_utc."""
        return self.race_start_time_utc or self.start_time_utc

    @property
    def effective_start_datetime_utc(self):
        """Timezone-aware datetime using effective start time. Use for stint calculations."""
        return datetime.combine(self.date, self.effective_start_time_utc).replace(tzinfo=dt_utc.utc)

    @property
    def effective_end_datetime_utc(self):
        return self.effective_start_datetime_utc + timedelta(seconds=self.length_seconds)

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
    name = models.CharField(max_length=50)
    timezone = models.CharField(max_length=100, default='UTC')
    signed_up_at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(
        'User',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='driver_signups',
    )

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


class Feedback(models.Model):
    text = models.TextField()
    page_url = models.CharField(max_length=500, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    # ID of the last request the reporter's browser saw answered, sent by the
    # X-Last-Request-Id header from base.html. Turns "it broke when I saved"
    # into the exact log line and Sentry issue for that save.
    request_id = models.CharField(max_length=32, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-submitted_at']

    def __str__(self):
        return (
            f"Feedback at "
            f"{self.submitted_at:%Y-%m-%d %H:%M} — "
            f"{self.text[:50]}"
        )


class StintAssignment(models.Model):
    CONDITION_CHOICES = [
        ('dry',   'Dry'),
        ('mixed', 'Mixed'),
        ('wet',   'Wet'),
    ]

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='stint_assignments')
    stint_number = models.PositiveIntegerField()
    driver = models.ForeignKey(
        Driver,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stint_assignments',
    )
    condition = models.CharField(
        max_length=10,
        choices=CONDITION_CHOICES,
        default='dry',
    )
    actual_start_utc = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            'Manual override for stint start time. When set, this '
            'and all subsequent stints cascade from this time.'
        ),
    )

    class Meta:
        unique_together = ('event', 'stint_number')
        ordering = ['stint_number']

    def __str__(self):
        return f"Stint {self.stint_number} - {self.driver or 'Unassigned'}"


class ActivityLog(models.Model):
    """
    One row per handled request: what was done, by whom, and what came back.

    This is a usage record, not a log stream. The two answer different
    questions — the stdout stream answers "what happened at 14:02", which
    needs no schema and can expire; this table answers "how many people opened
    the signup form and never submitted it", which needs a queryable shape and
    has to survive long enough to compare months.

    Deliberately not a Feedback-style anonymous table: attribution is the
    point. Two asymmetries in how that attribution is stored:

      * `user` is a real FK because Discord users are never deleted here and
        select_related() keeps the dashboard to one query.
      * `event_id_ref` is a bare UUID, NOT a FK. Deleting an event is itself
        an action worth recording; a FK would either cascade that history away
        or null out the one identifier needed to read it.
    """

    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    request_id = models.CharField(max_length=32, blank=True)
    action = models.CharField(max_length=64)
    method = models.CharField(max_length=8, blank=True)
    status_code = models.PositiveSmallIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )
    visitor_id = models.CharField(max_length=32, blank=True)
    event_id_ref = models.UUIDField(null=True, blank=True, db_index=True)
    path = models.CharField(max_length=300, blank=True)
    is_htmx = models.BooleanField(default=False)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        # The id tiebreak matters: two rows written in the same tick would
        # otherwise come back in arbitrary order, and a trail read out of
        # order is worse than no trail.
        ordering = ['-occurred_at', '-id']
        indexes = [
            # The two queries the dashboard runs: counts per action over a
            # window, and one visitor's trail in order. Both are composite
            # and both lead with the column they filter on, so neither
            # field needs a single-column index of its own.
            #
            # request_id is deliberately NOT indexed. It is looked up only
            # when someone pastes an ID into the dashboard, and an index
            # would be maintained on every request to speed up a query run
            # a few times a month.
            models.Index(fields=['action', '-occurred_at'],
                         name='activity_action_time_idx'),
            models.Index(fields=['visitor_id', '-occurred_at'],
                         name='activity_visitor_time_idx'),
        ]

    def __str__(self):
        return f"{self.occurred_at:%Y-%m-%d %H:%M} {self.action} -> {self.status_code}"

    @property
    def is_error(self):
        return self.status_code >= 400
