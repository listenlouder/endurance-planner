"""
Re-align stored availability onto the wall-clock half-hour slot grid.

The availability grid used to be anchored to an event's exact start time, so an
event starting at 12:15 had slots at :15/:45. utils.slot_grid_anchor() now
floors that anchor to the wall-clock half hour, which means those stored slots
would no longer land on any grid boundary and every driver would read as
unavailable.

For each affected event — one whose start_time_utc minutes are not 0 or 30 —
shift its drivers' availability back by the same offset the anchor moved, so
each stored slot lands on the corresponding boundary of the new grid. Events
already starting on a half hour have an offset of zero and are untouched.
"""
from datetime import timedelta

from django.db import migrations


def _offset_minutes(start_time):
    """How far the event start sits past its wall-clock half-hour boundary."""
    return start_time.minute % 30


def realign(apps, schema_editor):
    Event = apps.get_model('events', 'Event')
    Availability = apps.get_model('events', 'Availability')

    for event in Event.objects.all().iterator():
        offset = _offset_minutes(event.start_time_utc)
        if offset == 0:
            continue

        rows = list(
            Availability.objects.filter(driver__event=event).only('id', 'slot_utc')
        )
        if not rows:
            continue

        delta = timedelta(minutes=offset)
        for row in rows:
            row.slot_utc = row.slot_utc - delta

        # Shifting every row of an event by the same delta preserves the
        # (driver, slot_utc) uniqueness constraint.
        Availability.objects.bulk_update(rows, ['slot_utc'], batch_size=500)


def unrealign(apps, schema_editor):
    """Shift back onto the old exact-start grid."""
    Event = apps.get_model('events', 'Event')
    Availability = apps.get_model('events', 'Availability')

    for event in Event.objects.all().iterator():
        offset = _offset_minutes(event.start_time_utc)
        if offset == 0:
            continue

        rows = list(
            Availability.objects.filter(driver__event=event).only('id', 'slot_utc')
        )
        if not rows:
            continue

        delta = timedelta(minutes=offset)
        for row in rows:
            row.slot_utc = row.slot_utc + delta

        Availability.objects.bulk_update(rows, ['slot_utc'], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0008_add_stint_start_override'),
    ]

    operations = [
        migrations.RunPython(realign, unrealign),
    ]
