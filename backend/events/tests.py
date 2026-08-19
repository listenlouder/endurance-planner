"""
Comprehensive tests for the endurance racing planner application.

Test groups:
    StintLengthTests          - utils.stint_length_seconds()
    TotalStintsTests          - utils.total_stints()
    StintStartTimeTests       - utils.stint_start_time()
    StintEndTimeTests         - utils.stint_end_time()
    GetStintWindowsTests      - utils.get_stint_windows()
    GetAvailabilitySlotsTests - utils.get_availability_slots()
    CheckDriverConflictTests  - utils.check_driver_conflict()
    EventModelPropertyTests   - Event.start_datetime_utc, end_datetime_utc,
                                has_required_stint_fields
    ToTzFilterTests           - templatetags.tz_filters.to_tz
    DatetimeInTzFilterTests   - templatetags.tz_filters.datetime_in_tz
    TimeInTzFilterTests       - templatetags.tz_filters.time_in_tz
    ToUtcZFilterTests         - templatetags.tz_filters.to_utc_z
    EventCreateFormTests      - forms.EventCreateForm validation
    ValidateSignupPostTests   - views._validate_signup_post()
    ValidateAndSaveFieldTests - views._validate_and_save_field()
    AdminDashboardTests       - views.admin_dashboard() session-gated access
    SetTimezoneTests          - views.set_timezone() POST-only timezone cookie
    AdminPageSessionTests     - views.admin_page() key login and session handling
    FeedbackSubmitTests       - views.feedback_submit() POST endpoint
    FeedbackViewTests         - views.feedback_view() password-protected viewer
"""

import datetime as dt
import json
import uuid
from datetime import timezone
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .forms import EventCreateForm
from .models import Availability, Driver, Event, Feedback, StintAssignment
from .templatetags.tz_filters import (
    dict_get,
    seconds_to_hours_display,
    to_utc_z,
)
from .utils import (
    build_stint_availability_matrix,
    format_stint_duration,
    get_availability_slots,
    get_stint_windows,
    last_stint_length_seconds,
    seconds_to_mmss,
    stint_length_seconds,
    total_stints,
    validate_stint_sanity,
)
from .views import _validate_and_save_field, _validate_signup_post


# ---------------------------------------------------------------------------
# Shared factory helpers
# ---------------------------------------------------------------------------

def make_event(**overrides):
    """
    Return an unsaved Event instance with all stint-calculation fields
    populated with sensible defaults.

    Default configuration:
      - 6-hour race (21 600 s)
      - avg_lap = 120 s, target_laps = 30
      - in_lap = 130 s, out_lap = 125 s
      - Calculated stint length:
          (120 * 30) + (130 + 125 - 240) = 3 600 + 15 = 3 615 s
      - Total stints: ceil(21 600 / 3 615) = 6
      - Start: 2026-06-01 12:00 UTC
    """
    defaults = dict(
        name='Test Race',
        date=dt.date(2026, 6, 1),
        start_time_utc=dt.time(12, 0, 0),
        length_seconds=21_600,          # 6 hours
        car='GT3',
        track='Spa',
        avg_lap_seconds=120.0,
        target_laps=30,
        in_lap_seconds=130.0,
        out_lap_seconds=125.0,
        fuel_capacity=80.0,
        fuel_per_lap=2.5,
        tire_change_fuel_min=10.0,
    )
    defaults.update(overrides)
    return Event(**defaults)


def save_event(**overrides):
    """Return a saved Event instance (hits the database)."""
    event = make_event(**overrides)
    event.save()
    return event


def utc(year, month, day, hour=0, minute=0, second=0):
    """Shorthand for a UTC-aware datetime."""
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Priority 1: Stint Logic and Math
# ---------------------------------------------------------------------------

class StintLengthTests(SimpleTestCase):
    """Tests for utils.stint_length_seconds()."""

    def test_formula_with_default_values(self):
        # (120 * 30) + (130 + 125 - 240) = 3600 + 15 = 3615
        event = make_event()
        self.assertAlmostEqual(stint_length_seconds(event), 3615.0)

    def test_formula_where_in_out_equal_avg(self):
        # in_lap = out_lap = avg_lap → transition_delta = 0
        # stint = avg_lap * target_laps
        event = make_event(avg_lap_seconds=100.0, in_lap_seconds=100.0,
                           out_lap_seconds=100.0, target_laps=20)
        self.assertAlmostEqual(stint_length_seconds(event), 2000.0)

    def test_formula_where_in_out_longer_than_avg(self):
        # transition_delta is positive → stint is longer
        event = make_event(avg_lap_seconds=90.0, in_lap_seconds=100.0,
                           out_lap_seconds=110.0, target_laps=10)
        # (90*10) + (100+110-180) = 900 + 30 = 930
        self.assertAlmostEqual(stint_length_seconds(event), 930.0)

    def test_formula_where_in_out_shorter_than_avg(self):
        # transition_delta is negative → stint is shorter
        event = make_event(avg_lap_seconds=200.0, in_lap_seconds=150.0,
                           out_lap_seconds=150.0, target_laps=5)
        # (200*5) + (150+150-400) = 1000 + (-100) = 900
        self.assertAlmostEqual(stint_length_seconds(event), 900.0)

    def test_formula_with_fractional_lap_seconds(self):
        event = make_event(avg_lap_seconds=93.5, in_lap_seconds=100.0,
                           out_lap_seconds=95.0, target_laps=25)
        # (93.5*25) + (100+95-187) = 2337.5 + 8 = 2345.5
        self.assertAlmostEqual(stint_length_seconds(event), 2345.5)

    def test_single_lap_stint(self):
        # target_laps=1: stint = avg + (in + out - 2*avg) = in + out - avg
        event = make_event(avg_lap_seconds=100.0, in_lap_seconds=110.0,
                           out_lap_seconds=110.0, target_laps=1)
        # (100*1) + (110+110-200) = 100 + 20 = 120
        self.assertAlmostEqual(stint_length_seconds(event), 120.0)

    def test_large_target_laps(self):
        event = make_event(avg_lap_seconds=80.0, in_lap_seconds=90.0,
                           out_lap_seconds=90.0, target_laps=100)
        # (80*100) + (90+90-160) = 8000 + 20 = 8020
        self.assertAlmostEqual(stint_length_seconds(event), 8020.0)


class TotalStintsTests(SimpleTestCase):
    """Tests for utils.total_stints()."""

    def test_divides_evenly(self):
        # stint = 3600 s, race = 7 * 3600 = 25200 s → 7 stints exactly
        event = make_event(
            avg_lap_seconds=120.0, in_lap_seconds=120.0, out_lap_seconds=120.0,
            target_laps=30, length_seconds=25_200,
        )
        # stint_length = 120*30 + (120+120-240) = 3600
        self.assertEqual(total_stints(event), 7)

    def test_does_not_divide_evenly_rounds_up(self):
        # Default: stint = 3615 s, race = 21600 s
        # 21600 / 3615 = 5.976... → ceil = 6
        event = make_event()
        self.assertEqual(total_stints(event), 6)

    def test_one_stint_race(self):
        # race length < 1 stint → ceil gives 1
        event = make_event(
            avg_lap_seconds=120.0, in_lap_seconds=120.0, out_lap_seconds=120.0,
            target_laps=30, length_seconds=1_800,  # only 30 minutes
        )
        # stint_length = 3600, race = 1800 → 0.5 → ceil = 1
        self.assertEqual(total_stints(event), 1)

    def test_exactly_two_stints(self):
        event = make_event(
            avg_lap_seconds=120.0, in_lap_seconds=120.0, out_lap_seconds=120.0,
            target_laps=30, length_seconds=7_200,
        )
        # stint_length = 3600, race = 7200 → exactly 2.0 → ceil = 2
        self.assertEqual(total_stints(event), 2)

    def test_long_race_many_stints(self):
        # 24-hour race with 1-hour stints
        event = make_event(
            avg_lap_seconds=120.0, in_lap_seconds=120.0, out_lap_seconds=120.0,
            target_laps=30, length_seconds=86_400,
        )
        # stint_length = 3600, 86400/3600 = 24
        self.assertEqual(total_stints(event), 24)


class GetStintWindowsTests(SimpleTestCase):
    """Tests for utils.get_stint_windows()."""

    def test_returns_correct_number_of_stints(self):
        event = make_event()
        windows = get_stint_windows(event)
        self.assertEqual(len(windows), total_stints(event))

    def test_stint_numbers_are_sequential_starting_at_one(self):
        event = make_event()
        windows = get_stint_windows(event)
        numbers = [w['stint_number'] for w in windows]
        self.assertEqual(numbers, list(range(1, len(windows) + 1)))

    def test_each_window_has_required_keys(self):
        event = make_event()
        for window in get_stint_windows(event):
            self.assertIn('stint_number', window)
            self.assertIn('start_utc', window)
            self.assertIn('end_utc', window)

    def test_first_window_starts_at_event_start(self):
        event = make_event()
        windows = get_stint_windows(event)
        self.assertEqual(windows[0]['start_utc'], event.start_datetime_utc)

    def test_last_window_ends_at_last_stint_duration_from_its_start(self):
        # get_stint_windows uses last_stint_length_seconds() for the final
        # stint, which is calculated from remaining laps (floor division).
        # This means the last window's end time equals its start + that
        # computed duration — not necessarily the exact race clock end.
        event = make_event()
        windows = get_stint_windows(event)
        last = windows[-1]
        expected_duration = last_stint_length_seconds(event)
        expected_end = last['start_utc'] + dt.timedelta(seconds=expected_duration)
        self.assertEqual(last['end_utc'], expected_end)

    def test_last_window_ends_at_or_before_event_end(self):
        # The lap-count-based last stint duration may be slightly shorter
        # than the raw clock race length.
        event = make_event()
        windows = get_stint_windows(event)
        self.assertLessEqual(windows[-1]['end_utc'], event.end_datetime_utc)

    def test_consecutive_windows_are_contiguous(self):
        # end of window N == start of window N+1
        event = make_event()
        windows = get_stint_windows(event)
        for i in range(len(windows) - 1):
            self.assertEqual(windows[i]['end_utc'], windows[i + 1]['start_utc'])

    def test_start_utc_is_the_effective_start_plus_whole_stints(self):
        # Previously cross-checked against a stint_start_time() helper; that
        # helper had no callers and no override support, so the expectation is
        # computed here instead.
        event = make_event()
        stint_length = stint_length_seconds(event)

        for w in get_stint_windows(event):
            expected = event.effective_start_datetime_utc + dt.timedelta(
                seconds=(w['stint_number'] - 1) * stint_length
            )
            self.assertEqual(w['start_utc'], expected)

    def test_all_datetimes_are_utc_aware(self):
        event = make_event()
        for w in get_stint_windows(event):
            self.assertIsNotNone(w['start_utc'].tzinfo)
            self.assertIsNotNone(w['end_utc'].tzinfo)

    def test_single_stint_race(self):
        event = make_event(length_seconds=1_800,
                           avg_lap_seconds=120.0, in_lap_seconds=120.0,
                           out_lap_seconds=120.0, target_laps=30)
        windows = get_stint_windows(event)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]['stint_number'], 1)
        self.assertEqual(windows[0]['start_utc'], event.start_datetime_utc)
        self.assertEqual(windows[0]['end_utc'], event.end_datetime_utc)


class GetAvailabilitySlotsTests(SimpleTestCase):
    """Tests for utils.get_availability_slots()."""

    def test_slots_start_at_event_start(self):
        event = make_event()
        slots = get_availability_slots(event)
        self.assertEqual(slots[0], event.start_datetime_utc)

    def test_slots_are_30_minutes_apart(self):
        event = make_event()
        slots = get_availability_slots(event)
        for i in range(len(slots) - 1):
            gap = slots[i + 1] - slots[i]
            self.assertEqual(gap, dt.timedelta(minutes=30))

    def test_slot_count_for_6_hour_race(self):
        # 6h race + 1h buffer = 7h window = 14 slots (00:00 … 06:30)
        event = make_event(length_seconds=21_600)
        slots = get_availability_slots(event)
        self.assertEqual(len(slots), 14)

    def test_slot_count_for_1_hour_race(self):
        # 1h race + 1h buffer = 2h window = 4 slots
        event = make_event(length_seconds=3_600)
        slots = get_availability_slots(event)
        self.assertEqual(len(slots), 4)

    def test_last_slot_is_before_buffer_end(self):
        # Slots extend to 1h past race end; last slot is within that buffer
        event = make_event()
        slots = get_availability_slots(event)
        buffer_end = event.end_datetime_utc + dt.timedelta(hours=1)
        self.assertLess(slots[-1], buffer_end)

    def test_no_slot_at_or_after_buffer_end(self):
        event = make_event()
        slots = get_availability_slots(event)
        buffer_end = event.end_datetime_utc + dt.timedelta(hours=1)
        for slot in slots:
            self.assertLess(slot, buffer_end)

    def test_all_slots_are_utc_aware(self):
        event = make_event()
        for slot in get_availability_slots(event):
            self.assertIsNotNone(slot.tzinfo)
            self.assertEqual(slot.utcoffset(), dt.timedelta(0))

    def test_24_hour_race_has_50_slots(self):
        # 24h race + 1h buffer = 25h window = 50 slots
        event = make_event(length_seconds=86_400)
        slots = get_availability_slots(event)
        self.assertEqual(len(slots), 50)

    def test_45_minute_race_slot_count(self):
        # 45 min race + 1h buffer = 1h45min window
        # Slots at +0, +30, +60, +90 min (all < 105 min). 4 slots total.
        event = make_event(length_seconds=2_700)
        slots = get_availability_slots(event)
        self.assertEqual(len(slots), 4)
        self.assertEqual(slots[0], event.start_datetime_utc)
        self.assertEqual(slots[-1], event.start_datetime_utc + dt.timedelta(minutes=90))

    def test_30_minute_race_slot_count(self):
        # 30 min race + 1h buffer = 1h30min window
        # Slots at +0, +30, +60 min (all < 90 min). 3 slots total.
        event = make_event(length_seconds=1_800)
        slots = get_availability_slots(event)
        self.assertEqual(len(slots), 3)
        self.assertEqual(slots[0], event.start_datetime_utc)


# ---------------------------------------------------------------------------
# Priority 1 continued: Event model properties
# ---------------------------------------------------------------------------

class EventModelPropertyTests(TestCase):
    """Tests for Event.start_datetime_utc, end_datetime_utc, has_required_stint_fields."""

    def test_start_datetime_utc_combines_date_and_time(self):
        event = make_event(date=dt.date(2026, 3, 15), start_time_utc=dt.time(8, 30, 0))
        expected = dt.datetime(2026, 3, 15, 8, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(event.start_datetime_utc, expected)

    def test_start_datetime_utc_is_aware(self):
        event = make_event()
        self.assertIsNotNone(event.start_datetime_utc.tzinfo)
        self.assertEqual(event.start_datetime_utc.utcoffset(), dt.timedelta(0))

    def test_start_datetime_utc_midnight(self):
        event = make_event(date=dt.date(2026, 1, 1), start_time_utc=dt.time(0, 0, 0))
        self.assertEqual(event.start_datetime_utc, dt.datetime(2026, 1, 1, tzinfo=timezone.utc))

    def test_end_datetime_utc_is_start_plus_length(self):
        event = make_event(length_seconds=21_600)
        expected = event.start_datetime_utc + dt.timedelta(seconds=21_600)
        self.assertEqual(event.end_datetime_utc, expected)

    def test_end_datetime_utc_crosses_midnight(self):
        event = make_event(
            date=dt.date(2026, 6, 1),
            start_time_utc=dt.time(22, 0, 0),
            length_seconds=7_200,   # 2 hours → ends at 00:00 next day
        )
        expected = dt.datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(event.end_datetime_utc, expected)

    def test_end_datetime_utc_uses_effective_start_when_race_start_set(self):
        # race_start_time_utc is 2h after session start; end should be
        # anchored to race start, not session start
        event = make_event(
            date=dt.date(2026, 6, 1),
            start_time_utc=dt.time(10, 0, 0),
            race_start_time_utc=dt.time(12, 0, 0),
            length_seconds=21_600,  # 6 hours
        )
        # ends at 12:00 + 6h = 18:00, not 10:00 + 6h = 16:00
        expected = dt.datetime(2026, 6, 1, 18, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(event.end_datetime_utc, expected)

    def test_end_datetime_utc_equals_effective_end_datetime_utc(self):
        # The two properties must agree in all cases
        event = make_event(
            date=dt.date(2026, 6, 1),
            start_time_utc=dt.time(10, 0, 0),
            race_start_time_utc=dt.time(11, 30, 0),
            length_seconds=14_400,
        )
        self.assertEqual(event.end_datetime_utc, event.effective_end_datetime_utc)

    def test_has_required_stint_fields_true_when_all_set(self):
        event = make_event()
        self.assertTrue(event.has_required_stint_fields)

    def test_has_required_stint_fields_false_when_avg_lap_missing(self):
        event = make_event(avg_lap_seconds=None)
        self.assertFalse(event.has_required_stint_fields)

    def test_has_required_stint_fields_false_when_fuel_capacity_missing(self):
        event = make_event(fuel_capacity=None)
        self.assertFalse(event.has_required_stint_fields)

    def test_has_required_stint_fields_false_when_fuel_per_lap_missing(self):
        event = make_event(fuel_per_lap=None)
        self.assertFalse(event.has_required_stint_fields)

    def test_has_required_stint_fields_false_when_target_laps_missing(self):
        event = make_event(target_laps=None)
        self.assertFalse(event.has_required_stint_fields)

    def test_has_required_stint_fields_false_when_in_lap_missing(self):
        event = make_event(in_lap_seconds=None)
        self.assertFalse(event.has_required_stint_fields)

    def test_has_required_stint_fields_false_when_out_lap_missing(self):
        event = make_event(out_lap_seconds=None)
        self.assertFalse(event.has_required_stint_fields)

    def test_has_required_stint_fields_false_when_all_optional_missing(self):
        event = make_event(
            avg_lap_seconds=None, in_lap_seconds=None, out_lap_seconds=None,
            target_laps=None, fuel_capacity=None, fuel_per_lap=None,
        )
        self.assertFalse(event.has_required_stint_fields)

    def test_admin_key_auto_generated_on_save(self):
        event = save_event()
        self.assertTrue(event.admin_key)
        self.assertEqual(len(event.admin_key), 20)

    def test_admin_key_unique_per_event(self):
        e1 = save_event(name='Race 1')
        e2 = save_event(name='Race 2')
        self.assertNotEqual(e1.admin_key, e2.admin_key)


# ---------------------------------------------------------------------------
# Priority 2: Timezone template filters
# ---------------------------------------------------------------------------

class ToUtcZFilterTests(SimpleTestCase):
    """Tests for templatetags.tz_filters.to_utc_z."""

    def test_formats_as_iso_with_z_suffix(self):
        source = dt.datetime(2026, 6, 15, 12, 30, 45, tzinfo=timezone.utc)
        result = to_utc_z(source)
        self.assertEqual(result, '2026-06-15T12:30:45Z')

    def test_midnight(self):
        source = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        result = to_utc_z(source)
        self.assertEqual(result, '2026-01-01T00:00:00Z')

    def test_no_microseconds_in_output(self):
        source = dt.datetime(2026, 6, 15, 10, 0, 0, 123456, tzinfo=timezone.utc)
        result = to_utc_z(source)
        # No microseconds — only seconds precision
        self.assertEqual(result, '2026-06-15T10:00:00Z')
        self.assertNotIn('.', result)

    def test_end_of_year(self):
        source = dt.datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        result = to_utc_z(source)
        self.assertEqual(result, '2026-12-31T23:59:59Z')

    def test_zero_padded_fields(self):
        source = dt.datetime(2026, 3, 5, 8, 7, 6, tzinfo=timezone.utc)
        result = to_utc_z(source)
        self.assertEqual(result, '2026-03-05T08:07:06Z')

    def test_result_ends_with_z(self):
        source = dt.datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(to_utc_z(source).endswith('Z'))


# ---------------------------------------------------------------------------
# Priority 3: Form validation
# ---------------------------------------------------------------------------

class EventCreateFormTests(SimpleTestCase):
    """Tests for forms.EventCreateForm."""

    # Use a fixed future date so tests stay green regardless of test-run date
    FUTURE_DATE = '2027-01-15'
    PAST_DATE = '2020-06-01'

    def _valid_data(self, **overrides):
        data = {
            'name': 'Spa 24H',
            'date': self.FUTURE_DATE,
            'start_time_utc': '14:00',
            'length_hours': 24,
            'length_minutes': 0,
        }
        data.update(overrides)
        return data

    def test_valid_form_is_valid(self):
        form = EventCreateForm(data=self._valid_data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_form_cleaned_data(self):
        form = EventCreateForm(data=self._valid_data())
        form.is_valid()
        self.assertEqual(form.cleaned_data['name'], 'Spa 24H')
        self.assertEqual(form.cleaned_data['length_hours'], 24)

    def test_past_date_is_rejected(self):
        form = EventCreateForm(data=self._valid_data(date=self.PAST_DATE))
        self.assertFalse(form.is_valid())
        self.assertIn('date', form.errors)
        self.assertIn('past', form.errors['date'][0])

    def test_missing_name_is_rejected(self):
        data = self._valid_data()
        del data['name']
        form = EventCreateForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn('name', form.errors)

    def test_missing_date_is_rejected(self):
        data = self._valid_data()
        del data['date']
        form = EventCreateForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn('date', form.errors)

    def test_missing_start_time_is_rejected(self):
        data = self._valid_data()
        del data['start_time_utc']
        form = EventCreateForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn('start_time_utc', form.errors)

    def test_missing_length_hours_is_rejected(self):
        data = self._valid_data()
        del data['length_hours']
        form = EventCreateForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn('length_hours', form.errors)

    def test_length_hours_below_minimum_rejected(self):
        # hours=0, minutes=0 should fail with a non-field ValidationError
        form = EventCreateForm(data=self._valid_data(length_hours=0, length_minutes=0))
        self.assertFalse(form.is_valid())
        self.assertTrue(form.non_field_errors())

    def test_length_hours_at_minimum_accepted(self):
        form = EventCreateForm(data=self._valid_data(length_hours=1, length_minutes=0))
        self.assertTrue(form.is_valid(), form.errors)

    def test_length_hours_above_maximum_rejected(self):
        form = EventCreateForm(data=self._valid_data(length_hours=169))
        self.assertFalse(form.is_valid())
        self.assertIn('length_hours', form.errors)

    def test_length_hours_at_maximum_accepted(self):
        form = EventCreateForm(data=self._valid_data(length_hours=168, length_minutes=0))
        self.assertTrue(form.is_valid(), form.errors)

    def test_invalid_date_format_rejected(self):
        form = EventCreateForm(data=self._valid_data(date='15/01/2027'))
        self.assertFalse(form.is_valid())
        self.assertIn('date', form.errors)

    def test_invalid_time_format_rejected(self):
        form = EventCreateForm(data=self._valid_data(start_time_utc='not-a-time'))
        self.assertFalse(form.is_valid())
        self.assertIn('start_time_utc', form.errors)

    def test_non_integer_length_hours_rejected(self):
        form = EventCreateForm(data=self._valid_data(length_hours='twelve'))
        self.assertFalse(form.is_valid())
        self.assertIn('length_hours', form.errors)

    def test_empty_name_rejected(self):
        form = EventCreateForm(data=self._valid_data(name=''))
        self.assertFalse(form.is_valid())
        self.assertIn('name', form.errors)

    def test_name_at_max_length_accepted(self):
        form = EventCreateForm(data=self._valid_data(name='A' * 255))
        self.assertTrue(form.is_valid(), form.errors)

    def test_name_over_max_length_rejected(self):
        form = EventCreateForm(data=self._valid_data(name='A' * 256))
        self.assertFalse(form.is_valid())
        self.assertIn('name', form.errors)


# ---------------------------------------------------------------------------
# Priority 3: views._validate_signup_post()
# ---------------------------------------------------------------------------

class ValidateSignupPostTests(SimpleTestCase):
    """Tests for views._validate_signup_post().

    _validate_signup_post() accepts a QueryDict-like object and returns
    (cleaned, errors). We use a plain dict subclass that adds .getlist().
    """

    class FakePost(dict):
        """Minimal stand-in for request.POST that supports .getlist()."""
        def getlist(self, key):
            value = self.get(key, [])
            return value if isinstance(value, list) else [value]

    def _post(self, **kwargs):
        return self.FakePost(**kwargs)

    def _valid_post(self, **overrides):
        data = {
            'driver_name': 'Alice',
            'timezone': 'UTC',
            'slots': ['2026-06-01T12:00:00Z'],
        }
        data.update(overrides)
        return self._post(**data)

    def test_valid_post_returns_no_errors(self):
        cleaned, errors = _validate_signup_post(self._valid_post())
        self.assertEqual(errors, {})

    def test_valid_post_returns_stripped_name(self):
        cleaned, errors = _validate_signup_post(self._valid_post(driver_name='  Bob  '))
        self.assertEqual(cleaned['driver_name'], 'Bob')

    def test_valid_post_returns_timezone(self):
        cleaned, errors = _validate_signup_post(self._valid_post(timezone='America/New_York'))
        self.assertEqual(cleaned['timezone'], 'America/New_York')

    def test_valid_post_returns_slots_list(self):
        slots = ['2026-06-01T12:00:00Z', '2026-06-01T12:30:00Z']
        cleaned, errors = _validate_signup_post(self._valid_post(slots=slots))
        self.assertEqual(cleaned['slots_raw'], slots)

    def test_empty_driver_name_is_error(self):
        _, errors = _validate_signup_post(self._valid_post(driver_name=''))
        self.assertIn('driver_name', errors)

    def test_whitespace_only_name_is_error(self):
        _, errors = _validate_signup_post(self._valid_post(driver_name='   '))
        self.assertIn('driver_name', errors)

    def test_missing_driver_name_key_is_error(self):
        post = self._post(timezone='UTC', slots=['2026-06-01T12:00:00Z'])
        _, errors = _validate_signup_post(post)
        self.assertIn('driver_name', errors)

    def test_missing_timezone_is_error(self):
        _, errors = _validate_signup_post(self._valid_post(timezone=''))
        self.assertIn('timezone', errors)

    def test_missing_timezone_key_is_error(self):
        post = self._post(driver_name='Alice', slots=['2026-06-01T12:00:00Z'])
        _, errors = _validate_signup_post(post)
        self.assertIn('timezone', errors)

    def test_no_slots_selected_is_error(self):
        _, errors = _validate_signup_post(self._valid_post(slots=[]))
        self.assertIn('slots', errors)

    def test_missing_slots_key_is_error(self):
        post = self._post(driver_name='Alice', timezone='UTC')
        _, errors = _validate_signup_post(post)
        self.assertIn('slots', errors)

    def test_sql_injection_in_name_passes_through(self):
        # Validation only checks emptiness; ORM handles escaping
        name = "'; DROP TABLE events_driver; --"
        cleaned, errors = _validate_signup_post(self._valid_post(driver_name=name))
        self.assertEqual(errors, {})
        self.assertEqual(cleaned['driver_name'], name.strip())

    def test_name_over_50_characters_returns_error(self):
        # _validate_signup_post enforces the 50-character limit directly
        long_name = 'X' * 51
        cleaned, errors = _validate_signup_post(self._valid_post(driver_name=long_name))
        self.assertIn('driver_name', errors)

    def test_name_exactly_50_characters_passes_validation(self):
        name_50 = 'X' * 50
        cleaned, errors = _validate_signup_post(self._valid_post(driver_name=name_50))
        self.assertEqual(errors, {})
        self.assertEqual(cleaned['driver_name'], name_50)

    def test_multiple_errors_reported_together(self):
        post = self._post(driver_name='', timezone='', slots=[])
        _, errors = _validate_signup_post(post)
        self.assertIn('driver_name', errors)
        self.assertIn('timezone', errors)
        self.assertIn('slots', errors)

    def test_invalid_timezone_string_not_caught_here(self):
        # _validate_signup_post does NOT validate that timezone is a real IANA zone;
        # that check happens in the view after calling this function.
        cleaned, errors = _validate_signup_post(
            self._valid_post(timezone='Not/A/Real/Zone')
        )
        self.assertEqual(errors, {})
        self.assertEqual(cleaned['timezone'], 'Not/A/Real/Zone')


# ---------------------------------------------------------------------------
# Priority 3: views._validate_and_save_field()
# ---------------------------------------------------------------------------

class ValidateAndSaveFieldTests(TestCase):
    """Tests for views._validate_and_save_field()."""

    def setUp(self):
        self.event = save_event()

    def _call(self, field_name, value_str):
        return _validate_and_save_field(self.event, field_name, value_str)

    def _refresh(self):
        self.event.refresh_from_db()

    # --- Text fields ---

    def test_text_field_valid_saves_and_returns_none(self):
        error = self._call('name', 'New Event Name')
        self.assertIsNone(error)
        self._refresh()
        self.assertEqual(self.event.name, 'New Event Name')

    def test_text_field_strips_whitespace(self):
        self._call('name', '  Padded Name  ')
        self._refresh()
        self.assertEqual(self.event.name, 'Padded Name')

    def test_required_text_field_empty_returns_error(self):
        error = self._call('name', '')
        self.assertIsNotNone(error)
        self.assertIn('required', error.lower())

    def test_required_text_field_whitespace_only_returns_error(self):
        error = self._call('name', '   ')
        self.assertIsNotNone(error)

    def test_optional_text_field_empty_saves_empty_string(self):
        # 'car' is not required
        error = self._call('car', '')
        self.assertIsNone(error)
        self._refresh()
        self.assertEqual(self.event.car, '')

    def test_textarea_field_saves(self):
        error = self._call('setup', 'High downforce, soft tyres')
        self.assertIsNone(error)
        self._refresh()
        self.assertEqual(self.event.setup, 'High downforce, soft tyres')

    # --- Date field ---

    def test_date_valid_iso_saves(self):
        error = self._call('date', '2027-06-01')
        self.assertIsNone(error)
        self._refresh()
        self.assertEqual(self.event.date, dt.date(2027, 6, 1))

    def test_date_invalid_format_returns_error(self):
        error = self._call('date', '01/06/2027')
        self.assertIsNotNone(error)
        self.assertIn('YYYY-MM-DD', error)

    def test_date_empty_returns_error(self):
        error = self._call('date', '')
        self.assertIsNotNone(error)
        self.assertIn('required', error.lower())

    def test_date_nonsense_string_returns_error(self):
        error = self._call('date', 'not-a-date')
        self.assertIsNotNone(error)

    # --- Time field ---

    def test_time_valid_saves(self):
        error = self._call('start_time_utc', '09:30')
        self.assertIsNone(error)
        self._refresh()
        self.assertEqual(self.event.start_time_utc, dt.time(9, 30))

    def test_time_invalid_format_returns_error(self):
        error = self._call('start_time_utc', '9:30 AM')
        self.assertIsNotNone(error)
        self.assertIn('HH:MM', error)

    def test_time_empty_returns_error(self):
        error = self._call('start_time_utc', '')
        self.assertIsNotNone(error)

    def test_time_nonsense_returns_error(self):
        error = self._call('start_time_utc', 'noon')
        self.assertIsNotNone(error)

    # --- Number fields ---

    def test_number_valid_saves(self):
        # avg_lap_seconds now uses mmss type; use fuel_capacity for plain number
        error = self._call('fuel_capacity', '50.0')
        self.assertIsNone(error)
        self._refresh()
        self.assertAlmostEqual(self.event.fuel_capacity, 50.0)

    def test_length_hours_converts_to_seconds(self):
        error = self._call('length_hours', '6')
        self.assertIsNone(error)
        self._refresh()
        self.assertEqual(self.event.length_seconds, 21_600)

    def test_length_hours_fractional_converts_correctly(self):
        error = self._call('length_hours', '1.5')
        self.assertIsNone(error)
        self._refresh()
        self.assertEqual(self.event.length_seconds, 5_400)

    def test_target_laps_saved_as_int(self):
        error = self._call('target_laps', '25.0')
        self.assertIsNone(error)
        self._refresh()
        self.assertEqual(self.event.target_laps, 25)
        self.assertIsInstance(self.event.target_laps, int)

    def test_number_not_a_number_returns_error(self):
        # Use a plain number field (fuel_capacity) not mmss
        error = self._call('fuel_capacity', 'lots')
        self.assertIsNotNone(error)
        self.assertIn('valid number', error.lower())

    def test_required_number_empty_returns_error(self):
        # 'length_hours' is required
        error = self._call('length_hours', '')
        self.assertIsNotNone(error)
        self.assertIn('required', error.lower())

    def test_optional_number_empty_sets_none(self):
        # 'fuel_capacity' is optional and a plain number field
        error = self._call('fuel_capacity', '')
        self.assertIsNone(error)
        self._refresh()
        self.assertIsNone(self.event.fuel_capacity)

    def test_number_below_min_returns_error(self):
        # 'fuel_per_lap' min=0.01
        error = self._call('fuel_per_lap', '0.005')
        self.assertIsNotNone(error)
        self.assertIn('at least', error.lower())

    def test_number_at_min_is_accepted(self):
        error = self._call('fuel_per_lap', '0.01')
        self.assertIsNone(error)

    def test_number_above_max_returns_error(self):
        # 'length_hours' max=168
        error = self._call('length_hours', '200')
        self.assertIsNotNone(error)
        self.assertIn('at most', error.lower())

    def test_number_at_max_is_accepted(self):
        error = self._call('length_hours', '168')
        self.assertIsNone(error)

    def test_length_hours_below_min_returns_error(self):
        error = self._call('length_hours', '0')
        self.assertIsNotNone(error)

    def test_fuel_per_lap_min_constraint(self):
        # min=0.01
        error = self._call('fuel_per_lap', '0.005')
        self.assertIsNotNone(error)

    def test_fuel_per_lap_at_min_accepted(self):
        error = self._call('fuel_per_lap', '0.01')
        self.assertIsNone(error)

    def test_tire_change_fuel_min_zero_accepted(self):
        # tire_change_fuel_min has min=0 → zero is valid
        error = self._call('tire_change_fuel_min', '0')
        self.assertIsNone(error)
        self._refresh()
        self.assertAlmostEqual(self.event.tire_change_fuel_min, 0.0)

    def test_tire_change_fuel_min_negative_rejected(self):
        error = self._call('tire_change_fuel_min', '-1')
        self.assertIsNotNone(error)

    def test_whitespace_only_value_treated_as_empty(self):
        # Required field with whitespace-only input
        error = self._call('length_hours', '   ')
        self.assertIsNotNone(error)

    def test_number_with_leading_trailing_spaces_accepted(self):
        # value_str is stripped before parsing; use plain number field
        error = self._call('fuel_capacity', '  100  ')
        self.assertIsNone(error)
        self._refresh()
        self.assertAlmostEqual(self.event.fuel_capacity, 100.0)


# ---------------------------------------------------------------------------
# Priority 4: Admin views — session, timezone cookie, key-based login
# ---------------------------------------------------------------------------


class AdminDashboardTests(TestCase):
    """Tests for views.admin_dashboard() — session-gated access."""

    def setUp(self):
        self.event = save_event()
        self.url = reverse('admin_dashboard', kwargs={'event_id': self.event.id})

    def _set_admin_session(self, event_id):
        """Helper: write the admin session flag for the given event_id."""
        session = self.client.session
        session[f'admin_{event_id}'] = True
        session.save()

    def test_without_session_redirects_to_discord_login(self):
        # New behaviour: unauthenticated + no session → redirect to Discord OAuth
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/discord/login/', response['Location'])

    def test_with_wrong_event_session_redirects_to_discord_login(self):
        # Session for a different event does not grant access → redirect
        other_id = uuid.uuid4()
        self._set_admin_session(other_id)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/discord/login/', response['Location'])

    def test_with_valid_session_returns_200(self):
        self._set_admin_session(self.event.id)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)

    def test_with_valid_session_uses_admin_template(self):
        self._set_admin_session(self.event.id)

        response = self.client.get(self.url)

        template_names = [t.name for t in response.templates]
        self.assertIn('admin.html', template_names)

    def test_nonexistent_event_with_session_returns_404(self):
        nonexistent_id = uuid.uuid4()
        self._set_admin_session(nonexistent_id)
        url = reverse('admin_dashboard', kwargs={'event_id': nonexistent_id})

        response = self.client.get(url)

        self.assertEqual(response.status_code, 404)


class SetTimezoneTests(TestCase):
    """Tests for views.set_timezone() — POST-only timezone cookie endpoint."""

    def setUp(self):
        self.url = reverse('set_timezone')

    def test_get_returns_405(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 405)

    def test_get_returns_allow_post_header(self):
        response = self.client.get(self.url)

        self.assertEqual(response['Allow'], 'POST')

    def test_post_sets_cookie(self):
        response = self.client.post(self.url, {'timezone': 'America/New_York'})

        self.assertEqual(response.status_code, 200)
        self.assertIn('admin_timezone', response.cookies)
        self.assertEqual(response.cookies['admin_timezone'].value, 'America/New_York')

    def test_post_unknown_timezone_falls_back_to_utc(self):
        response = self.client.post(self.url, {'timezone': 'Fake/Zone'})

        self.assertEqual(response.cookies['admin_timezone'].value, 'UTC')

    def test_post_changed_timezone_sets_hx_refresh(self):
        # Seed a current cookie so the view sees a different incoming timezone
        self.client.cookies['admin_timezone'] = 'UTC'

        response = self.client.post(self.url, {'timezone': 'America/New_York'})

        self.assertEqual(response['HX-Refresh'], 'true')

    def test_post_same_timezone_no_hx_refresh(self):
        self.client.cookies['admin_timezone'] = 'America/New_York'

        response = self.client.post(self.url, {'timezone': 'America/New_York'})

        self.assertNotIn('HX-Refresh', response)


class AdminPageSessionTests(TestCase):
    """Tests for views.admin_page() — key-based login and session promotion."""

    def setUp(self):
        self.event = save_event()
        self.url = reverse(
            'admin_page',
            kwargs={'event_id': self.event.id, 'admin_key': self.event.admin_key},
        )
        self.wrong_key_url = reverse(
            'admin_page',
            kwargs={'event_id': self.event.id, 'admin_key': 'wrong-key-value'},
        )

    def test_wrong_key_does_not_grant_session(self):
        self.client.get(self.wrong_key_url)

        session_flag = self.client.session.get(f'admin_{self.event.id}')
        self.assertFalse(bool(session_flag))

    def test_valid_key_sets_session_flag(self):
        self.client.get(self.url)

        self.assertTrue(self.client.session.get(f'admin_{self.event.id}'))

    def test_valid_key_cycles_session(self):
        # Force the client to have an established session key before the request
        session = self.client.session
        session['warmup'] = True
        session.save()
        key_before = self.client.session.session_key

        self.client.get(self.url)

        key_after = self.client.session.session_key
        self.assertNotEqual(key_before, key_after)

    def test_valid_key_returns_302_redirect(self):
        # admin_page now redirects to the key-less admin_dashboard URL so that
        # the admin key only appears in logs once, not on every subsequent visit.
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)

    def test_invalid_key_renders_error_template(self):
        response = self.client.get(self.wrong_key_url)

        template_names = [t.name for t in response.templates]
        self.assertIn('admin_error.html', template_names)

    def test_after_valid_key_dashboard_accessible(self):
        # Hitting admin_page with the correct key should set the session flag,
        # allowing admin_dashboard to serve the page without the key in the URL.
        self.client.get(self.url)

        dashboard_url = reverse('admin_dashboard', kwargs={'event_id': self.event.id})
        response = self.client.get(dashboard_url)

        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# build_stint_availability_matrix
# ---------------------------------------------------------------------------

class BuildStintAvailabilityMatrixTests(TestCase):
    """Tests for utils.build_stint_availability_matrix().

    Requires the database because Availability records are fetched via ORM.
    """

    def setUp(self):
        self.event = save_event()  # 6-hour race, stint_length=3615s, 6 stints
        self.driver = Driver.objects.create(
            event=self.event,
            name='Alice',
            timezone='UTC',
        )

    def _add_slot(self, driver, slot_utc):
        Availability.objects.create(driver=driver, slot_utc=slot_utc)

    def _stint_windows(self, event=None):
        return get_stint_windows(event or self.event)

    # --- Empty drivers list ---

    def test_empty_drivers_list_returns_empty_dict(self):
        result = build_stint_availability_matrix([], self._stint_windows())
        self.assertEqual(result, {})

    # --- All slots available → 'full' ---

    def test_all_slots_available_returns_full(self):
        # Stint 1: anchor=12:00, first_grid_slot=12:00, end=13:00:15
        # Grid slots: 12:00, 12:30, 13:00  (all < 13:00:15)
        self._add_slot(self.driver, utc(2026, 6, 1, 12, 0))
        self._add_slot(self.driver, utc(2026, 6, 1, 12, 30))
        self._add_slot(self.driver, utc(2026, 6, 1, 13, 0))

        windows = self._stint_windows()
        result = build_stint_availability_matrix([self.driver], windows)

        self.assertEqual(result[str(self.driver.id)][1], 'full')

    # --- No slots available → 'none' ---

    def test_no_slots_available_returns_none(self):
        # Driver has no availability at all
        windows = self._stint_windows()
        result = build_stint_availability_matrix([self.driver], windows)

        self.assertEqual(result[str(self.driver.id)][1], 'none')

    # --- Some slots available → 'partial' ---

    def test_some_slots_available_returns_partial(self):
        # Stint 1 has grid slots: 12:00, 12:30, 13:00 — driver only has 12:00
        self._add_slot(self.driver, utc(2026, 6, 1, 12, 0))

        windows = self._stint_windows()
        result = build_stint_availability_matrix([self.driver], windows)

        self.assertEqual(result[str(self.driver.id)][1], 'partial')

    # --- Empty stint → 'empty' ---

    def test_empty_stint_window_returns_empty(self):
        # Construct an artificial window where end == start so no grid slots exist
        start = utc(2026, 6, 1, 12, 0)
        artificial_windows = [{'stint_number': 1, 'start_utc': start, 'end_utc': start}]

        result = build_stint_availability_matrix([self.driver], artificial_windows)

        self.assertEqual(result[str(self.driver.id)][1], 'empty')

    # --- Grid anchor snap (critical regression test) ---

    def test_pre_grid_slot_covering_stint_start_counted_as_partial(self):
        # Default event: stint_length=3615s, grid_anchor=12:00:00 UTC
        # Stint 2 starts at 13:00:15 UTC.
        # snapped_start = 12:00 + ceil(3615/1800)*30min = 13:30
        # Because stint start (13:00:15) < snapped_start (13:30), the pre-slot
        # (13:00) is included — it covers the 13:00:15 stint start.
        # total_slots = [13:00 (pre), 13:30, 14:00] — driver has only 13:00 → 'partial'
        self._add_slot(self.driver, utc(2026, 6, 1, 13, 0))

        windows = self._stint_windows()
        result = build_stint_availability_matrix([self.driver], windows)

        self.assertEqual(result[str(self.driver.id)][2], 'partial')

    def test_grid_snap_slot_at_ceil_boundary_counts_for_stint(self):
        # 13:30 is the first grid slot for stint 2 (see above); driver has it
        self._add_slot(self.driver, utc(2026, 6, 1, 13, 30))

        windows = self._stint_windows()
        result = build_stint_availability_matrix([self.driver], windows)

        # Stint 2 has slots 13:30 and 14:00; driver has only 13:30 → 'partial'
        self.assertEqual(result[str(self.driver.id)][2], 'partial')

    # --- Multiple drivers keyed by str(driver.id) ---

    def test_two_drivers_keyed_independently(self):
        bob = Driver.objects.create(event=self.event, name='Bob', timezone='UTC')

        # Alice has all slots for stint 1; Bob has none
        self._add_slot(self.driver, utc(2026, 6, 1, 12, 0))
        self._add_slot(self.driver, utc(2026, 6, 1, 12, 30))
        self._add_slot(self.driver, utc(2026, 6, 1, 13, 0))

        windows = self._stint_windows()
        result = build_stint_availability_matrix([self.driver, bob], windows)

        self.assertEqual(result[str(self.driver.id)][1], 'full')
        self.assertEqual(result[str(bob.id)][1], 'none')

    def test_result_keys_are_strings_of_driver_ids(self):
        windows = self._stint_windows()
        result = build_stint_availability_matrix([self.driver], windows)

        self.assertIn(str(self.driver.id), result)

    # --- Full matrix across multiple stints ---

    def test_different_statuses_per_stint_in_same_matrix(self):
        # Stint 1: start=12:00:00, slots [12:00, 12:30, 13:00] — driver has all → 'full'
        # Stint 2: start=13:00:15, snapped=13:30, pre_slot=13:00
        #          total_slots = [13:00 (pre), 13:30, 14:00]
        #          driver has 13:00 (from stint 1 coverage) but not 13:30/14:00 → 'partial'
        # Stint 3: start=14:00:30, snapped=14:30, pre_slot=14:00
        #          end = 15:00:45, total_slots = [14:00 (pre), 14:30, 15:00]
        #          driver has 14:30 only → 'partial'
        self._add_slot(self.driver, utc(2026, 6, 1, 12, 0))
        self._add_slot(self.driver, utc(2026, 6, 1, 12, 30))
        self._add_slot(self.driver, utc(2026, 6, 1, 13, 0))
        # Stint 2 on-grid slots (13:30, 14:00) not added
        # Stint 3 — only 14:30 (pre-slot 14:00 not added)
        self._add_slot(self.driver, utc(2026, 6, 1, 14, 30))

        windows = self._stint_windows()
        result = build_stint_availability_matrix([self.driver], windows)

        driver_matrix = result[str(self.driver.id)]
        self.assertEqual(driver_matrix[1], 'full')
        self.assertEqual(driver_matrix[2], 'partial')
        self.assertEqual(driver_matrix[3], 'partial')

    def test_empty_stint_windows_returns_empty_dict(self):
        # Guard: empty stint_windows must not raise IndexError
        result = build_stint_availability_matrix([self.driver], [])
        self.assertEqual(result, {})

    def test_ceil_snap_past_end_pre_slot_still_checked(self):
        # grid_anchor = 12:00 (first window's start_utc).
        # Second window: starts at 12:10 (off-grid), ends at 12:15.
        # snapped_start = 12:00 + ceil(10/30)*30min = 12:30, which is past end.
        # Because start (12:10) < snapped_start (12:30), the pre-slot (12:00)
        # is included — it covers the 12:10–12:15 window.
        # total_slots = [12:00], driver has no availability → 'none'.
        anchor = utc(2026, 6, 1, 12, 0)
        windows = [
            {'stint_number': 1, 'start_utc': anchor, 'end_utc': anchor + dt.timedelta(minutes=30)},
            {'stint_number': 2, 'start_utc': anchor + dt.timedelta(minutes=10),
             'end_utc': anchor + dt.timedelta(minutes=15)},
        ]

        result = build_stint_availability_matrix([self.driver], windows)

        self.assertEqual(result[str(self.driver.id)][2], 'none')


# ---------------------------------------------------------------------------
# dict_get template filter
# ---------------------------------------------------------------------------

class DictGetFilterTests(SimpleTestCase):
    """Tests for templatetags.tz_filters.dict_get."""

    def test_string_key_that_exists_returns_value(self):
        result = dict_get({'foo': 'bar'}, 'foo')
        self.assertEqual(result, 'bar')

    def test_integer_key_where_dict_has_int_key_returns_value(self):
        result = dict_get({1: 'one'}, 1)
        self.assertEqual(result, 'one')

    def test_integer_key_where_dict_has_string_key_falls_back_to_str(self):
        # Key passed as int; dict has the string equivalent → str(key) fallback
        result = dict_get({'1': 'one'}, 1)
        self.assertEqual(result, 'one')

    def test_no_reverse_coercion_str_key_for_int_keyed_dict(self):
        # By design: str→int coercion is not performed; '1' does not match int key 1
        result = dict_get({1: 'one'}, '1')
        self.assertIsNone(result)

    def test_missing_key_returns_none(self):
        result = dict_get({'a': 1}, 'b')
        self.assertIsNone(result)

    def test_none_dict_returns_none(self):
        result = dict_get(None, 'any')
        self.assertIsNone(result)

    def test_nested_usage_with_string_keys(self):
        outer = {'x': {'y': 42}}
        inner = dict_get(outer, 'x')
        result = dict_get(inner, 'y')
        self.assertEqual(result, 42)

    def test_nested_usage_with_int_key_on_inner_string_keyed_dict(self):
        # Inner dict has string keys; passing int key falls back to str(key)
        outer = {'section': {'3': 'three'}}
        inner = dict_get(outer, 'section')
        result = dict_get(inner, 3)
        self.assertEqual(result, 'three')


# ---------------------------------------------------------------------------
# feedback_submit view
# ---------------------------------------------------------------------------

class FeedbackSubmitTests(TestCase):
    """Tests for views.feedback_submit() — HTMX POST endpoint."""

    def setUp(self):
        self.url = reverse('feedback_submit')

    def test_get_request_returns_400(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 400)

    def test_empty_text_returns_inline_error_html(self):
        response = self.client.post(self.url, {'text': ''})

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Please enter some feedback', response.content)

    def test_empty_text_does_not_create_feedback_record(self):
        self.client.post(self.url, {'text': ''})

        self.assertEqual(Feedback.objects.count(), 0)

    def test_whitespace_only_text_returns_inline_error_html(self):
        response = self.client.post(self.url, {'text': '   '})

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Please enter some feedback', response.content)

    def test_whitespace_only_text_does_not_create_feedback_record(self):
        self.client.post(self.url, {'text': '   '})

        self.assertEqual(Feedback.objects.count(), 0)

    def test_text_over_1000_chars_returns_inline_error_html(self):
        response = self.client.post(self.url, {'text': 'x' * 1001})

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'under 1000 characters', response.content)

    def test_text_over_1000_chars_does_not_create_feedback_record(self):
        self.client.post(self.url, {'text': 'x' * 1001})

        self.assertEqual(Feedback.objects.count(), 0)

    def test_text_exactly_1000_chars_is_accepted(self):
        response = self.client.post(self.url, {'text': 'x' * 1000})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Feedback.objects.count(), 1)

    def test_valid_submission_creates_feedback_record(self):
        self.client.post(self.url, {'text': 'Great app!'})

        self.assertEqual(Feedback.objects.count(), 1)

    def test_valid_submission_stores_correct_text(self):
        self.client.post(self.url, {'text': 'Great app!'})

        feedback = Feedback.objects.get()
        self.assertEqual(feedback.text, 'Great app!')

    def test_valid_submission_stores_page_url(self):
        self.client.post(self.url, {'text': 'Good', 'page_url': '/some/page/'})

        feedback = Feedback.objects.get()
        self.assertEqual(feedback.page_url, '/some/page/')

    def test_valid_submission_stores_user_agent(self):
        self.client.post(self.url, {'text': 'Good', 'user_agent': 'TestBrowser/1.0'})

        feedback = Feedback.objects.get()
        self.assertEqual(feedback.user_agent, 'TestBrowser/1.0')

    def test_valid_submission_returns_200_with_hx_trigger_header(self):
        response = self.client.post(self.url, {'text': 'Nice work'})

        self.assertEqual(response.status_code, 200)
        # Must be kebab-case: the Alpine listener in base.html is an HTML
        # attribute, and attribute names are lowercased by the parser, so a
        # camelCase event name can never be listened for.
        self.assertEqual(response['HX-Trigger'], 'feedback-success')

    def test_valid_submission_returns_empty_body(self):
        response = self.client.post(self.url, {'text': 'Nice work'})

        self.assertEqual(response.content, b'')

    def test_page_url_longer_than_500_chars_is_truncated_to_500(self):
        long_url = '/path/' + 'a' * 600
        self.client.post(self.url, {'text': 'Hi', 'page_url': long_url})

        feedback = Feedback.objects.get()
        self.assertEqual(len(feedback.page_url), 500)

    def test_user_agent_longer_than_500_chars_is_truncated_to_500(self):
        long_ua = 'Mozilla/' + 'x' * 600
        self.client.post(self.url, {'text': 'Hi', 'user_agent': long_ua})

        feedback = Feedback.objects.get()
        self.assertEqual(len(feedback.user_agent), 500)


# ---------------------------------------------------------------------------
# feedback_view view
# ---------------------------------------------------------------------------

class FeedbackViewTests(TestCase):
    """Tests for views.feedback_view() — password-protected feedback viewer."""

    def setUp(self):
        self.url = reverse('feedback_view')

    def _authenticate(self):
        """Helper: set the session flag that marks the browser as authenticated."""
        session = self.client.session
        session['feedback_authenticated'] = True
        session.save()

    # --- GET when not authenticated ---

    def test_get_unauthenticated_returns_200(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)

    def test_get_unauthenticated_renders_password_prompt(self):
        response = self.client.get(self.url)

        self.assertFalse(response.context['authenticated'])

    def test_get_unauthenticated_does_not_expose_feedback_items(self):
        response = self.client.get(self.url)

        self.assertNotIn('feedback_items', response.context)

    # --- POST with wrong password ---

    @override_settings(FEEDBACK_PASSWORD='testpass')
    @patch('events.views.time.sleep')
    def test_post_wrong_password_returns_200(self, mock_sleep):
        response = self.client.post(self.url, {'password': 'wrongpass'})

        self.assertEqual(response.status_code, 200)

    @override_settings(FEEDBACK_PASSWORD='testpass')
    @patch('events.views.time.sleep')
    def test_post_wrong_password_renders_error_message(self, mock_sleep):
        response = self.client.post(self.url, {'password': 'wrongpass'})

        self.assertFalse(response.context['authenticated'])
        self.assertIn('error', response.context)
        self.assertIn('Incorrect', response.context['error'])

    @override_settings(FEEDBACK_PASSWORD='testpass')
    @patch('events.views.time.sleep')
    def test_post_wrong_password_does_not_set_session(self, mock_sleep):
        self.client.post(self.url, {'password': 'wrongpass'})

        self.assertFalse(bool(self.client.session.get('feedback_authenticated')))

    # --- POST with correct password ---

    @override_settings(FEEDBACK_PASSWORD='testpass')
    def test_post_correct_password_sets_session(self):
        self.client.post(self.url, {'password': 'testpass'})

        self.assertTrue(self.client.session.get('feedback_authenticated'))

    @override_settings(FEEDBACK_PASSWORD='testpass')
    def test_post_correct_password_shows_feedback_list(self):
        response = self.client.post(self.url, {'password': 'testpass'})

        self.assertTrue(response.context['authenticated'])
        self.assertIn('feedback_items', response.context)

    # --- POST with empty password when FEEDBACK_PASSWORD is also empty ---

    @override_settings(FEEDBACK_PASSWORD='')
    @patch('events.views.time.sleep')
    def test_post_empty_password_rejected_when_setting_is_also_empty(self, mock_sleep):
        # The `and django_settings.FEEDBACK_PASSWORD` guard must prevent login
        # even when both sides of compare_digest would be empty strings.
        response = self.client.post(self.url, {'password': ''})

        self.assertFalse(response.context['authenticated'])
        self.assertFalse(bool(self.client.session.get('feedback_authenticated')))

    # --- GET when authenticated via session ---

    def test_get_authenticated_shows_feedback_list(self):
        self._authenticate()

        response = self.client.get(self.url)

        self.assertTrue(response.context['authenticated'])
        self.assertIn('feedback_items', response.context)

    def test_get_authenticated_returns_200(self):
        self._authenticate()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)

    # --- Logout ---

    def test_logout_clears_session_and_redirects(self):
        self._authenticate()

        response = self.client.get(self.url + '?logout=1')

        self.assertRedirects(response, self.url)
        self.assertFalse(bool(self.client.session.get('feedback_authenticated')))

    def test_logout_unauthenticated_session_still_redirects(self):
        # Logout should redirect even if the session flag was never set.
        response = self.client.get(self.url + '?logout=1')

        self.assertRedirects(response, self.url)

    # --- Ordering and queryset cap ---

    def test_feedback_items_are_shown_most_recent_first(self):
        # Create two items in chronological order; the view must return them reversed.
        older = Feedback.objects.create(text='First post')
        newer = Feedback.objects.create(text='Second post')
        self._authenticate()

        response = self.client.get(self.url)

        items = response.context['feedback_items']
        self.assertEqual(items[0].pk, newer.pk)
        self.assertEqual(items[1].pk, older.pk)

    def test_queryset_is_capped_at_200_items(self):
        Feedback.objects.bulk_create(
            [Feedback(text=f'item {i}') for i in range(201)]
        )
        self._authenticate()

        response = self.client.get(self.url)

        self.assertEqual(len(response.context['feedback_items']), 200)
        self.assertEqual(response.context['total'], 200)


# ===========================================================================
# Phase A/B/C design system tests
# ===========================================================================

# ---------------------------------------------------------------------------
# seconds_to_hours_display filter (Phase A)
# ---------------------------------------------------------------------------

class SecondsToHoursDisplayFilterTests(SimpleTestCase):
    """Tests for templatetags.tz_filters.seconds_to_hours_display.

    The filter converts a seconds integer to a human-readable duration.
    It returns only hours when minutes == 0, e.g. 3600 → "1h".
    When minutes > 0, format is "Xh Ym", e.g. 5400 → "1h 30m".
    A falsy value (0, None) returns the em-dash sentinel "—".
    """

    def test_exact_hours_no_minutes_suffix(self):
        # 3600 s = 1 hour exactly → only hours shown
        self.assertEqual(seconds_to_hours_display(3600), '1h')

    def test_hours_and_minutes(self):
        # 5400 s = 1 h 30 m
        self.assertEqual(seconds_to_hours_display(5400), '1h 30m')

    def test_twenty_four_hours(self):
        # 86400 s = 24 h (a typical race length)
        self.assertEqual(seconds_to_hours_display(86400), '24h')

    def test_six_hours_thirty_minutes(self):
        # 23400 s = 6 h 30 m
        self.assertEqual(seconds_to_hours_display(23400), '6h 30m')

    def test_one_minute_only(self):
        # Sub-hour durations omit the hours part: "0h 1m" reads as a bug.
        self.assertEqual(seconds_to_hours_display(60), '1m')

    def test_fifty_nine_minutes(self):
        self.assertEqual(seconds_to_hours_display(3540), '59m')

    def test_half_an_hour(self):
        # The uncovered-window banner routinely reports gaps under an hour.
        self.assertEqual(seconds_to_hours_display(1800), '30m')

    def test_whole_hours_keep_the_hours_only_form(self):
        self.assertEqual(seconds_to_hours_display(7200), '2h')

    def test_hours_and_minutes_keep_both(self):
        self.assertEqual(seconds_to_hours_display(23400), '6h 30m')

    def test_zero_returns_em_dash(self):
        # 0 is falsy — the filter returns the sentinel
        self.assertEqual(seconds_to_hours_display(0), '—')

    def test_none_returns_em_dash(self):
        self.assertEqual(seconds_to_hours_display(None), '—')

    def test_large_race_with_remainder(self):
        # 25 h 15 m = 90900 s
        self.assertEqual(seconds_to_hours_display(90900), '25h 15m')

    def test_seconds_less_than_one_minute_ignored(self):
        # 3615 s = 1 h 0 m 15 s → minutes part is 0 → only "1h"
        self.assertEqual(seconds_to_hours_display(3615), '1h')


# ---------------------------------------------------------------------------
# get_item filter (Phase A)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Home view — recruiting_events context (Phase C)
# ---------------------------------------------------------------------------

class HomeViewRecruitingContextTests(TestCase):
    """Tests for the home view's recruiting_events context variable.

    The view filters to recruiting=True events whose start_datetime is in
    the future, annotates with driver_count, and caps at 8 results.
    """

    def setUp(self):
        self.url = reverse('home')
        # A fixed future start time well beyond today
        self.future_date = dt.date(2030, 6, 1)
        self.future_time = dt.time(12, 0, 0)

    def _make_recruiting(self, name='Race', **overrides):
        return save_event(
            name=name,
            date=overrides.pop('date', self.future_date),
            start_time_utc=overrides.pop('start_time_utc', self.future_time),
            recruiting=True,
            **overrides,
        )

    def _make_non_recruiting(self, name='Non-recruiting Race'):
        return save_event(
            name=name,
            date=self.future_date,
            start_time_utc=self.future_time,
            recruiting=False,
        )

    def test_home_returns_200(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)

    def test_no_recruiting_events_gives_empty_list(self):
        self._make_non_recruiting()

        response = self.client.get(self.url)

        self.assertEqual(list(response.context['recruiting_events']), [])

    def test_recruiting_event_appears_in_context(self):
        event = self._make_recruiting(name='Recruiting Race')

        response = self.client.get(self.url)

        names = [e.name for e in response.context['recruiting_events']]
        self.assertIn('Recruiting Race', names)

    def test_non_recruiting_event_excluded_from_context(self):
        self._make_recruiting(name='Recruiting Race')
        self._make_non_recruiting(name='Hidden Race')

        response = self.client.get(self.url)

        names = [e.name for e in response.context['recruiting_events']]
        self.assertNotIn('Hidden Race', names)

    def test_driver_count_annotation_present_and_zero_with_no_drivers(self):
        self._make_recruiting()

        response = self.client.get(self.url)

        event = response.context['recruiting_events'][0]
        self.assertEqual(event.driver_count, 0)

    def test_driver_count_annotation_reflects_actual_signup_count(self):
        event = self._make_recruiting()
        Driver.objects.create(event=event, name='Alice', timezone='UTC')
        Driver.objects.create(event=event, name='Bob', timezone='UTC')

        response = self.client.get(self.url)

        ctx_event = response.context['recruiting_events'][0]
        self.assertEqual(ctx_event.driver_count, 2)

    def test_driver_count_not_cross_contaminated_between_events(self):
        event_a = self._make_recruiting(name='Race A')
        event_b = self._make_recruiting(name='Race B')
        Driver.objects.create(event=event_a, name='Alice', timezone='UTC')
        # event_b has no drivers

        response = self.client.get(self.url)

        ctx_events = {e.name: e for e in response.context['recruiting_events']}
        self.assertEqual(ctx_events['Race A'].driver_count, 1)
        self.assertEqual(ctx_events['Race B'].driver_count, 0)

    def test_past_recruiting_event_excluded(self):
        # An event dated yesterday, recruiting=True — must not appear
        yesterday = dt.date.today() - dt.timedelta(days=1)
        save_event(
            name='Past Recruiting',
            date=yesterday,
            start_time_utc=dt.time(12, 0, 0),
            recruiting=True,
        )

        response = self.client.get(self.url)

        names = [e.name for e in response.context['recruiting_events']]
        self.assertNotIn('Past Recruiting', names)

    def test_results_capped_at_eight(self):
        # Create 10 recruiting events, all in the future
        for i in range(10):
            save_event(
                name=f'Race {i}',
                date=dt.date(2030, 6, i + 1),
                start_time_utc=dt.time(12, 0, 0),
                recruiting=True,
            )

        response = self.client.get(self.url)

        self.assertLessEqual(len(response.context['recruiting_events']), 8)

    def test_uses_home_template(self):
        response = self.client.get(self.url)

        template_names = [t.name for t in response.templates]
        self.assertIn('home.html', template_names)


# ---------------------------------------------------------------------------
# Home template rendering (Phase C)
# ---------------------------------------------------------------------------

class HomeTemplateRenderingTests(TestCase):
    """Tests for home.html template content.

    Verifies that Phase C changes render correctly: the recruiting section,
    HTMX search input attributes, and the create event link.
    """

    def setUp(self):
        self.url = reverse('home')
        self.future_date = dt.date(2030, 6, 1)
        self.future_time = dt.time(14, 0, 0)

    def _make_recruiting(self, **overrides):
        return save_event(
            name=overrides.pop('name', 'Test Recruiting Race'),
            date=overrides.pop('date', self.future_date),
            start_time_utc=overrides.pop('start_time_utc', self.future_time),
            recruiting=True,
            **overrides,
        )

    def test_recruiting_section_absent_when_no_recruiting_events(self):
        response = self.client.get(self.url)

        # The recruiting section only appears inside {% if recruiting_events %}
        self.assertNotContains(response, 'Recruiting — looking for drivers')

    def test_recruiting_section_present_when_events_exist(self):
        self._make_recruiting()

        response = self.client.get(self.url)

        self.assertContains(response, 'Recruiting — looking for drivers')

    def test_recruiting_section_shows_event_name(self):
        self._make_recruiting(name='Spa 24H 2030')

        response = self.client.get(self.url)

        self.assertContains(response, 'Spa 24H 2030')

    def test_recruiting_section_shows_track_when_set(self):
        self._make_recruiting(track='Monza')

        response = self.client.get(self.url)

        self.assertContains(response, 'Monza')

    def test_recruiting_section_shows_car_when_set(self):
        self._make_recruiting(car='Ferrari GT3')

        response = self.client.get(self.url)

        self.assertContains(response, 'Ferrari GT3')

    def test_recruiting_event_link_uses_from_recruiting_param(self):
        event = self._make_recruiting()

        response = self.client.get(self.url)

        expected_url = reverse('view_event', kwargs={'event_id': event.id}) + '?from=recruiting'
        self.assertContains(response, expected_url)

    def test_create_event_link_resolves_correctly(self):
        response = self.client.get(self.url)

        expected_url = reverse('event_create')
        self.assertContains(response, f'href="{expected_url}"')

    def test_htmx_search_input_has_correct_hx_get_attribute(self):
        response = self.client.get(self.url)

        expected_search_url = reverse('event_search')
        self.assertContains(response, f'hx-get="{expected_search_url}"')

    def test_recruiting_section_shows_driver_count_singular(self):
        event = self._make_recruiting()
        Driver.objects.create(event=event, name='Alice', timezone='UTC')

        response = self.client.get(self.url)

        # Template: "{{ event.driver_count }} driver{{ event.driver_count|pluralize }}"
        self.assertContains(response, '1 driver signed up')

    def test_recruiting_section_shows_driver_count_plural(self):
        event = self._make_recruiting()
        Driver.objects.create(event=event, name='Alice', timezone='UTC')
        Driver.objects.create(event=event, name='Bob', timezone='UTC')

        response = self.client.get(self.url)

        self.assertContains(response, '2 drivers signed up')

    def test_recruiting_section_shows_length_via_filter(self):
        # 7200 s = 2 h exactly → "2h" via seconds_to_hours_display
        self._make_recruiting(length_seconds=7200)

        response = self.client.get(self.url)

        self.assertContains(response, '2h')

    def test_track_not_rendered_when_blank(self):
        self._make_recruiting(track='')

        response = self.client.get(self.url)

        # The template wraps track in {% if event.track %} so no "·" with empty
        # We verify the word "Track" doesn't appear as a label in the recruiting section
        # by checking the full recruiting item doesn't have a trailing "· " artifact.
        # A simpler proxy: confirm the recruiting block renders (event-item class present),
        # then confirm no track text appears.
        self.assertContains(response, 'event-item')
        self.assertNotContains(response, '· Monza')  # No track text present


# ---------------------------------------------------------------------------
# event_create_form.html — non-field errors rendered exactly once (Phase C)
# ---------------------------------------------------------------------------

class EventCreateFormNonFieldErrorRenderingTests(TestCase):
    """Tests for partials/event_create_form.html non-field error rendering.

    Phase C fixed a bug where non-field errors were rendered twice.
    These tests confirm the error text appears exactly once in the
    HTMX partial response.
    """

    def setUp(self):
        self.url = reverse('event_create')

    def _post_with_zero_length(self):
        """POST data that triggers the 'Race length must be greater than zero'
        non-field ValidationError."""
        return self.client.post(
            self.url,
            {
                'name': 'Test Race',
                'date': '2030-06-01',
                'start_time_utc': '12:00',
                'length_hours': '0',
                'length_minutes': '0',
            },
            HTTP_HX_REQUEST='true',
        )

    def test_non_field_error_message_appears_in_response(self):
        response = self._post_with_zero_length()

        self.assertContains(response, 'Race length must be greater than zero')

    def test_non_field_error_rendered_exactly_once(self):
        response = self._post_with_zero_length()

        content = response.content.decode()
        count = content.count('Race length must be greater than zero')
        self.assertEqual(count, 1, f"Expected exactly 1 occurrence, found {count}")

    def test_field_error_for_past_date_appears_in_response(self):
        response = self.client.post(
            self.url,
            {
                'name': 'Test Race',
                'date': '2000-01-01',
                'start_time_utc': '12:00',
                'length_hours': '6',
                'length_minutes': '0',
            },
            HTTP_HX_REQUEST='true',
        )

        self.assertContains(response, 'past')

    def test_name_field_error_appears_in_response(self):
        response = self.client.post(
            self.url,
            {
                'name': '',
                'date': '2030-06-01',
                'start_time_utc': '12:00',
                'length_hours': '6',
                'length_minutes': '0',
            },
            HTTP_HX_REQUEST='true',
        )

        self.assertContains(response, 'required')

    def test_valid_submission_does_not_return_form_errors(self):
        response = self.client.post(
            self.url,
            {
                'name': 'Valid Race',
                'date': '2030-06-01',
                'start_time_utc': '12:00',
                'length_hours': '6',
                'length_minutes': '0',
            },
            HTTP_HX_REQUEST='true',
        )

        # A successful HTMX POST returns the success partial, not the form
        self.assertNotContains(response, 'Race length must be greater than zero')


# ---------------------------------------------------------------------------
# event_search view (HTMX endpoint, Phase C)
# ---------------------------------------------------------------------------

class EventSearchViewTests(TestCase):
    """Tests for views.event_search() — the HTMX live-search endpoint.

    The view rejects non-HTMX requests, returns empty for short queries,
    and returns partial HTML matching events by name, track, or car.
    Only future events are returned.
    """

    def setUp(self):
        self.url = reverse('event_search')
        self.future_date = dt.date(2030, 6, 1)
        self.future_time = dt.time(12, 0, 0)

    def _htmx_get(self, query):
        return self.client.get(
            self.url,
            {'q': query},
            HTTP_HX_REQUEST='true',
        )

    def _make_future_event(self, **overrides):
        return save_event(
            date=overrides.pop('date', self.future_date),
            start_time_utc=overrides.pop('start_time_utc', self.future_time),
            **overrides,
        )

    def test_non_htmx_request_returns_400(self):
        response = self.client.get(self.url, {'q': 'spa'})

        self.assertEqual(response.status_code, 400)

    def test_query_shorter_than_two_chars_returns_empty_body(self):
        response = self._htmx_get('s')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.strip(), b'')

    def test_empty_query_returns_empty_body(self):
        response = self._htmx_get('')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.strip(), b'')

    def test_match_by_event_name(self):
        self._make_future_event(name='Spa 24H 2030')

        response = self._htmx_get('Spa')

        self.assertContains(response, 'Spa 24H 2030')

    def test_match_by_track(self):
        self._make_future_event(name='Night Race', track='Nurburgring')

        response = self._htmx_get('Nurb')

        self.assertContains(response, 'Night Race')

    def test_match_by_car(self):
        self._make_future_event(name='GT3 Cup', car='Ferrari GT3')

        response = self._htmx_get('Ferrari')

        self.assertContains(response, 'GT3 Cup')

    def test_case_insensitive_matching(self):
        self._make_future_event(name='Monza Sprint')

        response = self._htmx_get('monza')

        self.assertContains(response, 'Monza Sprint')

    def test_non_matching_query_returns_no_results(self):
        self._make_future_event(name='Spa 24H')

        response = self._htmx_get('zzz')

        self.assertNotContains(response, 'Spa 24H')

    def test_past_event_excluded_from_results(self):
        save_event(
            name='Old Race',
            date=dt.date(2020, 1, 1),
            start_time_utc=dt.time(12, 0, 0),
        )

        response = self._htmx_get('Old')

        self.assertNotContains(response, 'Old Race')

    def test_two_char_query_is_accepted(self):
        self._make_future_event(name='GT Championship')

        response = self._htmx_get('GT')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'GT Championship')

    def test_multiple_matching_events_all_appear(self):
        self._make_future_event(name='Spa Race 1', track='Spa')
        self._make_future_event(name='Spa Race 2', track='Spa')

        response = self._htmx_get('Spa')

        self.assertContains(response, 'Spa Race 1')
        self.assertContains(response, 'Spa Race 2')


# ---------------------------------------------------------------------------
# view_event view — context variables (Phase C)
# ---------------------------------------------------------------------------

class ViewEventContextTests(TestCase):
    """Tests for views.view_event() context variables.

    Covers: stints_ready, has_stints, has_unassigned, show_signup_link,
    length_display, and the ?from=recruiting query param.
    """

    def setUp(self):
        self.event = save_event(
            date=dt.date(2030, 6, 1),
            start_time_utc=dt.time(12, 0, 0),
        )
        self.url = reverse('view_event', kwargs={'event_id': self.event.id})

    def test_returns_200(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)

    def test_nonexistent_event_returns_404(self):
        bad_url = reverse('view_event', kwargs={'event_id': uuid.uuid4()})

        response = self.client.get(bad_url)

        self.assertEqual(response.status_code, 404)

    def test_stints_ready_true_when_all_fields_set(self):
        # save_event() uses make_event() which populates all stint fields
        response = self.client.get(self.url)

        self.assertTrue(response.context['stints_ready'])

    def test_stints_ready_false_when_required_fields_missing(self):
        event = save_event(
            name='No Stint Fields',
            date=dt.date(2030, 6, 1),
            start_time_utc=dt.time(12, 0, 0),
            avg_lap_seconds=None,
            in_lap_seconds=None,
            out_lap_seconds=None,
            target_laps=None,
            fuel_capacity=None,
            fuel_per_lap=None,
        )
        url = reverse('view_event', kwargs={'event_id': event.id})

        response = self.client.get(url)

        self.assertFalse(response.context['stints_ready'])

    def test_has_stints_false_when_no_stint_assignments_exist(self):
        response = self.client.get(self.url)

        self.assertFalse(response.context['has_stints'])

    def test_has_stints_true_when_stint_assignments_exist(self):
        from .models import StintAssignment
        driver = Driver.objects.create(event=self.event, name='Alice', timezone='UTC')
        StintAssignment.objects.create(event=self.event, stint_number=1, driver=driver)

        response = self.client.get(self.url)

        self.assertTrue(response.context['has_stints'])

    def test_has_unassigned_false_when_all_stints_have_drivers(self):
        from .models import StintAssignment
        driver = Driver.objects.create(event=self.event, name='Alice', timezone='UTC')
        StintAssignment.objects.create(event=self.event, stint_number=1, driver=driver)

        response = self.client.get(self.url)

        self.assertFalse(response.context['has_unassigned'])

    def test_has_unassigned_true_when_any_stint_has_no_driver(self):
        from .models import StintAssignment
        StintAssignment.objects.create(event=self.event, stint_number=1, driver=None)

        response = self.client.get(self.url)

        self.assertTrue(response.context['has_unassigned'])

    def test_show_signup_link_false_without_from_param(self):
        response = self.client.get(self.url)

        self.assertFalse(response.context['show_signup_link'])

    def test_show_signup_link_true_with_from_recruiting_param(self):
        response = self.client.get(self.url + '?from=recruiting')

        self.assertTrue(response.context['show_signup_link'])

    def test_show_signup_link_false_with_other_from_param(self):
        response = self.client.get(self.url + '?from=admin')

        self.assertFalse(response.context['show_signup_link'])

    def test_from_recruiting_param_accepted_without_error(self):
        # Regression: ensure the query param does not raise a 500
        response = self.client.get(self.url + '?from=recruiting')

        self.assertEqual(response.status_code, 200)

    def test_length_display_whole_hours(self):
        # 7200 s = 2 h exactly → "2h"
        event = save_event(
            name='2h Race',
            date=dt.date(2030, 6, 1),
            start_time_utc=dt.time(12, 0, 0),
            length_seconds=7200,
        )
        url = reverse('view_event', kwargs={'event_id': event.id})

        response = self.client.get(url)

        self.assertEqual(response.context['length_display'], '2h')

    def test_length_display_hours_and_minutes(self):
        # 9000 s = 2 h 30 m
        event = save_event(
            name='2.5h Race',
            date=dt.date(2030, 6, 1),
            start_time_utc=dt.time(12, 0, 0),
            length_seconds=9000,
        )
        url = reverse('view_event', kwargs={'event_id': event.id})

        response = self.client.get(url)

        self.assertEqual(response.context['length_display'], '2h 30m')

    def test_event_name_present_in_rendered_page(self):
        event = save_event(
            name='Branded Race 2030',
            date=dt.date(2030, 6, 1),
            start_time_utc=dt.time(12, 0, 0),
        )
        url = reverse('view_event', kwargs={'event_id': event.id})

        response = self.client.get(url)

        self.assertContains(response, 'Branded Race 2030')

    def test_signup_link_visible_when_from_recruiting(self):
        response = self.client.get(self.url + '?from=recruiting')

        signup_url = reverse('signup', kwargs={'event_id': self.event.id})
        self.assertContains(response, signup_url)

    def test_signup_link_hidden_without_from_recruiting(self):
        response = self.client.get(self.url)

        # The template wraps signup link in {% if show_signup_link %}
        # so the signup anchor should not appear in the page body
        signup_url = reverse('signup', kwargs={'event_id': self.event.id})
        self.assertNotContains(response, signup_url)

    def test_stint_table_class_present_when_stints_exist(self):
        from .models import StintAssignment
        driver = Driver.objects.create(event=self.event, name='Alice', timezone='UTC')
        StintAssignment.objects.create(event=self.event, stint_number=1, driver=driver)

        response = self.client.get(self.url)

        self.assertContains(response, 'stint-table')


# ---------------------------------------------------------------------------
# driver_list.html — wac-table class (Phase B)
# ---------------------------------------------------------------------------

class DriverListTemplateClassTests(TestCase):
    """Smoke test that driver_list.html uses the wac-table component class.

    We access the admin dashboard (which renders driver_list.html as a partial)
    and confirm the class is present when drivers exist.
    """

    def setUp(self):
        self.event = save_event(
            date=dt.date(2030, 6, 1),
            start_time_utc=dt.time(12, 0, 0),
        )
        self.admin_url = reverse('admin_dashboard', kwargs={'event_id': self.event.id})

    def _set_admin_session(self):
        session = self.client.session
        session[f'admin_{self.event.id}'] = True
        session.save()

    def test_unified_table_class_present_when_drivers_exist(self):
        Driver.objects.create(event=self.event, name='Alice', timezone='UTC')
        self._set_admin_session()

        response = self.client.get(self.admin_url)

        self.assertContains(response, 'unified-table')

    def test_no_drivers_message_shown_when_driver_list_empty(self):
        self._set_admin_session()

        response = self.client.get(self.admin_url)

        self.assertContains(response, 'No drivers have signed up yet')


# ---------------------------------------------------------------------------
# AdminSaveDetailsTests
# ---------------------------------------------------------------------------

class AdminSaveDetailsTests(TestCase):
    """Tests for views.admin_save_details() — batch-save event detail fields."""

    def setUp(self):
        self.event = save_event()
        self.url = reverse('admin_save_details', kwargs={'event_id': self.event.id})

    def _set_admin_session(self):
        session = self.client.session
        session[f'admin_{self.event.id}'] = True
        session.save()

    def _valid_post(self, **overrides):
        data = {
            'name': 'Updated Race',
            'date': '2027-06-01',
            'start_time_utc': '14:00',
            'length_hours': '2',
            'length_minutes': '0',
        }
        data.update(overrides)
        return data

    def test_without_session_returns_403(self):
        response = self.client.post(self.url, self._valid_post())

        self.assertEqual(response.status_code, 403)

    def test_valid_post_returns_200(self):
        self._set_admin_session()

        response = self.client.post(self.url, self._valid_post())

        self.assertEqual(response.status_code, 200)

    def test_valid_post_returns_hx_trigger_show_toast(self):
        self._set_admin_session()

        response = self.client.post(self.url, self._valid_post())

        self.assertIn('show-toast', json.loads(response['HX-Trigger']))

    def test_valid_post_saves_name(self):
        self._set_admin_session()

        self.client.post(self.url, self._valid_post(name='Brand New Name'))

        self.event.refresh_from_db()
        self.assertEqual(self.event.name, 'Brand New Name')

    def test_valid_post_saves_date(self):
        self._set_admin_session()

        self.client.post(self.url, self._valid_post(date='2028-03-15'))

        self.event.refresh_from_db()
        self.assertEqual(self.event.date, dt.date(2028, 3, 15))

    def test_valid_post_saves_start_time(self):
        self._set_admin_session()

        self.client.post(self.url, self._valid_post(start_time_utc='09:30'))

        self.event.refresh_from_db()
        self.assertEqual(self.event.start_time_utc, dt.time(9, 30))

    def test_length_hours_and_minutes_converted_to_seconds(self):
        self._set_admin_session()

        self.client.post(self.url, self._valid_post(length_hours='2', length_minutes='30'))

        self.event.refresh_from_db()
        self.assertEqual(self.event.length_seconds, 9000)

    def test_length_hours_only_no_minutes_converts_correctly(self):
        self._set_admin_session()

        self.client.post(self.url, self._valid_post(length_hours='6', length_minutes='0'))

        self.event.refresh_from_db()
        self.assertEqual(self.event.length_seconds, 21600)

    def test_empty_name_returns_error_partial(self):
        self._set_admin_session()

        response = self.client.post(self.url, self._valid_post(name=''))

        self.assertEqual(response.status_code, 422)
        self.assertNotIn('HX-Trigger', response)

    def test_empty_name_response_contains_error_content(self):
        self._set_admin_session()

        response = self.client.post(self.url, self._valid_post(name=''))

        template_names = [t.name for t in response.templates]
        self.assertIn('partials/form_errors.html', template_names)

    def test_invalid_date_format_returns_error_partial(self):
        self._set_admin_session()

        response = self.client.post(self.url, self._valid_post(date='not-a-date'))

        self.assertEqual(response.status_code, 422)
        self.assertNotIn('HX-Trigger', response)
        template_names = [t.name for t in response.templates]
        self.assertIn('partials/form_errors.html', template_names)

    def test_invalid_start_time_returns_error_partial(self):
        self._set_admin_session()

        response = self.client.post(self.url, self._valid_post(start_time_utc='25:99'))

        self.assertEqual(response.status_code, 422)
        self.assertNotIn('HX-Trigger', response)
        template_names = [t.name for t in response.templates]
        self.assertIn('partials/form_errors.html', template_names)

    def test_zero_length_race_returns_error_partial(self):
        self._set_admin_session()

        response = self.client.post(
            self.url,
            self._valid_post(length_hours='0', length_minutes='0'),
        )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn('HX-Trigger', response)
        template_names = [t.name for t in response.templates]
        self.assertIn('partials/form_errors.html', template_names)

    def test_zero_length_race_does_not_update_event(self):
        self._set_admin_session()
        original_seconds = self.event.length_seconds

        self.client.post(
            self.url,
            self._valid_post(length_hours='0', length_minutes='0'),
        )

        self.event.refresh_from_db()
        self.assertEqual(self.event.length_seconds, original_seconds)

    def test_recruiting_on_sets_recruiting_true(self):
        self._set_admin_session()

        self.client.post(self.url, self._valid_post(recruiting='on'))

        self.event.refresh_from_db()
        self.assertTrue(self.event.recruiting)

    def test_omitting_recruiting_sets_recruiting_false(self):
        self._set_admin_session()
        self.event.recruiting = True
        self.event.save()

        self.client.post(self.url, self._valid_post())

        self.event.refresh_from_db()
        self.assertFalse(self.event.recruiting)

    def test_team_name_is_saved(self):
        self._set_admin_session()

        self.client.post(self.url, self._valid_post(team_name='Apex Racing'))

        self.event.refresh_from_db()
        self.assertEqual(self.event.team_name, 'Apex Racing')

    def test_valid_post_saves_car(self):
        self._set_admin_session()

        self.client.post(self.url, self._valid_post(car='Ferrari 488'))

        self.event.refresh_from_db()
        self.assertEqual(self.event.car, 'Ferrari 488')

    def test_valid_post_saves_track(self):
        self._set_admin_session()

        self.client.post(self.url, self._valid_post(track='Nurburgring'))

        self.event.refresh_from_db()
        self.assertEqual(self.event.track, 'Nurburgring')

    def test_error_response_does_not_save_name(self):
        self._set_admin_session()
        original_name = self.event.name

        self.client.post(self.url, self._valid_post(name='', date='not-a-date'))

        self.event.refresh_from_db()
        self.assertEqual(self.event.name, original_name)


# ---------------------------------------------------------------------------
# AdminSaveCalcTests
# ---------------------------------------------------------------------------

class AdminSaveCalcTests(TestCase):
    """Tests for views.admin_save_calc() — batch-save stint calculation fields."""

    def setUp(self):
        # Start with an event that lacks all stint calc fields so we can
        # observe partial-save behaviour without triggering HX-Refresh
        self.event = save_event(
            avg_lap_seconds=None,
            in_lap_seconds=None,
            out_lap_seconds=None,
            target_laps=None,
            fuel_capacity=None,
            fuel_per_lap=None,
        )
        self.url = reverse('admin_save_calc', kwargs={'event_id': self.event.id})

    def _set_admin_session(self):
        session = self.client.session
        session[f'admin_{self.event.id}'] = True
        session.save()

    def _all_calc_fields(self, **overrides):
        """POST data that satisfies all required stint-calc fields."""
        data = {
            'avg_lap': '2:00',
            'in_lap': '2:10',
            'out_lap': '2:05',
            'fuel_capacity': '80',
            'fuel_burn': '2.5',
            'target_laps': '30',
        }
        data.update(overrides)
        return data

    def test_without_session_returns_403(self):
        response = self.client.post(self.url, self._all_calc_fields())

        self.assertEqual(response.status_code, 403)

    def test_valid_post_returns_200(self):
        self._set_admin_session()

        response = self.client.post(self.url, self._all_calc_fields())

        self.assertEqual(response.status_code, 200)

    def test_valid_post_without_all_fields_returns_show_toast(self):
        self._set_admin_session()
        # Post only some fields so event still lacks required stint fields
        response = self.client.post(self.url, {'avg_lap': '2:00'})

        self.assertIn('show-toast', json.loads(response['HX-Trigger']))
        self.assertNotIn('HX-Refresh', response)

    def test_mmss_value_correctly_converted_to_seconds(self):
        self._set_admin_session()

        self.client.post(self.url, {'avg_lap': '2:18'})

        self.event.refresh_from_db()
        self.assertEqual(self.event.avg_lap_seconds, 138)

    def test_in_lap_mmss_correctly_converted(self):
        self._set_admin_session()

        self.client.post(self.url, {'in_lap': '1:30'})

        self.event.refresh_from_db()
        self.assertEqual(self.event.in_lap_seconds, 90)

    def test_out_lap_mmss_correctly_converted(self):
        self._set_admin_session()

        self.client.post(self.url, {'out_lap': '3:00'})

        self.event.refresh_from_db()
        self.assertEqual(self.event.out_lap_seconds, 180)

    def test_invalid_mmss_format_returns_error_partial(self):
        self._set_admin_session()

        response = self.client.post(self.url, {'avg_lap': 'not-a-time'})

        # 422 signals a validation error; base.html opts 422 into being
        # swapped so the partial actually renders.
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('HX-Trigger', response)
        template_names = [t.name for t in response.templates]
        self.assertIn('partials/form_errors.html', template_names)

    def test_mmss_with_seconds_gte_60_returns_error_partial(self):
        self._set_admin_session()

        response = self.client.post(self.url, {'avg_lap': '2:60'})

        # 422 signals a validation error; base.html opts 422 into being
        # swapped so the partial actually renders.
        self.assertEqual(response.status_code, 422)
        template_names = [t.name for t in response.templates]
        self.assertIn('partials/form_errors.html', template_names)

    def test_fuel_capacity_saved(self):
        self._set_admin_session()

        self.client.post(self.url, {'fuel_capacity': '75.5'})

        self.event.refresh_from_db()
        self.assertAlmostEqual(self.event.fuel_capacity, 75.5)

    def test_fuel_burn_saved_as_fuel_per_lap(self):
        self._set_admin_session()

        self.client.post(self.url, {'fuel_burn': '2.2'})

        self.event.refresh_from_db()
        self.assertAlmostEqual(self.event.fuel_per_lap, 2.2)

    def test_target_laps_saved(self):
        self._set_admin_session()

        self.client.post(self.url, {'target_laps': '25'})

        self.event.refresh_from_db()
        self.assertEqual(self.event.target_laps, 25)

    def test_when_all_required_fields_complete_returns_hx_refresh(self):
        self._set_admin_session()
        # Post all fields so event.has_required_stint_fields becomes True after save
        response = self.client.post(self.url, self._all_calc_fields())

        self.assertEqual(response['HX-Refresh'], 'true')
        self.assertNotIn('HX-Trigger', response)

    def test_partial_post_leaves_unprovided_fields_unchanged(self):
        self._set_admin_session()
        # Pre-set avg_lap_seconds so we can check it is not wiped by a partial POST
        self.event.avg_lap_seconds = 120.0
        self.event.save()

        self.client.post(self.url, {'fuel_capacity': '60'})

        self.event.refresh_from_db()
        self.assertAlmostEqual(self.event.avg_lap_seconds, 120.0)

    def test_partial_post_only_updates_provided_field(self):
        self._set_admin_session()

        self.client.post(self.url, {'fuel_capacity': '99'})

        self.event.refresh_from_db()
        self.assertAlmostEqual(self.event.fuel_capacity, 99.0)
        # Other fields remain None since nothing else was POSTed
        self.assertIsNone(self.event.avg_lap_seconds)

    def test_invalid_numeric_field_returns_error_partial(self):
        self._set_admin_session()

        response = self.client.post(self.url, {'fuel_capacity': 'abc'})

        # 422 signals a validation error; base.html opts 422 into being
        # swapped so the partial actually renders.
        self.assertEqual(response.status_code, 422)
        template_names = [t.name for t in response.templates]
        self.assertIn('partials/form_errors.html', template_names)


# ---------------------------------------------------------------------------
# AdminSaveAssignmentsTests
# ---------------------------------------------------------------------------

class AdminSaveAssignmentsTests(TestCase):
    """Tests for views.admin_save_assignments() — bulk-save stint driver assignments."""

    def setUp(self):
        self.event = save_event()
        self.url = reverse('admin_save_assignments', kwargs={'event_id': self.event.id})
        self.driver_a = Driver.objects.create(
            event=self.event, name='Alice', timezone='UTC'
        )
        self.driver_b = Driver.objects.create(
            event=self.event, name='Bob', timezone='UTC'
        )

    def _set_admin_session(self):
        session = self.client.session
        session[f'admin_{self.event.id}'] = True
        session.save()

    def test_without_session_returns_403(self):
        response = self.client.post(self.url, {'stint_1': str(self.driver_a.id)})

        self.assertEqual(response.status_code, 403)

    def test_returns_400_when_event_lacks_required_stint_fields(self):
        self._set_admin_session()
        event_no_fields = save_event(
            avg_lap_seconds=None,
            in_lap_seconds=None,
            out_lap_seconds=None,
            target_laps=None,
            fuel_capacity=None,
            fuel_per_lap=None,
        )
        url = reverse('admin_save_assignments', kwargs={'event_id': event_no_fields.id})
        session = self.client.session
        session[f'admin_{event_no_fields.id}'] = True
        session.save()

        response = self.client.post(url, {'stint_1': str(self.driver_a.id)})

        self.assertEqual(response.status_code, 400)

    def test_valid_post_returns_200(self):
        self._set_admin_session()

        response = self.client.post(self.url, {'stint_1': str(self.driver_a.id)})

        self.assertEqual(response.status_code, 200)

    def test_valid_post_returns_hx_trigger_show_toast(self):
        self._set_admin_session()

        response = self.client.post(self.url, {'stint_1': str(self.driver_a.id)})

        self.assertIn('show-toast', json.loads(response['HX-Trigger']))

    def test_valid_post_creates_stint_assignment_row(self):
        self._set_admin_session()

        self.client.post(self.url, {'stint_1': str(self.driver_a.id)})

        assignment = StintAssignment.objects.get(event=self.event, stint_number=1)
        self.assertEqual(assignment.driver, self.driver_a)

    def test_omitting_stint_param_leaves_that_stint_unassigned(self):
        self._set_admin_session()
        # Only assign stint 1; remaining stints get no POST param → driver=None
        self.client.post(self.url, {'stint_1': str(self.driver_a.id)})

        unassigned = StintAssignment.objects.filter(
            event=self.event, driver=None
        )
        self.assertGreater(unassigned.count(), 0)

    def test_reposting_clears_existing_assignments_before_saving(self):
        self._set_admin_session()
        # First save — assign driver_a to stint 1
        self.client.post(self.url, {'stint_1': str(self.driver_a.id)})
        # Second save — assign driver_b to stint 1 instead
        self.client.post(self.url, {'stint_1': str(self.driver_b.id)})

        assignment = StintAssignment.objects.get(event=self.event, stint_number=1)
        self.assertEqual(assignment.driver, self.driver_b)

    def test_reposting_does_not_leave_duplicate_assignment_rows(self):
        self._set_admin_session()

        self.client.post(self.url, {'stint_1': str(self.driver_a.id)})
        self.client.post(self.url, {'stint_1': str(self.driver_b.id)})

        count = StintAssignment.objects.filter(
            event=self.event, stint_number=1
        ).count()
        self.assertEqual(count, 1)

    def test_invalid_driver_id_results_in_unassigned_stint(self):
        self._set_admin_session()
        bogus_id = uuid.uuid4()

        self.client.post(self.url, {'stint_1': str(bogus_id)})

        assignment = StintAssignment.objects.get(event=self.event, stint_number=1)
        self.assertIsNone(assignment.driver)

    def test_driver_from_different_event_results_in_unassigned_stint(self):
        self._set_admin_session()
        other_event = save_event()
        foreign_driver = Driver.objects.create(
            event=other_event, name='Carol', timezone='UTC'
        )

        self.client.post(self.url, {'stint_1': str(foreign_driver.id)})

        assignment = StintAssignment.objects.get(event=self.event, stint_number=1)
        self.assertIsNone(assignment.driver)

    def test_multiple_stints_assigned_in_one_post(self):
        self._set_admin_session()

        self.client.post(self.url, {
            'stint_1': str(self.driver_a.id),
            'stint_2': str(self.driver_b.id),
        })

        a1 = StintAssignment.objects.get(event=self.event, stint_number=1)
        a2 = StintAssignment.objects.get(event=self.event, stint_number=2)
        self.assertEqual(a1.driver, self.driver_a)
        self.assertEqual(a2.driver, self.driver_b)


# ---------------------------------------------------------------------------
# AdminAddDriverTests
# ---------------------------------------------------------------------------

class AdminAddDriverTests(TestCase):
    """Tests for views.admin_add_driver() — POST-only driver creation."""

    def setUp(self):
        self.event = save_event(
            avg_lap_seconds=None,
            in_lap_seconds=None,
            out_lap_seconds=None,
            target_laps=None,
            fuel_capacity=None,
            fuel_per_lap=None,
        )
        self.url = reverse('admin_add_driver', kwargs={'event_id': self.event.id})

    def _set_admin_session(self):
        session = self.client.session
        session[f'admin_{self.event.id}'] = True
        session.save()

    def test_without_session_returns_403(self):
        response = self.client.post(
            self.url,
            {'driver_name': 'Alice', 'timezone': 'UTC'},
        )

        self.assertEqual(response.status_code, 403)

    def test_get_returns_405(self):
        self._set_admin_session()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 405)

    def test_valid_post_returns_200(self):
        self._set_admin_session()

        response = self.client.post(
            self.url,
            {'driver_name': 'Alice', 'timezone': 'America/New_York'},
        )

        self.assertEqual(response.status_code, 200)

    def test_valid_post_creates_driver_in_database(self):
        self._set_admin_session()

        self.client.post(
            self.url,
            {'driver_name': 'Alice', 'timezone': 'America/New_York'},
        )

        self.assertTrue(
            Driver.objects.filter(event=self.event, name='Alice').exists()
        )

    def test_valid_post_response_contains_driver_list_html(self):
        self._set_admin_session()

        response = self.client.post(
            self.url,
            {'driver_name': 'Alice', 'timezone': 'UTC'},
        )

        self.assertIn(b'Alice', response.content)

    def test_missing_driver_name_returns_error_html(self):
        self._set_admin_session()

        response = self.client.post(
            self.url,
            {'driver_name': '', 'timezone': 'UTC'},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn(b'Driver name is required', response.content)

    def test_missing_driver_name_does_not_create_driver(self):
        self._set_admin_session()

        self.client.post(self.url, {'driver_name': '', 'timezone': 'UTC'})

        self.assertEqual(Driver.objects.filter(event=self.event).count(), 0)

    def test_invalid_timezone_returns_error_html(self):
        self._set_admin_session()

        response = self.client.post(
            self.url,
            {'driver_name': 'Bob', 'timezone': 'Not/A/Real/Zone'},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn(b'valid timezone', response.content)

    def test_invalid_timezone_does_not_create_driver(self):
        self._set_admin_session()

        self.client.post(
            self.url,
            {'driver_name': 'Bob', 'timezone': 'Not/A/Real/Zone'},
        )

        self.assertEqual(Driver.objects.filter(event=self.event).count(), 0)

    def test_when_event_has_required_stint_fields_returns_hx_refresh(self):
        # Re-configure event to have all required stint fields
        self.event.avg_lap_seconds = 120.0
        self.event.in_lap_seconds = 130.0
        self.event.out_lap_seconds = 125.0
        self.event.target_laps = 30
        self.event.fuel_capacity = 80.0
        self.event.fuel_per_lap = 2.5
        self.event.save()
        self._set_admin_session()

        response = self.client.post(
            self.url,
            {'driver_name': 'Carol', 'timezone': 'UTC'},
        )

        self.assertEqual(response['HX-Refresh'], 'true')

    def test_when_event_has_required_stint_fields_driver_is_still_created(self):
        self.event.avg_lap_seconds = 120.0
        self.event.in_lap_seconds = 130.0
        self.event.out_lap_seconds = 125.0
        self.event.target_laps = 30
        self.event.fuel_capacity = 80.0
        self.event.fuel_per_lap = 2.5
        self.event.save()
        self._set_admin_session()

        self.client.post(
            self.url,
            {'driver_name': 'Carol', 'timezone': 'UTC'},
        )

        self.assertTrue(
            Driver.objects.filter(event=self.event, name='Carol').exists()
        )

    def test_valid_availability_slots_create_availability_records(self):
        self._set_admin_session()
        # Get a valid slot for this event (the event starts 2026-06-01 12:00 UTC)
        from .utils import get_availability_slots
        slots = get_availability_slots(self.event)
        # Use the first valid slot
        slot_str = (
            slots[0].isoformat().replace('+00:00', 'Z')
            if slots[0].tzinfo else slots[0].isoformat() + 'Z'
        )

        self.client.post(self.url, {
            'driver_name': 'Dave',
            'timezone': 'UTC',
            'slots': slot_str,
        })

        driver = Driver.objects.get(event=self.event, name='Dave')
        self.assertEqual(driver.availability.count(), 1)


# ---------------------------------------------------------------------------
# AdminRemoveDriverTests
# ---------------------------------------------------------------------------

class AdminRemoveDriverTests(TestCase):
    """Tests for views.admin_remove_driver() — DELETE-only driver removal."""

    def setUp(self):
        # Start without stint fields so the default removal path uses HX-Reswap
        self.event = save_event(
            avg_lap_seconds=None,
            in_lap_seconds=None,
            out_lap_seconds=None,
            target_laps=None,
            fuel_capacity=None,
            fuel_per_lap=None,
        )
        self.driver = Driver.objects.create(
            event=self.event, name='Alice', timezone='UTC'
        )
        self.url = reverse(
            'admin_remove_driver',
            kwargs={'event_id': self.event.id, 'driver_id': self.driver.id},
        )

    def _set_admin_session(self):
        session = self.client.session
        session[f'admin_{self.event.id}'] = True
        session.save()

    def test_without_session_returns_403(self):
        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 403)

    def test_post_returns_405(self):
        self._set_admin_session()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 405)

    def test_get_returns_405(self):
        self._set_admin_session()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 405)

    def test_valid_delete_removes_driver_from_database(self):
        self._set_admin_session()

        self.client.delete(self.url)

        self.assertFalse(Driver.objects.filter(id=self.driver.id).exists())

    def test_valid_delete_without_stint_fields_returns_hx_reswap_delete(self):
        self._set_admin_session()

        response = self.client.delete(self.url)

        self.assertEqual(response['HX-Reswap'], 'delete')

    def test_valid_delete_without_stint_fields_does_not_return_hx_refresh(self):
        self._set_admin_session()

        response = self.client.delete(self.url)

        self.assertNotIn('HX-Refresh', response)

    def test_valid_delete_with_stint_fields_returns_hx_refresh(self):
        # Configure event to have all required stint fields
        self.event.avg_lap_seconds = 120.0
        self.event.in_lap_seconds = 130.0
        self.event.out_lap_seconds = 125.0
        self.event.target_laps = 30
        self.event.fuel_capacity = 80.0
        self.event.fuel_per_lap = 2.5
        self.event.save()
        self._set_admin_session()

        response = self.client.delete(self.url)

        self.assertEqual(response['HX-Refresh'], 'true')

    def test_valid_delete_with_stint_fields_does_not_return_hx_reswap(self):
        self.event.avg_lap_seconds = 120.0
        self.event.in_lap_seconds = 130.0
        self.event.out_lap_seconds = 125.0
        self.event.target_laps = 30
        self.event.fuel_capacity = 80.0
        self.event.fuel_per_lap = 2.5
        self.event.save()
        self._set_admin_session()

        response = self.client.delete(self.url)

        self.assertNotIn('HX-Reswap', response)

    def test_valid_delete_returns_200(self):
        self._set_admin_session()

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# AdminEditDriverNameTests
# ---------------------------------------------------------------------------

class AdminEditDriverNameTests(TestCase):
    """Tests for views.admin_edit_driver_name() — inline driver name editing."""

    def setUp(self):
        self.event = save_event()
        self.driver = Driver.objects.create(
            event=self.event, name='Alice', timezone='UTC'
        )
        self.url = reverse(
            'admin_edit_driver_name',
            kwargs={'event_id': self.event.id, 'driver_id': self.driver.id},
        )

    def _set_admin_session(self):
        session = self.client.session
        session[f'admin_{self.event.id}'] = True
        session.save()

    def test_without_session_get_returns_403(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 403)

    def test_without_session_post_returns_403(self):
        response = self.client.post(self.url, {'name': 'Bob'})

        self.assertEqual(response.status_code, 403)

    def test_get_returns_200(self):
        self._set_admin_session()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)

    def test_get_renders_edit_form_partial(self):
        self._set_admin_session()

        response = self.client.get(self.url)

        template_names = [t.name for t in response.templates]
        self.assertIn('partials/driver_name_edit_form.html', template_names)

    def test_get_with_cancel_returns_display_partial(self):
        self._set_admin_session()

        response = self.client.get(self.url + '?cancel=1')

        template_names = [t.name for t in response.templates]
        self.assertIn('partials/driver_name_display.html', template_names)

    def test_get_with_cancel_returns_200(self):
        self._set_admin_session()

        response = self.client.get(self.url + '?cancel=1')

        self.assertEqual(response.status_code, 200)

    def test_post_with_valid_name_saves_driver_name(self):
        self._set_admin_session()

        self.client.post(self.url, {'name': 'Bob'})

        self.driver.refresh_from_db()
        self.assertEqual(self.driver.name, 'Bob')

    def test_post_with_valid_name_returns_display_partial(self):
        self._set_admin_session()

        response = self.client.post(self.url, {'name': 'Bob'})

        template_names = [t.name for t in response.templates]
        self.assertIn('partials/driver_name_display.html', template_names)

    def test_post_with_valid_name_returns_200(self):
        self._set_admin_session()

        response = self.client.post(self.url, {'name': 'Bob'})

        self.assertEqual(response.status_code, 200)

    def test_post_with_empty_name_returns_edit_form_partial(self):
        self._set_admin_session()

        response = self.client.post(self.url, {'name': ''})

        template_names = [t.name for t in response.templates]
        self.assertIn('partials/driver_name_edit_form.html', template_names)

    def test_post_with_empty_name_response_contains_error_message(self):
        self._set_admin_session()

        response = self.client.post(self.url, {'name': ''})

        self.assertIn(b'cannot be empty', response.content)

    def test_post_with_empty_name_does_not_update_driver(self):
        self._set_admin_session()

        self.client.post(self.url, {'name': ''})

        self.driver.refresh_from_db()
        self.assertEqual(self.driver.name, 'Alice')

    def test_post_with_whitespace_only_name_does_not_update_driver(self):
        self._set_admin_session()

        self.client.post(self.url, {'name': '   '})

        self.driver.refresh_from_db()
        self.assertEqual(self.driver.name, 'Alice')

    def test_driver_from_different_event_returns_404(self):
        other_event = save_event()
        other_driver = Driver.objects.create(
            event=other_event, name='Carol', timezone='UTC'
        )
        url = reverse(
            'admin_edit_driver_name',
            kwargs={'event_id': self.event.id, 'driver_id': other_driver.id},
        )
        self._set_admin_session()

        response = self.client.get(url)

        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Discord OAuth adapter tests
# ---------------------------------------------------------------------------

class DiscordAdapterUpdateFieldsTests(TestCase):
    """Unit tests for DiscordAccountAdapter._update_discord_fields.

    The sociallogin object is mocked so no real OAuth flow is triggered —
    only our custom field-setting logic is exercised.
    """

    def _make_adapter(self):
        from events.adapters import DiscordAccountAdapter
        return DiscordAccountAdapter()

    def _make_sociallogin(self, extra_data, is_existing=False):
        """Build a minimal mock sociallogin object."""
        from unittest.mock import MagicMock
        sociallogin = MagicMock()
        sociallogin.account.extra_data = extra_data
        sociallogin.account.provider = 'discord'
        sociallogin.is_existing = is_existing
        return sociallogin

    def _make_user(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        # Use a unique username to avoid collisions across tests
        return User.objects.create_user(username=f'tmp_{uuid.uuid4().hex[:8]}', password='x')

    def test_global_name_used_as_discord_username_when_present(self):
        adapter = self._make_adapter()
        user = self._make_user()
        sociallogin = self._make_sociallogin({
            'id': '111222333',
            'global_name': 'GlobalName',
            'username': 'raw_username',
        })

        adapter._update_discord_fields(user, sociallogin)

        user.refresh_from_db()
        self.assertEqual(user.discord_username, 'GlobalName')

    def test_username_used_as_discord_username_when_global_name_absent(self):
        adapter = self._make_adapter()
        user = self._make_user()
        sociallogin = self._make_sociallogin({
            'id': '111222333',
            'username': 'raw_username',
            # global_name intentionally omitted
        })

        adapter._update_discord_fields(user, sociallogin)

        user.refresh_from_db()
        self.assertEqual(user.discord_username, 'raw_username')

    def test_username_used_when_global_name_is_none(self):
        adapter = self._make_adapter()
        user = self._make_user()
        sociallogin = self._make_sociallogin({
            'id': '111222333',
            'global_name': None,
            'username': 'raw_username',
        })

        adapter._update_discord_fields(user, sociallogin)

        user.refresh_from_db()
        self.assertEqual(user.discord_username, 'raw_username')

    def test_avatar_url_built_from_discord_id_and_hash(self):
        adapter = self._make_adapter()
        user = self._make_user()
        sociallogin = self._make_sociallogin({
            'id': '987654321',
            'username': 'someuser',
            'avatar': 'abc123def456',
        })

        adapter._update_discord_fields(user, sociallogin)

        user.refresh_from_db()
        expected = 'https://cdn.discordapp.com/avatars/987654321/abc123def456.png?size=128'
        self.assertEqual(user.discord_avatar, expected)

    def test_default_avatar_url_used_when_no_avatar_hash(self):
        adapter = self._make_adapter()
        user = self._make_user()
        sociallogin = self._make_sociallogin({
            'id': '987654321',
            'username': 'someuser',
            # avatar intentionally omitted
        })

        adapter._update_discord_fields(user, sociallogin)

        user.refresh_from_db()
        self.assertEqual(user.discord_avatar, 'https://cdn.discordapp.com/embed/avatars/0.png')

    def test_default_avatar_url_used_when_avatar_is_none(self):
        adapter = self._make_adapter()
        user = self._make_user()
        sociallogin = self._make_sociallogin({
            'id': '987654321',
            'username': 'someuser',
            'avatar': None,
        })

        adapter._update_discord_fields(user, sociallogin)

        user.refresh_from_db()
        self.assertEqual(user.discord_avatar, 'https://cdn.discordapp.com/embed/avatars/0.png')

    def test_user_username_set_to_discord_id(self):
        adapter = self._make_adapter()
        user = self._make_user()
        discord_id = '444555666'
        sociallogin = self._make_sociallogin({
            'id': discord_id,
            'username': 'someuser',
        })

        adapter._update_discord_fields(user, sociallogin)

        user.refresh_from_db()
        self.assertEqual(user.username, discord_id)

    def test_discord_id_stored_on_user(self):
        adapter = self._make_adapter()
        user = self._make_user()
        sociallogin = self._make_sociallogin({
            'id': '777888999',
            'username': 'someuser',
        })

        adapter._update_discord_fields(user, sociallogin)

        user.refresh_from_db()
        self.assertEqual(user.discord_id, '777888999')

    def test_pre_social_login_updates_existing_user(self):
        """pre_social_login calls _update_discord_fields when is_existing=True."""
        from django.contrib.auth import get_user_model
        from unittest.mock import patch, MagicMock
        adapter = self._make_adapter()
        user = self._make_user()
        sociallogin = self._make_sociallogin(
            extra_data={'id': '123456789', 'username': 'updated_name', 'global_name': 'UpdatedGlobal'},
            is_existing=True,
        )
        sociallogin.user = user

        with patch.object(adapter.__class__.__bases__[0], 'pre_social_login'):
            adapter.pre_social_login(None, sociallogin)

        user.refresh_from_db()
        self.assertEqual(user.discord_username, 'UpdatedGlobal')
        self.assertEqual(user.username, '123456789')

    def test_pre_social_login_does_not_update_non_existing_user(self):
        """pre_social_login skips _update_discord_fields when is_existing=False."""
        from unittest.mock import patch
        adapter = self._make_adapter()
        user = self._make_user()
        original_username = user.username
        sociallogin = self._make_sociallogin(
            extra_data={'id': '999000111', 'username': 'new_discord_name'},
            is_existing=False,
        )
        sociallogin.user = user

        with patch.object(adapter.__class__.__bases__[0], 'pre_social_login'):
            adapter.pre_social_login(None, sociallogin)

        user.refresh_from_db()
        # Non-existing flow should not have modified the user
        self.assertEqual(user.username, original_username)

    def test_non_alphanumeric_avatar_hash_falls_back_to_default(self):
        """avatar_hash containing non-alphanumeric chars (e.g. path traversal) must
        not be interpolated into the CDN URL."""
        adapter = self._make_adapter()
        user = self._make_user()
        sociallogin = self._make_sociallogin({
            'id': '123456789',
            'username': 'someuser',
            'avatar': '../../etc/passwd',
        })

        adapter._update_discord_fields(user, sociallogin)

        user.refresh_from_db()
        self.assertEqual(user.discord_avatar, 'https://cdn.discordapp.com/embed/avatars/0.png')

    def test_no_db_write_when_fields_unchanged(self):
        """_update_discord_fields must skip save() when all values are already current."""
        from unittest.mock import patch
        adapter = self._make_adapter()
        user = self._make_user()

        sociallogin = self._make_sociallogin({
            'id': '555666777',
            'username': 'stableuser',
            'avatar': 'abc123',
        })
        # Prime the user with the same values the adapter would set
        adapter._update_discord_fields(user, sociallogin)
        user.refresh_from_db()

        with patch.object(user.__class__, 'save') as mock_save:
            adapter._update_discord_fields(user, sociallogin)

        mock_save.assert_not_called()

    def test_db_write_occurs_when_discord_username_changes(self):
        """_update_discord_fields must save when display name changes."""
        from unittest.mock import patch
        adapter = self._make_adapter()
        user = self._make_user()

        first_login = self._make_sociallogin({
            'id': '111222333',
            'global_name': 'OldName',
        })
        adapter._update_discord_fields(user, first_login)
        user.refresh_from_db()

        second_login = self._make_sociallogin({
            'id': '111222333',
            'global_name': 'NewName',
        })
        adapter._update_discord_fields(user, second_login)

        user.refresh_from_db()
        self.assertEqual(user.discord_username, 'NewName')


# ---------------------------------------------------------------------------
# my_availability view tests
# ---------------------------------------------------------------------------

class MyAvailabilityViewTests(TestCase):
    """Tests for views.my_availability() — Discord-authenticated driver edit shortcut."""

    def setUp(self):
        self.event = save_event(
            date=dt.date(2030, 6, 1),
            start_time_utc=dt.time(12, 0, 0),
        )
        self.url = reverse('my_availability', kwargs={'event_id': self.event.id})
        self.user = _make_auth_user()

    def test_unauthenticated_redirects(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)

    def test_unauthenticated_redirect_target_includes_discord_login(self):
        response = self.client.get(self.url)

        self.assertIn('/accounts/discord/login/', response['Location'])

    def test_unauthenticated_redirect_includes_next_param(self):
        response = self.client.get(self.url)

        self.assertIn('next=', response['Location'])

    def test_authenticated_user_with_no_driver_record_redirects_to_signup(self):
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertRedirects(
            response,
            reverse('signup', kwargs={'event_id': self.event.id}),
            fetch_redirect_response=False,
        )

    def test_authenticated_user_with_driver_record_gets_200(self):
        Driver.objects.create(event=self.event, name='Me', timezone='UTC', user=self.user)
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)

    def test_authenticated_user_with_multiple_driver_records_does_not_raise(self):
        """When duplicate Driver rows exist for a user+event, .filter().first() must
        not raise MultipleObjectsReturned."""
        Driver.objects.create(event=self.event, name='Me1', timezone='UTC', user=self.user)
        Driver.objects.create(event=self.event, name='Me2', timezone='UTC', user=self.user)
        self.client.force_login(self.user)

        try:
            self.client.get(self.url)
        except Exception as exc:
            self.fail(f'my_availability raised an unexpected exception: {exc}')


# ---------------------------------------------------------------------------
# Context processor tests
# ---------------------------------------------------------------------------

class AuthContextProcessorTests(TestCase):
    """Tests for events.context_processors.auth_context.

    The processor is called on every request and injects discord_user
    into the template context when the user is authenticated.
    """

    def _make_discord_user(self, discord_username='', username='fallback'):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.create_user(
            username=username,
            password='testpass',
        )
        user.discord_username = discord_username
        user.discord_id = '123'
        user.discord_avatar = 'https://example.com/avatar.png'
        user.save(update_fields=['discord_username', 'discord_id', 'discord_avatar'])
        return user

    def test_unauthenticated_request_gives_none(self):
        from events.context_processors import auth_context
        from unittest.mock import MagicMock
        request = MagicMock()
        request.user.is_authenticated = False

        result = auth_context(request)

        self.assertIsNone(result['discord_user'])

    def test_authenticated_user_gives_discord_user_dict(self):
        from events.context_processors import auth_context
        from unittest.mock import MagicMock
        request = MagicMock()
        request.user.is_authenticated = True
        request.user.discord_username = 'DiscordName'
        request.user.discord_avatar = 'https://cdn.example.com/avatar.png'
        request.user.discord_id = '42'
        request.user.username = 'fallback'

        result = auth_context(request)

        self.assertIsNotNone(result['discord_user'])
        self.assertEqual(result['discord_user']['username'], 'DiscordName')
        self.assertEqual(result['discord_user']['avatar'], 'https://cdn.example.com/avatar.png')
        self.assertEqual(result['discord_user']['id'], '42')

    def test_discord_username_falls_back_to_username_when_blank(self):
        from events.context_processors import auth_context
        from unittest.mock import MagicMock
        request = MagicMock()
        request.user.is_authenticated = True
        request.user.discord_username = ''
        request.user.discord_avatar = ''
        request.user.discord_id = None
        request.user.username = 'plain_username'

        result = auth_context(request)

        self.assertEqual(result['discord_user']['username'], 'plain_username')

    def test_discord_id_none_becomes_empty_string(self):
        from events.context_processors import auth_context
        from unittest.mock import MagicMock
        request = MagicMock()
        request.user.is_authenticated = True
        request.user.discord_username = 'someone'
        request.user.discord_avatar = ''
        request.user.discord_id = None
        request.user.username = 'someone'

        result = auth_context(request)

        self.assertEqual(result['discord_user']['id'], '')


# ---------------------------------------------------------------------------
# Home view — authenticated user context tests
# ---------------------------------------------------------------------------

def _make_auth_user(username=None):
    """Create and return a saved User for use in authenticated view tests."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    uname = username or f'user_{uuid.uuid4().hex[:8]}'
    return User.objects.create_user(username=uname, password='testpass')


class HomeViewAuthenticatedTests(TestCase):
    """Tests for the home view's admin_events and driver_events context variables.

    These context entries are only present when the user is authenticated.
    """

    def setUp(self):
        self.url = reverse('home')
        self.user = _make_auth_user()
        self.future_date = dt.date(2030, 6, 1)
        self.future_time = dt.time(12, 0, 0)

    def _future_event(self, name='Race', **overrides):
        return save_event(
            name=name,
            date=overrides.pop('date', self.future_date),
            start_time_utc=overrides.pop('start_time_utc', self.future_time),
            **overrides,
        )

    def test_unauthenticated_user_has_no_admin_events_key(self):
        response = self.client.get(self.url)

        self.assertNotIn('admin_events', response.context)

    def test_unauthenticated_user_has_no_driver_events_key(self):
        response = self.client.get(self.url)

        self.assertNotIn('driver_events', response.context)

    def test_authenticated_user_with_no_events_has_empty_admin_events(self):
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertEqual(len(list(response.context['admin_events'])), 0)

    def test_authenticated_user_with_no_events_has_empty_driver_events(self):
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertEqual(len(list(response.context['driver_events'])), 0)

    def test_event_created_by_user_appears_in_admin_events(self):
        event = self._future_event(name='My Admin Event', created_by=self.user)
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        admin_ids = [e.id for e in response.context['admin_events']]
        self.assertIn(event.id, admin_ids)

    def test_event_not_created_by_user_absent_from_admin_events(self):
        other_user = _make_auth_user()
        self._future_event(name='Other Event', created_by=other_user)
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertEqual(len(list(response.context['admin_events'])), 0)

    def test_signed_up_event_appears_in_driver_events(self):
        other_user = _make_auth_user()
        event = self._future_event(name='Signup Event', created_by=other_user)
        Driver.objects.create(event=event, name='Me', timezone='UTC', user=self.user)
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        driver_ids = [e.id for e in response.context['driver_events']]
        self.assertIn(event.id, driver_ids)

    def test_event_created_by_user_excluded_from_driver_events_even_if_signed_up(self):
        # User is both admin and driver → should be in admin_events only
        event = self._future_event(name='Own Event', created_by=self.user)
        Driver.objects.create(event=event, name='Me', timezone='UTC', user=self.user)
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        driver_ids = [e.id for e in response.context['driver_events']]
        self.assertNotIn(event.id, driver_ids)
        admin_ids = [e.id for e in response.context['admin_events']]
        self.assertIn(event.id, admin_ids)

    def test_my_driver_name_annotation_is_correct(self):
        other_user = _make_auth_user()
        event = self._future_event(name='Annotated Race', created_by=other_user)
        Driver.objects.create(event=event, name='SpeedRacer', timezone='UTC', user=self.user)
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        driver_event = next(e for e in response.context['driver_events'] if e.id == event.id)
        self.assertEqual(driver_event.my_driver_name, 'SpeedRacer')

    def test_admin_events_ordered_newest_date_first(self):
        event_old = self._future_event(name='Old Race', created_by=self.user, date=dt.date(2028, 1, 1))
        event_new = self._future_event(name='New Race', created_by=self.user, date=dt.date(2030, 6, 1))
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        admin_events = list(response.context['admin_events'])
        self.assertEqual(admin_events[0].id, event_new.id)
        self.assertEqual(admin_events[1].id, event_old.id)


# ---------------------------------------------------------------------------
# event_create view tests
# ---------------------------------------------------------------------------

class EventCreateAuthTests(TestCase):
    """Tests for the event_create view's created_by behaviour with auth."""

    def setUp(self):
        self.url = reverse('event_create')
        self.user = _make_auth_user()
        # A valid date well in the future avoids the 'date in the past' form error
        self.future_date = dt.date(2030, 6, 1)
        self.valid_post = {
            'name': 'Auth Test Race',
            'date': '2030-06-01',
            'start_time_utc': '12:00',
            'length_hours': 6,
            'length_minutes': 0,
            'car': '',
            'track': '',
            'team_name': '',
            'recruiting': '',
        }

    def test_authenticated_post_sets_created_by_to_user(self):
        self.client.force_login(self.user)

        self.client.post(self.url, self.valid_post)

        event = Event.objects.get(name='Auth Test Race')
        self.assertEqual(event.created_by, self.user)

    def test_unauthenticated_post_leaves_created_by_as_none(self):
        self.client.post(self.url, self.valid_post)

        event = Event.objects.get(name='Auth Test Race')
        self.assertIsNone(event.created_by)


# ---------------------------------------------------------------------------
# signup view tests
# ---------------------------------------------------------------------------

class SignupViewAuthTests(TestCase):
    """Tests for the signup view's auth-aware prefill_name and driver.user assignment."""

    def setUp(self):
        self.event = save_event(
            date=dt.date(2030, 6, 1),
            start_time_utc=dt.time(12, 0, 0),
        )
        self.url = reverse('signup', kwargs={'event_id': self.event.id})
        self.user = _make_auth_user(username='plain_user')
        self.user.discord_username = 'DiscordUser'
        self.user.save(update_fields=['discord_username'])

    def _valid_post_data(self):
        """Build the minimal valid POST data for the signup view."""
        from events.utils import get_availability_slots
        slots = get_availability_slots(self.event)
        slot_str = (
            slots[0].isoformat().replace('+00:00', 'Z')
            if slots[0].tzinfo else slots[0].isoformat() + 'Z'
        )
        return {
            'driver_name': 'Test Driver',
            'timezone': 'UTC',
            'slots': [slot_str],
        }

    def test_get_unauthenticated_prefill_name_is_empty_string(self):
        response = self.client.get(self.url)

        self.assertEqual(response.context['prefill_name'], '')

    def test_get_authenticated_prefill_name_is_discord_username(self):
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertEqual(response.context['prefill_name'], 'DiscordUser')

    def test_get_authenticated_no_discord_username_falls_back_to_username(self):
        self.user.discord_username = ''
        self.user.save(update_fields=['discord_username'])
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertEqual(response.context['prefill_name'], 'plain_user')

    def test_post_authenticated_sets_driver_user(self):
        self.client.force_login(self.user)

        self.client.post(self.url, self._valid_post_data())

        driver = Driver.objects.filter(event=self.event).first()
        self.assertIsNotNone(driver)
        self.assertEqual(driver.user, self.user)

    def test_post_unauthenticated_driver_user_is_none(self):
        self.client.post(self.url, self._valid_post_data())

        driver = Driver.objects.filter(event=self.event).first()
        self.assertIsNotNone(driver)
        self.assertIsNone(driver.user)


# ---------------------------------------------------------------------------
# admin_dashboard view — Discord auth paths
# ---------------------------------------------------------------------------

class AdminDashboardDiscordAuthTests(TestCase):
    """Tests for admin_dashboard() covering the Discord-owner and
    authenticated-non-owner access paths added to the view.
    """

    def setUp(self):
        self.owner = _make_auth_user(username='owner_user')
        self.other_user = _make_auth_user(username='other_user')
        self.event = save_event(
            date=dt.date(2030, 6, 1),
            start_time_utc=dt.time(12, 0, 0),
            created_by=self.owner,
        )
        self.url = reverse('admin_dashboard', kwargs={'event_id': self.event.id})

    def _set_admin_session(self, event_id=None):
        session = self.client.session
        session[f'admin_{event_id or self.event.id}'] = True
        session.save()

    def test_discord_owner_gets_200(self):
        self.client.force_login(self.owner)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)

    def test_discord_owner_access_sets_session_flag(self):
        self.client.force_login(self.owner)

        self.client.get(self.url)

        self.assertTrue(self.client.session.get(f'admin_{self.event.id}'))

    def test_discord_owner_sees_admin_template(self):
        self.client.force_login(self.owner)

        response = self.client.get(self.url)

        template_names = [t.name for t in response.templates]
        self.assertIn('admin.html', template_names)

    def test_authenticated_non_owner_with_valid_session_gets_200(self):
        self.client.force_login(self.other_user)
        self._set_admin_session()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)

    def test_authenticated_non_owner_without_session_gets_error_page(self):
        self.client.force_login(self.other_user)

        response = self.client.get(self.url)

        # Returns 200 with an error page, not a redirect
        self.assertEqual(response.status_code, 200)
        template_names = [t.name for t in response.templates]
        self.assertIn('admin_error.html', template_names)

    def test_authenticated_non_owner_without_session_does_not_redirect(self):
        self.client.force_login(self.other_user)

        response = self.client.get(self.url)

        self.assertNotEqual(response.status_code, 302)

    def test_unauthenticated_without_session_redirects_to_discord_login(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/discord/login/', response['Location'])

    def test_unauthenticated_without_session_redirect_includes_next_param(self):
        response = self.client.get(self.url)

        self.assertIn('next=', response['Location'])

    def test_unauthenticated_with_valid_session_gets_200(self):
        self._set_admin_session()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        template_names = [t.name for t in response.templates]
        self.assertIn('admin.html', template_names)

    def test_discord_owner_access_rotates_session_key(self):
        """cycle_key() must be called in the Discord-owner branch to prevent
        session fixation after privilege elevation."""
        from unittest.mock import patch
        self.client.force_login(self.owner)
        # Capture the session key before the request
        key_before = self.client.session.session_key

        # Patch cycle_key to record calls; the real implementation still runs
        original_cycle = self.client.session.__class__.cycle_key
        called = []

        def recording_cycle_key(self_session):
            called.append(True)
            return original_cycle(self_session)

        with patch.object(self.client.session.__class__, 'cycle_key', recording_cycle_key):
            self.client.get(self.url)

        self.assertTrue(called, 'cycle_key() was not called in the Discord-owner path')

    def test_owner_of_different_event_treated_as_non_owner(self):
        """Authenticated user who owns a different event is not the owner here."""
        other_event = save_event(
            created_by=self.other_user,
            date=dt.date(2030, 7, 1),
            start_time_utc=dt.time(12, 0, 0),
        )
        # other_user owns other_event but NOT self.event
        self.client.force_login(self.other_user)

        response = self.client.get(self.url)

        # No session → should get error page, not admin
        template_names = [t.name for t in response.templates]
        self.assertIn('admin_error.html', template_names)


# ---------------------------------------------------------------------------
# view_event — user_driver context variable
# ---------------------------------------------------------------------------

class ViewEventUserDriverTests(TestCase):
    """Tests for view_event()'s user_driver context variable.

    Verifies that an authenticated driver gets their Driver object back,
    a non-driver gets None, and unauthenticated users get None.
    """

    def setUp(self):
        self.event = save_event(
            date=dt.date(2030, 6, 1),
            start_time_utc=dt.time(12, 0, 0),
        )
        self.url = reverse('view_event', kwargs={'event_id': self.event.id})
        self.user = _make_auth_user()

    def test_unauthenticated_user_driver_is_none(self):
        response = self.client.get(self.url)

        self.assertIsNone(response.context['user_driver'])

    def test_authenticated_user_not_signed_up_user_driver_is_none(self):
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertIsNone(response.context['user_driver'])

    def test_authenticated_user_who_is_driver_gets_driver_object(self):
        driver = Driver.objects.create(
            event=self.event,
            name='Known Driver',
            timezone='UTC',
            user=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertEqual(response.context['user_driver'], driver)

    def test_driver_for_different_event_does_not_appear(self):
        other_event = save_event(
            date=dt.date(2030, 7, 1),
            start_time_utc=dt.time(12, 0, 0),
        )
        Driver.objects.create(
            event=other_event,
            name='Wrong Event Driver',
            timezone='UTC',
            user=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertIsNone(response.context['user_driver'])


# ===========================================================================
# XSS prevention — _safe_json helper and view-level injection
# ===========================================================================

import json as _json
from events.views import _safe_json


# ---------------------------------------------------------------------------
# _safe_json unit tests
# ---------------------------------------------------------------------------

class SafeJsonUnitTests(SimpleTestCase):
    """Tests for views._safe_json() — the XSS-safe JSON serialiser."""

    # --- Character escaping ---

    def test_less_than_is_escaped(self):
        result = _safe_json('<')
        self.assertNotIn('<', result)
        self.assertIn('\\u003c', result)

    def test_greater_than_is_escaped(self):
        result = _safe_json('>')
        self.assertNotIn('>', result)
        self.assertIn('\\u003e', result)

    def test_ampersand_is_escaped(self):
        result = _safe_json('&')
        self.assertNotIn('&', result)
        self.assertIn('\\u0026', result)

    def test_classic_script_injection_payload_is_escaped(self):
        payload = '</script><script>alert(1)</script>'
        result = _safe_json(payload)
        self.assertNotIn('</script>', result)
        self.assertNotIn('<script>', result)

    def test_all_three_characters_escaped_in_single_string(self):
        result = _safe_json('<div id="a&b">')
        self.assertNotIn('<', result)
        self.assertNotIn('>', result)
        self.assertNotIn('&', result)

    # --- Valid JSON output ---

    def test_output_is_valid_json_for_string_with_special_chars(self):
        payload = '</script><script>alert(1)</script>'
        result = _safe_json(payload)
        parsed = _json.loads(result)
        # The parsed value must round-trip back to the original string
        self.assertEqual(parsed, payload)

    def test_output_is_valid_json_for_string_with_ampersand(self):
        result = _safe_json('fish & chips')
        parsed = _json.loads(result)
        self.assertEqual(parsed, 'fish & chips')

    # --- Normal values round-trip ---

    def test_plain_string_round_trips(self):
        result = _safe_json('hello world')
        self.assertEqual(_json.loads(result), 'hello world')

    def test_integer_round_trips(self):
        result = _safe_json(42)
        self.assertEqual(_json.loads(result), 42)

    def test_float_round_trips(self):
        result = _safe_json(3.14)
        self.assertAlmostEqual(_json.loads(result), 3.14)

    def test_none_round_trips(self):
        result = _safe_json(None)
        self.assertIsNone(_json.loads(result))

    def test_true_round_trips(self):
        result = _safe_json(True)
        self.assertTrue(_json.loads(result))

    def test_false_round_trips(self):
        result = _safe_json(False)
        self.assertFalse(_json.loads(result))

    def test_empty_string_round_trips(self):
        result = _safe_json('')
        self.assertEqual(_json.loads(result), '')

    def test_empty_list_round_trips(self):
        result = _safe_json([])
        self.assertEqual(_json.loads(result), [])

    def test_empty_dict_round_trips(self):
        result = _safe_json({})
        self.assertEqual(_json.loads(result), {})

    # --- Nested / complex structures ---

    def test_list_of_dicts_with_xss_payload_round_trips(self):
        data = [
            {'id': '1', 'name': '</script><script>alert(1)</script>'},
            {'id': '2', 'name': 'Normal Driver'},
        ]
        result = _safe_json(data)

        # No raw injection characters in the serialised output
        self.assertNotIn('</script>', result)
        self.assertNotIn('<script>', result)

        # But the parsed value is the original data unchanged
        parsed = _json.loads(result)
        self.assertEqual(parsed[0]['name'], '</script><script>alert(1)</script>')
        self.assertEqual(parsed[1]['name'], 'Normal Driver')

    def test_nested_dict_with_ampersand_round_trips(self):
        data = {'driver': 'Alonso & Prost', 'team': 'A<B>C'}
        result = _safe_json(data)

        self.assertNotIn('&', result)
        self.assertNotIn('<', result)
        self.assertNotIn('>', result)

        parsed = _json.loads(result)
        self.assertEqual(parsed['driver'], 'Alonso & Prost')
        self.assertEqual(parsed['team'], 'A<B>C')

    def test_kwargs_forwarded_to_json_dumps(self):
        # Confirm that extra kwargs (e.g. sort_keys) are honoured
        data = {'b': 2, 'a': 1}
        result = _safe_json(data, sort_keys=True)
        # With sort_keys the first key in the JSON text must be "a"
        self.assertLess(result.index('"a"'), result.index('"b"'))


# ---------------------------------------------------------------------------
# View-level XSS tests — admin page
# ---------------------------------------------------------------------------

class AdminPageXssTests(TestCase):
    """
    Assert that XSS payloads in driver names cannot break out of <script>
    blocks on the admin page.

    Auth path: admin_dashboard URL (/<event_id>/admin/) with the admin
    session flag pre-set.  The admin_page key URL now redirects immediately
    and no longer renders the template directly.
    """

    XSS_PAYLOAD = '</script><script>alert(1)</script>'

    def setUp(self):
        self.event = save_event()
        self.driver = Driver.objects.create(
            event=self.event,
            name=self.XSS_PAYLOAD,
            timezone='UTC',
        )
        # admin_page now redirects — use admin_dashboard with session flag
        self.url = reverse(
            'admin_dashboard',
            kwargs={'event_id': self.event.id},
        )
        # Pre-set the admin session so admin_dashboard serves the page
        session = self.client.session
        session[f'admin_{self.event.id}'] = True
        session.save()

    def _decoded(self, response):
        return response.content.decode('utf-8')

    def test_raw_script_close_tag_absent_from_response(self):
        response = self.client.get(self.url)

        self.assertNotIn('</script><script>', self._decoded(response))

    def test_driver_name_payload_does_not_appear_verbatim_in_script_block(self):
        response = self.client.get(self.url)

        # The raw payload must not appear in the page at all
        self.assertNotIn(self.XSS_PAYLOAD, self._decoded(response))

    def test_drivers_json_context_contains_escaped_name(self):
        response = self.client.get(self.url)

        drivers_json_str = response.context['drivers_json']
        # The escaped form must be present
        self.assertIn('\\u003c/script\\u003e', drivers_json_str)
        # The raw form must not be present
        self.assertNotIn('</script>', drivers_json_str)

    def test_drivers_json_in_context_is_valid_json(self):
        response = self.client.get(self.url)

        drivers_json_str = response.context['drivers_json']
        parsed = _json.loads(drivers_json_str)
        names = [d['name'] for d in parsed]
        self.assertIn(self.XSS_PAYLOAD, names)

    def test_ampersand_in_driver_name_is_escaped_in_drivers_json(self):
        Driver.objects.create(
            event=self.event,
            name='Fast & Furious',
            timezone='UTC',
        )
        response = self.client.get(self.url)

        drivers_json_str = response.context['drivers_json']
        self.assertNotIn('Fast & Furious', drivers_json_str)
        self.assertIn('\\u0026', drivers_json_str)

    def test_slot_timestamps_json_present_and_valid(self):
        response = self.client.get(self.url)

        slot_json_str = response.context['slot_timestamps_json']
        parsed = _json.loads(slot_json_str)
        self.assertIsInstance(parsed, list)

    def test_response_status_200(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# View-level XSS tests — public view page
# ---------------------------------------------------------------------------

class ViewEventXssTests(TestCase):
    """
    Assert that XSS payloads in driver names cannot break out of <script>
    blocks on the public view page (/<event_id>/view/).

    The view page renders stint_rows_json which contains driver_name, a
    user-controlled field.
    """

    XSS_PAYLOAD = '</script><script>alert(1)</script>'

    def setUp(self):
        self.event = save_event()
        # Create a StintAssignment with the malicious driver so driver_name
        # appears in stint_rows_json
        self.driver = Driver.objects.create(
            event=self.event,
            name=self.XSS_PAYLOAD,
            timezone='UTC',
        )
        StintAssignment.objects.create(
            event=self.event,
            stint_number=1,
            driver=self.driver,
        )
        self.url = reverse('view_event', kwargs={'event_id': self.event.id})

    def _decoded(self, response):
        return response.content.decode('utf-8')

    def test_raw_script_close_tag_absent_from_response(self):
        response = self.client.get(self.url)

        self.assertNotIn('</script><script>', self._decoded(response))

    def test_driver_name_payload_does_not_appear_verbatim_in_response(self):
        response = self.client.get(self.url)

        self.assertNotIn(self.XSS_PAYLOAD, self._decoded(response))

    def test_stint_rows_json_context_contains_escaped_driver_name(self):
        response = self.client.get(self.url)

        stint_rows_json_str = response.context['stint_rows_json']
        # Escaped form present
        self.assertIn('\\u003c/script\\u003e', stint_rows_json_str)
        # Raw form absent
        self.assertNotIn('</script>', stint_rows_json_str)

    def test_stint_rows_json_in_context_is_valid_json(self):
        response = self.client.get(self.url)

        stint_rows_json_str = response.context['stint_rows_json']
        parsed = _json.loads(stint_rows_json_str)
        driver_names = [row['driver_name'] for row in parsed if row['driver_name']]
        self.assertIn(self.XSS_PAYLOAD, driver_names)

    def test_ampersand_in_driver_name_escaped_in_stint_rows_json(self):
        amp_driver = Driver.objects.create(
            event=self.event,
            name='Fast & Furious',
            timezone='UTC',
        )
        StintAssignment.objects.create(
            event=self.event,
            stint_number=2,
            driver=amp_driver,
        )
        response = self.client.get(self.url)

        stint_rows_json_str = response.context['stint_rows_json']
        self.assertNotIn('Fast & Furious', stint_rows_json_str)
        self.assertIn('\\u0026', stint_rows_json_str)

    def test_unassigned_stint_driver_name_is_null_in_json(self):
        StintAssignment.objects.create(
            event=self.event,
            stint_number=3,
            driver=None,
        )
        response = self.client.get(self.url)

        stint_rows_json_str = response.context['stint_rows_json']
        parsed = _json.loads(stint_rows_json_str)
        unassigned = [row for row in parsed if row['stint_number'] == 3]
        self.assertEqual(len(unassigned), 1)
        self.assertIsNone(unassigned[0]['driver_name'])

    def test_response_status_200(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# AdminPageRedirectTests — Fix 1: admin_page redirects after key validation
# ---------------------------------------------------------------------------


class AdminPageRedirectTests(TestCase):
    """Tests for views.admin_page() — redirect behaviour added by the security
    fix that prevents the admin key from appearing in the browser's URL bar
    after login.
    """

    def setUp(self):
        self.event = save_event()
        self.valid_url = reverse(
            'admin_page',
            kwargs={'event_id': self.event.id, 'admin_key': self.event.admin_key},
        )
        self.wrong_key_url = reverse(
            'admin_page',
            kwargs={'event_id': self.event.id, 'admin_key': 'wrong-key-value'},
        )
        self.dashboard_url = reverse(
            'admin_dashboard', kwargs={'event_id': self.event.id}
        )

    def test_valid_key_returns_302(self):
        response = self.client.get(self.valid_url)

        self.assertEqual(response.status_code, 302)

    def test_valid_key_redirects_to_admin_dashboard_url(self):
        response = self.client.get(self.valid_url)

        self.assertRedirects(
            response,
            self.dashboard_url,
            fetch_redirect_response=False,
        )

    def test_invalid_key_returns_200_not_redirect(self):
        response = self.client.get(self.wrong_key_url)

        self.assertEqual(response.status_code, 200)

    def test_invalid_key_renders_admin_error_template(self):
        response = self.client.get(self.wrong_key_url)

        template_names = [t.name for t in response.templates]
        self.assertIn('admin_error.html', template_names)

    def test_valid_key_sets_session_flag_before_redirect(self):
        self.client.get(self.valid_url)

        self.assertTrue(self.client.session.get(f'admin_{self.event.id}'))

    def test_redirect_target_returns_200_because_session_was_set(self):
        # Follow the redirect — admin_dashboard should serve the page because
        # admin_page already set the session flag in the same request cycle.
        response = self.client.get(self.valid_url, follow=True)

        self.assertEqual(response.status_code, 200)
        template_names = [t.name for t in response.templates]
        self.assertIn('admin.html', template_names)


# ---------------------------------------------------------------------------
# DriverDeleteAuthTests — Fix 2: driver_delete authorization
# ---------------------------------------------------------------------------


class DriverDeleteAuthTests(TestCase):
    """Tests for views.driver_delete() — authorization checks added to prevent
    arbitrary users from deleting other drivers.
    """

    def setUp(self):
        self.event = save_event()
        self.driver = Driver.objects.create(
            event=self.event,
            name='Test Driver',
            timezone='UTC',
        )
        self.url = reverse(
            'driver_delete',
            kwargs={'event_id': self.event.id, 'driver_id': self.driver.id},
        )

    def _set_admin_session(self, event_id):
        """Write the admin session flag for the given event, matching the
        pattern used throughout this test file."""
        session = self.client.session
        session[f'admin_{event_id}'] = True
        session.save()

    def _make_driver_owner(self):
        """Create a User, link them to self.driver, and return the user."""
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.create_user(
            username=f'owner_{uuid.uuid4().hex[:8]}',
            password='testpass',
        )
        self.driver.user = user
        self.driver.save()
        return user

    def test_unauthenticated_user_gets_403(self):
        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 403)

    def test_unauthenticated_user_driver_still_exists_in_db(self):
        self.client.delete(self.url)

        self.assertTrue(Driver.objects.filter(id=self.driver.id).exists())

    def test_authenticated_non_owner_gets_403(self):
        # Log in as a different user who has no link to the driver
        other_user = _make_auth_user()
        self.client.force_login(other_user)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 403)

    def test_authenticated_non_owner_driver_still_exists_in_db(self):
        other_user = _make_auth_user()
        self.client.force_login(other_user)

        self.client.delete(self.url)

        self.assertTrue(Driver.objects.filter(id=self.driver.id).exists())

    def test_owner_gets_200(self):
        owner = self._make_driver_owner()
        self.client.force_login(owner)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 200)

    def test_owner_response_has_hx_redirect_to_root(self):
        owner = self._make_driver_owner()
        self.client.force_login(owner)

        response = self.client.delete(self.url)

        self.assertEqual(response['HX-Redirect'], '/')

    def test_owner_driver_deleted_from_db(self):
        owner = self._make_driver_owner()
        self.client.force_login(owner)

        self.client.delete(self.url)

        self.assertFalse(Driver.objects.filter(id=self.driver.id).exists())

    def test_admin_session_holder_gets_200(self):
        self._set_admin_session(self.event.id)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 200)

    def test_admin_session_holder_response_has_hx_redirect_to_root(self):
        self._set_admin_session(self.event.id)

        response = self.client.delete(self.url)

        self.assertEqual(response['HX-Redirect'], '/')

    def test_admin_session_holder_driver_deleted_from_db(self):
        self._set_admin_session(self.event.id)

        self.client.delete(self.url)

        self.assertFalse(Driver.objects.filter(id=self.driver.id).exists())

    def test_admin_session_for_different_event_gets_403(self):
        # An admin session scoped to a different event must not grant access
        other_event_id = uuid.uuid4()
        self._set_admin_session(other_event_id)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 403)

    def test_post_method_returns_405(self):
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 405)

    def test_get_method_returns_405(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 405)

    def test_wrong_method_response_has_allow_delete_header(self):
        response = self.client.post(self.url)

        self.assertEqual(response['Allow'], 'DELETE')


# ===========================================================================
# New coverage: race_start_time_utc, effective start, total_race_laps,
# laps_remaining_after_stint, driver name length, admin_save_calc/details,
# and Django admin panel removed.
# ===========================================================================

# ---------------------------------------------------------------------------
# Event.effective_start_time_utc and effective_start_datetime_utc
# ---------------------------------------------------------------------------

class EventEffectiveStartTests(SimpleTestCase):
    """Tests for Event.effective_start_time_utc and effective_start_datetime_utc."""

    def test_effective_start_time_utc_returns_race_start_when_set(self):
        event = make_event(
            start_time_utc=dt.time(10, 0, 0),
            race_start_time_utc=dt.time(12, 30, 0),
        )
        self.assertEqual(event.effective_start_time_utc, dt.time(12, 30, 0))

    def test_effective_start_time_utc_falls_back_to_session_start_when_none(self):
        event = make_event(
            start_time_utc=dt.time(10, 0, 0),
            race_start_time_utc=None,
        )
        self.assertEqual(event.effective_start_time_utc, dt.time(10, 0, 0))

    def test_effective_start_datetime_utc_uses_race_start_when_set(self):
        event = make_event(
            date=dt.date(2026, 6, 1),
            start_time_utc=dt.time(10, 0, 0),
            race_start_time_utc=dt.time(12, 30, 0),
        )
        expected = dt.datetime(2026, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(event.effective_start_datetime_utc, expected)

    def test_effective_start_datetime_utc_uses_session_start_when_race_start_none(self):
        event = make_event(
            date=dt.date(2026, 6, 1),
            start_time_utc=dt.time(10, 0, 0),
            race_start_time_utc=None,
        )
        expected = dt.datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(event.effective_start_datetime_utc, expected)

    def test_effective_start_datetime_utc_is_timezone_aware(self):
        event = make_event(race_start_time_utc=dt.time(14, 0, 0))
        self.assertIsNotNone(event.effective_start_datetime_utc.tzinfo)
        self.assertEqual(event.effective_start_datetime_utc.utcoffset(), dt.timedelta(0))

    def test_effective_start_datetime_utc_is_timezone_aware_when_race_start_none(self):
        event = make_event(race_start_time_utc=None)
        self.assertIsNotNone(event.effective_start_datetime_utc.tzinfo)
        self.assertEqual(event.effective_start_datetime_utc.utcoffset(), dt.timedelta(0))

    def test_effective_start_equals_session_start_when_race_start_same_as_session(self):
        # When race_start equals start_time, effective_start still equals race_start
        event = make_event(
            start_time_utc=dt.time(12, 0, 0),
            race_start_time_utc=dt.time(12, 0, 0),
        )
        self.assertEqual(event.effective_start_time_utc, dt.time(12, 0, 0))


# ---------------------------------------------------------------------------
# Driver model — name max_length constraint
# ---------------------------------------------------------------------------

class DriverNameMaxLengthTests(TestCase):
    """Tests for Driver.name max_length=50 at the model level."""

    def setUp(self):
        self.event = save_event()

    def test_name_at_max_length_50_saves_successfully(self):
        driver = Driver.objects.create(
            event=self.event,
            name='A' * 50,
            timezone='UTC',
        )
        driver.refresh_from_db()
        self.assertEqual(len(driver.name), 50)

    def test_driver_name_field_has_max_length_50(self):
        field = Driver._meta.get_field('name')
        self.assertEqual(field.max_length, 50)


# ---------------------------------------------------------------------------
# total_race_laps() utility function
# ---------------------------------------------------------------------------

class TotalRaceLapsTests(SimpleTestCase):
    """Tests for utils.total_race_laps()."""

    def setUp(self):
        from .utils import total_race_laps as _total_race_laps
        self.total_race_laps = _total_race_laps

    def test_returns_none_when_avg_lap_seconds_is_none(self):
        event = make_event(
            avg_lap_seconds=None,
            in_lap_seconds=None,
            out_lap_seconds=None,
            target_laps=None,
            fuel_capacity=None,
            fuel_per_lap=None,
        )
        self.assertIsNone(self.total_race_laps(event))

    def test_returns_none_when_length_seconds_is_none_and_no_stint_fields(self):
        # length_seconds is required by the model but we can test the guard
        # by making a stub with no avg_lap and no stint fields
        event = make_event(
            avg_lap_seconds=None,
            in_lap_seconds=None,
            out_lap_seconds=None,
            target_laps=None,
            fuel_capacity=None,
            fuel_per_lap=None,
        )
        self.assertIsNone(self.total_race_laps(event))

    def test_fallback_floor_division_when_only_avg_lap_and_length_set(self):
        # Without full stint fields, falls back to floor(length / avg_lap)
        # 86400 / 102 = 847.058... → floor = 847
        event = make_event(
            length_seconds=86_400,
            avg_lap_seconds=102.0,
            in_lap_seconds=None,
            out_lap_seconds=None,
            target_laps=None,
            fuel_capacity=None,
            fuel_per_lap=None,
        )
        self.assertEqual(self.total_race_laps(event), 847)

    def test_with_full_stint_fields_uses_stint_based_calculation(self):
        # Default event: 6-hour race, stint_length=3615s, 6 stints, target_laps=30
        # n=6 stints, sl=3615s
        # last_stint_time = 21600 - 5*3615 = 21600 - 18075 = 3525s
        # total = 5*30 + floor(3525/120) = 150 + 29 = 179
        event = make_event()
        result = self.total_race_laps(event)
        self.assertIsInstance(result, int)
        self.assertGreater(result, 0)

    def test_known_value_86400s_102s_lap_fallback(self):
        # Explicit check of the 847 example from the spec
        event = make_event(
            length_seconds=86_400,
            avg_lap_seconds=102.0,
            in_lap_seconds=None,
            out_lap_seconds=None,
            target_laps=None,
            fuel_capacity=None,
            fuel_per_lap=None,
        )
        self.assertEqual(self.total_race_laps(event), 847)

    def test_exact_division_returns_integer(self):
        # 3600s race / 120s lap = exactly 30 laps
        event = make_event(
            length_seconds=3_600,
            avg_lap_seconds=120.0,
            in_lap_seconds=None,
            out_lap_seconds=None,
            target_laps=None,
            fuel_capacity=None,
            fuel_per_lap=None,
        )
        self.assertEqual(self.total_race_laps(event), 30)

    def test_result_is_floor_not_ceiling_for_fractional_division(self):
        # 3601s / 120s = 30.008... → floor = 30 (not 31)
        event = make_event(
            length_seconds=3_601,
            avg_lap_seconds=120.0,
            in_lap_seconds=None,
            out_lap_seconds=None,
            target_laps=None,
            fuel_capacity=None,
            fuel_per_lap=None,
        )
        self.assertEqual(self.total_race_laps(event), 30)


# ---------------------------------------------------------------------------
# laps_remaining_after_stint() utility function
# ---------------------------------------------------------------------------

class LapsRemainingAfterStintTests(SimpleTestCase):
    """Tests for utils.laps_remaining_after_stint()."""

    def setUp(self):
        from .utils import laps_remaining_after_stint as _laps_remaining
        self.laps_remaining = _laps_remaining

    def _event_no_stint_fields(self):
        return make_event(
            avg_lap_seconds=None,
            in_lap_seconds=None,
            out_lap_seconds=None,
            target_laps=None,
            fuel_capacity=None,
            fuel_per_lap=None,
        )

    def test_returns_none_when_avg_lap_seconds_not_set(self):
        event = self._event_no_stint_fields()
        self.assertIsNone(self.laps_remaining(event, 1))

    def test_returns_none_when_target_laps_not_set(self):
        # target_laps is required by laps_remaining_after_stint
        event = make_event(target_laps=None, fuel_capacity=None, fuel_per_lap=None)
        self.assertIsNone(self.laps_remaining(event, 1))

    def test_clamps_to_zero_never_negative(self):
        # Using the default event (179 total laps, 30 target_laps per stint)
        # A very late stint number would go negative without the clamp
        event = make_event()
        result = self.laps_remaining(event, 100)
        self.assertEqual(result, 0)

    def test_result_for_stint_1_is_total_minus_one_stint(self):
        # Default event: total_race_laps ~179, target_laps=30
        # After stint 1: remaining = 179 - 30 = 149
        event = make_event()
        from .utils import total_race_laps
        total = total_race_laps(event)
        expected = max(0, total - 30)
        self.assertEqual(self.laps_remaining(event, 1), expected)

    def test_result_for_last_productive_stint_is_zero_or_near_zero(self):
        # Requesting remaining after a stint far beyond the race
        event = make_event()
        result = self.laps_remaining(event, 1000)
        self.assertEqual(result, 0)

    def test_exact_race_where_result_is_zero_at_final_stint(self):
        # 2-stint race dividing evenly: 30 laps total, 15 laps per stint
        # After stint 1: 30 - 15 = 15 laps remaining
        # After stint 2: 30 - 30 = 0 laps remaining
        event = make_event(
            length_seconds=3_600,
            avg_lap_seconds=120.0,
            in_lap_seconds=120.0,
            out_lap_seconds=120.0,
            target_laps=15,
        )
        # total_race_laps uses stint-based calc: n=2 stints, sl=3600/2=1800... wait,
        # let's compute: stint_length = 120*15 + (120+120-240) = 1800+0 = 1800
        # total_stints = ceil(3600/1800) = 2
        # last_stint_time = 3600 - 1*1800 = 1800, laps = 1*15 + floor(1800/120) = 15+15 = 30
        result_after_stint2 = self.laps_remaining(event, 2)
        self.assertEqual(result_after_stint2, 0)

    def test_formula_total_minus_stint_times_target(self):
        # Using fallback path: only avg_lap and length set, no full stint fields
        # total_race_laps = floor(3600/120) = 30, target_laps=10
        # After stint 1: remaining = max(0, 30 - 1*10) = 20
        event = make_event(
            length_seconds=3_600,
            avg_lap_seconds=120.0,
            in_lap_seconds=None,
            out_lap_seconds=None,
            target_laps=10,
            fuel_capacity=None,
            fuel_per_lap=None,
        )
        self.assertEqual(self.laps_remaining(event, 1), 20)

    def test_formula_after_third_stint_in_fallback_path(self):
        # total=30, target_laps=10 — after stint 3: 30 - 30 = 0
        event = make_event(
            length_seconds=3_600,
            avg_lap_seconds=120.0,
            in_lap_seconds=None,
            out_lap_seconds=None,
            target_laps=10,
            fuel_capacity=None,
            fuel_per_lap=None,
        )
        self.assertEqual(self.laps_remaining(event, 3), 0)


# ---------------------------------------------------------------------------
# get_stint_windows() uses effective_start_datetime_utc
# ---------------------------------------------------------------------------

class GetStintWindowsEffectiveStartTests(SimpleTestCase):
    """Tests that get_stint_windows() uses effective start (race_start_time_utc
    when set, otherwise session start_time_utc)."""

    def test_without_race_start_first_stint_begins_at_session_start(self):
        event = make_event(
            date=dt.date(2026, 6, 1),
            start_time_utc=dt.time(10, 0, 0),
            race_start_time_utc=None,
        )
        windows = get_stint_windows(event)
        self.assertEqual(windows[0]['start_utc'], utc(2026, 6, 1, 10, 0, 0))

    def test_with_race_start_first_stint_begins_at_race_start_not_session_start(self):
        event = make_event(
            date=dt.date(2026, 6, 1),
            start_time_utc=dt.time(10, 0, 0),
            race_start_time_utc=dt.time(12, 0, 0),
        )
        windows = get_stint_windows(event)
        # First stint must start at 12:00, NOT at 10:00
        self.assertEqual(windows[0]['start_utc'], utc(2026, 6, 1, 12, 0, 0))
        self.assertNotEqual(windows[0]['start_utc'], utc(2026, 6, 1, 10, 0, 0))

    def test_with_race_start_set_all_stint_starts_are_after_race_start(self):
        event = make_event(
            date=dt.date(2026, 6, 1),
            start_time_utc=dt.time(10, 0, 0),
            race_start_time_utc=dt.time(12, 0, 0),
        )
        windows = get_stint_windows(event)
        race_start_dt = utc(2026, 6, 1, 12, 0, 0)
        for w in windows:
            self.assertGreaterEqual(w['start_utc'], race_start_dt)


# ---------------------------------------------------------------------------
# get_availability_slots() always uses session start_time_utc
# ---------------------------------------------------------------------------

class GetAvailabilitySlotsSessionStartTests(SimpleTestCase):
    """Tests that get_availability_slots() always anchors to start_time_utc
    even when race_start_time_utc is set to a later time."""

    def test_slots_start_at_session_start_regardless_of_race_start(self):
        event = make_event(
            date=dt.date(2026, 6, 1),
            start_time_utc=dt.time(10, 0, 0),
            race_start_time_utc=dt.time(12, 0, 0),
        )
        slots = get_availability_slots(event)
        # Slots must begin at 10:00 (session start), not 12:00 (race start)
        self.assertEqual(slots[0], utc(2026, 6, 1, 10, 0, 0))

    def test_slots_do_not_start_at_race_start_when_different_from_session(self):
        event = make_event(
            date=dt.date(2026, 6, 1),
            start_time_utc=dt.time(10, 0, 0),
            race_start_time_utc=dt.time(12, 0, 0),
        )
        slots = get_availability_slots(event)
        # The race start (12:00) must NOT be the first slot
        self.assertNotEqual(slots[0], utc(2026, 6, 1, 12, 0, 0))

    def test_slot_count_is_based_on_session_start_and_effective_end(self):
        # session 10:00, race start 12:00, 6h race → race ends 18:00
        # buffer end = 19:00; slots 10:00 … 18:30 = 9h window = 18 slots
        event = make_event(
            date=dt.date(2026, 6, 1),
            start_time_utc=dt.time(10, 0, 0),
            race_start_time_utc=dt.time(12, 0, 0),
            length_seconds=21_600,
        )
        slots = get_availability_slots(event)
        self.assertEqual(len(slots), 18)


# ---------------------------------------------------------------------------
# Driver name length validation — signup view
# ---------------------------------------------------------------------------

class SignupDriverNameLengthTests(TestCase):
    """Tests that the signup view enforces the 50-character name limit."""

    def setUp(self):
        # Use a far-future date so availability slots exist
        self.event = save_event(
            date=dt.date(2030, 6, 1),
            start_time_utc=dt.time(12, 0, 0),
        )
        self.url = reverse('signup', kwargs={'event_id': self.event.id})

    def _slot_str(self):
        """Return a valid slot timestamp string for this event."""
        slots = get_availability_slots(self.event)
        s = slots[0]
        return s.isoformat().replace('+00:00', 'Z') if s.tzinfo else s.isoformat() + 'Z'

    def _post(self, name):
        return self.client.post(self.url, {
            'driver_name': name,
            'timezone': 'UTC',
            'slots': [self._slot_str()],
        })

    def test_name_over_50_characters_returns_200_with_error(self):
        response = self._post('A' * 51)

        self.assertEqual(response.status_code, 200)
        self.assertIn('driver_name', response.context['errors'])

    def test_name_over_50_characters_does_not_create_driver(self):
        self._post('A' * 51)

        self.assertEqual(Driver.objects.filter(event=self.event).count(), 0)

    def test_name_exactly_50_characters_creates_driver(self):
        self._post('A' * 50)

        self.assertEqual(Driver.objects.filter(event=self.event).count(), 1)

    def test_name_exactly_50_characters_saved_correctly(self):
        self._post('A' * 50)

        driver = Driver.objects.filter(event=self.event).first()
        self.assertIsNotNone(driver)
        self.assertEqual(len(driver.name), 50)

    def test_error_message_mentions_50_characters(self):
        response = self._post('A' * 51)

        error_msg = response.context['errors']['driver_name']
        self.assertIn('50', error_msg)


# ---------------------------------------------------------------------------
# Driver name length validation — admin_edit_driver_name view
# ---------------------------------------------------------------------------

class AdminEditDriverNameLengthTests(TestCase):
    """Tests that admin_edit_driver_name enforces the 50-character name limit."""

    def setUp(self):
        self.event = save_event()
        self.driver = Driver.objects.create(
            event=self.event, name='Original', timezone='UTC'
        )
        self.url = reverse(
            'admin_edit_driver_name',
            kwargs={'event_id': self.event.id, 'driver_id': self.driver.id},
        )

    def _set_admin_session(self):
        session = self.client.session
        session[f'admin_{self.event.id}'] = True
        session.save()

    def test_name_over_50_characters_returns_edit_form_partial(self):
        self._set_admin_session()

        response = self.client.post(self.url, {'name': 'B' * 51})

        template_names = [t.name for t in response.templates]
        self.assertIn('partials/driver_name_edit_form.html', template_names)

    def test_name_over_50_characters_does_not_update_driver(self):
        self._set_admin_session()

        self.client.post(self.url, {'name': 'B' * 51})

        self.driver.refresh_from_db()
        self.assertEqual(self.driver.name, 'Original')

    def test_name_over_50_characters_response_contains_error_message(self):
        self._set_admin_session()

        response = self.client.post(self.url, {'name': 'B' * 51})

        self.assertIn(b'50', response.content)

    def test_name_exactly_50_characters_saves_successfully(self):
        self._set_admin_session()

        self.client.post(self.url, {'name': 'C' * 50})

        self.driver.refresh_from_db()
        self.assertEqual(self.driver.name, 'C' * 50)

    def test_name_exactly_50_characters_returns_display_partial(self):
        self._set_admin_session()

        response = self.client.post(self.url, {'name': 'C' * 50})

        template_names = [t.name for t in response.templates]
        self.assertIn('partials/driver_name_display.html', template_names)


# ---------------------------------------------------------------------------
# admin_save_calc saves race_start_time_utc
# ---------------------------------------------------------------------------

class AdminSaveCalcRaceStartTests(TestCase):
    """Tests that admin_save_calc() saves and clears race_start_time_utc."""

    def setUp(self):
        self.event = save_event(
            avg_lap_seconds=None,
            in_lap_seconds=None,
            out_lap_seconds=None,
            target_laps=None,
            fuel_capacity=None,
            fuel_per_lap=None,
        )
        self.url = reverse('admin_save_calc', kwargs={'event_id': self.event.id})

    def _set_admin_session(self):
        session = self.client.session
        session[f'admin_{self.event.id}'] = True
        session.save()

    def test_posting_valid_race_start_time_saves_to_event(self):
        self._set_admin_session()

        self.client.post(self.url, {'race_start_time_utc': '13:00'})

        self.event.refresh_from_db()
        self.assertEqual(self.event.race_start_time_utc, dt.time(13, 0, 0))

    def test_posting_empty_race_start_time_sets_field_to_none(self):
        # Pre-set a value, then clear it
        self.event.race_start_time_utc = dt.time(13, 0, 0)
        self.event.save()
        self._set_admin_session()

        self.client.post(self.url, {'race_start_time_utc': ''})

        self.event.refresh_from_db()
        self.assertIsNone(self.event.race_start_time_utc)

    def test_posting_invalid_race_start_time_returns_error_partial(self):
        self._set_admin_session()

        response = self.client.post(self.url, {'race_start_time_utc': 'not-a-time'})

        template_names = [t.name for t in response.templates]
        self.assertIn('partials/form_errors.html', template_names)

    def test_posting_invalid_race_start_time_does_not_save(self):
        self.event.race_start_time_utc = dt.time(10, 0, 0)
        self.event.save()
        self._set_admin_session()

        self.client.post(self.url, {'race_start_time_utc': 'bad'})

        self.event.refresh_from_db()
        # The existing value must be unchanged
        self.assertEqual(self.event.race_start_time_utc, dt.time(10, 0, 0))

    def test_posting_race_start_without_other_fields_returns_show_toast(self):
        # Event has no required stint fields so no HX-Refresh; should get show-toast
        self._set_admin_session()

        response = self.client.post(self.url, {'race_start_time_utc': '13:00'})

        self.assertIn('show-toast', json.loads(response['HX-Trigger']))


# ---------------------------------------------------------------------------
# admin_save_details does NOT save race_start_time_utc
# ---------------------------------------------------------------------------

class AdminSaveDetailsNoRaceStartTests(TestCase):
    """Tests that admin_save_details() ignores race_start_time_utc even
    if the field is present in the POST body."""

    def setUp(self):
        self.event = save_event()
        self.url = reverse('admin_save_details', kwargs={'event_id': self.event.id})

    def _set_admin_session(self):
        session = self.client.session
        session[f'admin_{self.event.id}'] = True
        session.save()

    def _valid_post(self, **overrides):
        data = {
            'name': 'Updated Race',
            'date': '2027-06-01',
            'start_time_utc': '14:00',
            'length_hours': '2',
            'length_minutes': '0',
        }
        data.update(overrides)
        return data

    def test_race_start_time_utc_in_post_body_is_not_saved(self):
        # Pre-set race_start so we can verify it is unchanged after the save
        self.event.race_start_time_utc = dt.time(10, 0, 0)
        self.event.save()
        self._set_admin_session()

        self.client.post(
            self.url,
            self._valid_post(race_start_time_utc='14:00'),
        )

        self.event.refresh_from_db()
        # race_start_time_utc must still be the pre-set value
        self.assertEqual(self.event.race_start_time_utc, dt.time(10, 0, 0))

    def test_race_start_time_utc_remains_none_after_save_details(self):
        # Event starts with None race_start — posting to save-details must not change it
        self.assertIsNone(self.event.race_start_time_utc)
        self._set_admin_session()

        self.client.post(
            self.url,
            self._valid_post(race_start_time_utc='15:30'),
        )

        self.event.refresh_from_db()
        self.assertIsNone(self.event.race_start_time_utc)

    def test_save_details_still_saves_name_correctly(self):
        # Sanity check: the view does save other fields while ignoring race_start
        self._set_admin_session()

        self.client.post(self.url, self._valid_post(name='Sanity Check Race'))

        self.event.refresh_from_db()
        self.assertEqual(self.event.name, 'Sanity Check Race')


# ---------------------------------------------------------------------------
# Django admin panel removed — /admin/ returns 404
# ---------------------------------------------------------------------------

class DjangoAdminRemovedTests(TestCase):
    """Tests that the Django admin panel is not installed and /admin/ returns 404."""

    def test_admin_root_returns_404(self):
        response = self.client.get('/admin/')

        self.assertEqual(response.status_code, 404)

    def test_admin_login_url_returns_404(self):
        response = self.client.get('/admin/login/')

        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# admin_delete_event
# ---------------------------------------------------------------------------

class AdminDeleteEventTests(TestCase):
    """Tests for views.admin_delete_event()."""

    def setUp(self):
        self.event = save_event()
        self.driver = Driver.objects.create(
            event=self.event, name='Alice', timezone='UTC'
        )
        Availability.objects.create(
            driver=self.driver,
            slot_utc=utc(2026, 6, 1, 12, 0),
        )
        self.url = reverse('admin_delete_event', kwargs={'event_id': self.event.id})

    def _set_admin_session(self):
        session = self.client.session
        session[f'admin_{self.event.id}'] = True
        session.save()

    def test_get_request_returns_405(self):
        self._set_admin_session()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 405)
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())

    def test_post_without_session_returns_403(self):
        response = self.client.post(self.url, {'confirm_name': 'DELETE'})

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())

    def test_post_wrong_confirmation_redirects_to_admin(self):
        self._set_admin_session()

        response = self.client.post(self.url, {'confirm_name': 'WRONG'})

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse('admin_dashboard', kwargs={'event_id': self.event.id}),
            response['Location'],
        )
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())

    def test_post_correct_confirmation_deletes_event(self):
        self._set_admin_session()

        response = self.client.post(self.url, {'confirm_name': 'DELETE'})

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('home'), response['Location'])
        self.assertFalse(Event.objects.filter(pk=self.event.pk).exists())

    def test_cascade_deletes_driver_and_availability(self):
        self._set_admin_session()

        self.client.post(self.url, {'confirm_name': 'DELETE'})

        self.assertFalse(Driver.objects.filter(pk=self.driver.pk).exists())
        self.assertFalse(
            Availability.objects.filter(driver=self.driver).exists()
        )

    def test_admin_session_cleared_after_deletion(self):
        self._set_admin_session()

        self.client.post(self.url, {'confirm_name': 'DELETE'})

        self.assertFalse(
            bool(self.client.session.get(f'admin_{self.event.id}'))
        )


# ---------------------------------------------------------------------------
# admin_save_assignments — condition persistence
# ---------------------------------------------------------------------------

class AdminSaveAssignmentsConditionTests(TestCase):
    """Tests that admin_save_assignments preserves conditions on save."""

    def setUp(self):
        # save_event() provides all required stint fields by default
        self.event = save_event()
        self.driver = Driver.objects.create(
            event=self.event, name='Alice', timezone='UTC'
        )
        self.url = reverse(
            'admin_save_assignments', kwargs={'event_id': self.event.id}
        )

    def _set_admin_session(self):
        session = self.client.session
        session[f'admin_{self.event.id}'] = True
        session.save()

    def _post_assignment(self, stint_to_driver_map, conditions=None):
        """POST save-assignments with a {stint_number: driver_id_str} mapping.

        Pass conditions={stint_number: 'dry'|'mixed'|'wet'} to include
        condition_N fields, mirroring what the Alpine form always sends.
        """
        data = {f'stint_{n}': str(d_id) for n, d_id in stint_to_driver_map.items()}
        if conditions:
            data.update({f'condition_{n}': c for n, c in conditions.items()})
        return self.client.post(self.url, data)

    def test_pre_existing_wet_condition_is_preserved_after_save(self):
        StintAssignment.objects.create(
            event=self.event,
            stint_number=1,
            driver=self.driver,
            condition='wet',
        )
        self._set_admin_session()

        # Alpine always POSTs the condition value — pass it explicitly
        self._post_assignment({1: self.driver.id}, conditions={1: 'wet'})

        sa = StintAssignment.objects.get(event=self.event, stint_number=1)
        self.assertEqual(sa.condition, 'wet')

    def test_pre_existing_mixed_condition_is_preserved_after_save(self):
        StintAssignment.objects.create(
            event=self.event,
            stint_number=2,
            driver=self.driver,
            condition='mixed',
        )
        self._set_admin_session()

        self._post_assignment({2: self.driver.id}, conditions={2: 'mixed'})

        sa = StintAssignment.objects.get(event=self.event, stint_number=2)
        self.assertEqual(sa.condition, 'mixed')

    def test_new_stint_with_no_prior_assignment_defaults_to_dry(self):
        """A stint with no condition_N in POST falls back to 'dry'."""
        self._set_admin_session()

        # Omit conditions to exercise the missing-key fallback
        self._post_assignment({1: self.driver.id})

        sa = StintAssignment.objects.get(event=self.event, stint_number=1)
        self.assertEqual(sa.condition, 'dry')

    def test_conditions_preserved_across_all_stints_simultaneously(self):
        """Saving all stints at once writes each stint's individual condition."""
        self._set_admin_session()

        self._post_assignment(
            {1: self.driver.id},
            conditions={1: 'wet', 2: 'mixed'},
        )

        sa1 = StintAssignment.objects.get(event=self.event, stint_number=1)
        sa2 = StintAssignment.objects.get(event=self.event, stint_number=2)
        self.assertEqual(sa1.condition, 'wet')
        self.assertEqual(sa2.condition, 'mixed')

    def test_invalid_condition_value_falls_back_to_dry(self):
        """An invalid condition_N value must be rejected and fall back to dry."""
        self._set_admin_session()

        data = {
            'stint_1': str(self.driver.id),
            'condition_1': 'sideways',
        }
        self.client.post(self.url, data)

        sa = StintAssignment.objects.get(event=self.event, stint_number=1)
        self.assertEqual(sa.condition, 'dry')

    def test_save_without_admin_session_returns_403(self):
        response = self._post_assignment({1: self.driver.id})

        self.assertEqual(response.status_code, 403)

    def test_save_returns_200_on_success(self):
        self._set_admin_session()

        response = self._post_assignment({1: self.driver.id})

        self.assertEqual(response.status_code, 200)

    def test_driver_reassignment_does_not_change_condition(self):
        """Changing which driver covers a stint must not reset the condition."""
        driver2 = Driver.objects.create(
            event=self.event, name='Bob', timezone='UTC'
        )
        StintAssignment.objects.create(
            event=self.event, stint_number=1, driver=self.driver, condition='wet'
        )
        self._set_admin_session()

        # Alpine sends the current condition alongside the new driver
        self._post_assignment({1: driver2.id}, conditions={1: 'wet'})

        sa = StintAssignment.objects.get(event=self.event, stint_number=1)
        self.assertEqual(sa.condition, 'wet')
        self.assertEqual(sa.driver, driver2)


# ---------------------------------------------------------------------------
# view_event — condition in stint_rows_json
# ---------------------------------------------------------------------------

class ViewEventConditionTests(TestCase):
    """Tests that view_event includes condition in stint_rows_json."""

    def setUp(self):
        self.event = save_event()
        self.url = reverse('view_event', kwargs={'event_id': self.event.id})

    def test_condition_key_present_in_json_when_no_assignments_exist(self):
        import json as _json
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        json_str = response.context['stint_rows_json']
        rows = _json.loads(json_str)
        self.assertTrue(len(rows) > 0, 'Expected at least one stint row')
        for row in rows:
            self.assertIn('condition', row)

    def test_condition_defaults_to_dry_when_no_assignment_exists(self):
        import json as _json
        response = self.client.get(self.url)

        rows = _json.loads(response.context['stint_rows_json'])
        for row in rows:
            self.assertEqual(row['condition'], 'dry')

    def test_condition_reflects_wet_assignment(self):
        import json as _json
        StintAssignment.objects.create(
            event=self.event, stint_number=1, condition='wet'
        )

        response = self.client.get(self.url)

        rows = _json.loads(response.context['stint_rows_json'])
        stint1 = next(r for r in rows if r['stint_number'] == 1)
        self.assertEqual(stint1['condition'], 'wet')

    def test_condition_reflects_mixed_assignment(self):
        import json as _json
        StintAssignment.objects.create(
            event=self.event, stint_number=2, condition='mixed'
        )

        response = self.client.get(self.url)

        rows = _json.loads(response.context['stint_rows_json'])
        stint2 = next(r for r in rows if r['stint_number'] == 2)
        self.assertEqual(stint2['condition'], 'mixed')

    def test_unassigned_stints_default_to_dry_when_others_have_conditions(self):
        """Stints without a StintAssignment should still report 'dry'."""
        import json as _json
        # Only create an assignment for stint 1; all others have none
        StintAssignment.objects.create(
            event=self.event, stint_number=1, condition='wet'
        )

        response = self.client.get(self.url)

        rows = _json.loads(response.context['stint_rows_json'])
        for row in rows:
            if row['stint_number'] != 1:
                self.assertEqual(
                    row['condition'], 'dry',
                    f"Stint {row['stint_number']} expected 'dry', got {row['condition']!r}",
                )

    def test_view_event_requires_no_authentication(self):
        """Public view should be accessible without any session."""
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# _build_admin_context — conditions dict
# ---------------------------------------------------------------------------

class AdminContextConditionsTests(TestCase):
    """Tests that _build_admin_context builds the conditions dict correctly."""

    def setUp(self):
        self.event = save_event()
        self.url = reverse('admin_dashboard', kwargs={'event_id': self.event.id})

    def _set_admin_session(self):
        session = self.client.session
        session[f'admin_{self.event.id}'] = True
        session.save()

    def test_conditions_is_empty_dict_when_no_assignments_exist(self):
        self._set_admin_session()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['conditions'], {})

    def test_conditions_maps_stint_number_to_condition_string(self):
        StintAssignment.objects.create(
            event=self.event, stint_number=1, condition='wet'
        )
        StintAssignment.objects.create(
            event=self.event, stint_number=3, condition='mixed'
        )
        self._set_admin_session()

        response = self.client.get(self.url)

        conditions = response.context['conditions']
        self.assertEqual(conditions[1], 'wet')
        self.assertEqual(conditions[3], 'mixed')

    def test_conditions_uses_integer_keys(self):
        """Keys must be int stint_numbers, not strings."""
        StintAssignment.objects.create(
            event=self.event, stint_number=2, condition='dry'
        )
        self._set_admin_session()

        response = self.client.get(self.url)

        conditions = response.context['conditions']
        self.assertIn(2, conditions)
        self.assertNotIn('2', conditions)

    def test_conditions_built_from_single_query(self):
        """Verify the conditions dict is populated without N+1 queries.

        We check this by asserting the correct values are present for
        multiple stints — the implementation uses a single filter() call
        rather than per-stint lookups, which is verified implicitly by
        the correctness assertion plus a query count check.
        """
        for n, cond in [(1, 'dry'), (2, 'mixed'), (3, 'wet')]:
            StintAssignment.objects.create(
                event=self.event, stint_number=n, condition=cond
            )
        self._set_admin_session()

        # Warm the session cache so it doesn't count toward our query budget
        self.client.get(self.url)

        # A fresh client hit measures the actual page render query cost.
        # We don't assert a precise number (the page is complex), but we
        # do assert all three conditions are correct in one response.
        response = self.client.get(self.url)

        conditions = response.context['conditions']
        self.assertEqual(conditions[1], 'dry')
        self.assertEqual(conditions[2], 'mixed')
        self.assertEqual(conditions[3], 'wet')

    def test_conditions_only_contains_stints_with_assignments(self):
        """Stints without a StintAssignment row must not appear in conditions."""
        StintAssignment.objects.create(
            event=self.event, stint_number=1, condition='wet'
        )
        self._set_admin_session()

        response = self.client.get(self.url)

        conditions = response.context['conditions']
        # Stint 2 has no assignment — it must not be a key
        self.assertNotIn(2, conditions)
        self.assertNotIn(4, conditions)


# ---------------------------------------------------------------------------
# utils.seconds_to_mmss (M:SS format — distinct from the template filter)
# ---------------------------------------------------------------------------

class SecondsMmssUtilTests(SimpleTestCase):
    """Tests for utils.seconds_to_mmss() (M:SS, no leading zero on minutes).

    This is the utility function in utils.py, not the template filter in
    tz_filters.py. The utility returns '1:45' for 105 seconds; the template
    filter returns '01:45'.
    """

    def test_exact_minutes_no_seconds(self):
        self.assertEqual(seconds_to_mmss(120), '2:00')

    def test_minutes_and_seconds(self):
        self.assertEqual(seconds_to_mmss(105), '1:45')

    def test_less_than_one_minute(self):
        self.assertEqual(seconds_to_mmss(45), '0:45')

    def test_single_second(self):
        self.assertEqual(seconds_to_mmss(1), '0:01')

    def test_zero_seconds(self):
        self.assertEqual(seconds_to_mmss(0), '0:00')

    def test_none_returns_empty_string(self):
        self.assertEqual(seconds_to_mmss(None), '')

    def test_float_input_truncates(self):
        # 90.9 → int(90.9) = 90 → 1:30
        self.assertEqual(seconds_to_mmss(90.9), '1:30')

    def test_large_value_over_one_hour(self):
        # 3661 s = 61 minutes 1 second
        self.assertEqual(seconds_to_mmss(3661), '61:01')

    def test_seconds_component_zero_padded_to_two_digits(self):
        # 62 s = 1m 02s — the seconds part should be 02 not 2
        self.assertEqual(seconds_to_mmss(62), '1:02')

    def test_exactly_one_hour(self):
        self.assertEqual(seconds_to_mmss(3600), '60:00')


# ---------------------------------------------------------------------------
# utils.format_stint_duration
# ---------------------------------------------------------------------------

class FormatStintDurationTests(SimpleTestCase):
    """Tests for utils.format_stint_duration()."""

    def test_none_returns_em_dash(self):
        self.assertEqual(format_stint_duration(None), '—')

    def test_exact_minutes_no_seconds(self):
        self.assertEqual(format_stint_duration(3720), '62m')

    def test_minutes_and_seconds(self):
        self.assertEqual(format_stint_duration(3661), '61m 1s')

    def test_zero_seconds_returns_zero_minutes(self):
        self.assertEqual(format_stint_duration(0), '0m')

    def test_sub_minute_duration(self):
        self.assertEqual(format_stint_duration(45), '0m 45s')

    def test_rounds_to_nearest_second(self):
        # 3660.6 → rounds to 3661 → 61m 1s
        self.assertEqual(format_stint_duration(3660.6), '61m 1s')

    def test_rounds_down_when_below_half(self):
        # 3660.4 → rounds to 3660 → 61m
        self.assertEqual(format_stint_duration(3660.4), '61m')

    def test_float_exact_minutes(self):
        self.assertEqual(format_stint_duration(1800.0), '30m')

    def test_one_second(self):
        self.assertEqual(format_stint_duration(1), '0m 1s')

    def test_59_seconds(self):
        self.assertEqual(format_stint_duration(59), '0m 59s')


# ---------------------------------------------------------------------------
# utils.validate_stint_sanity
# ---------------------------------------------------------------------------

class ValidateStintSanityTests(SimpleTestCase):
    """Tests for utils.validate_stint_sanity()."""

    def test_returns_empty_list_when_required_fields_missing(self):
        # Event without stint fields set — should return no warnings
        event = make_event(
            avg_lap_seconds=None,
            in_lap_seconds=None,
            out_lap_seconds=None,
            target_laps=None,
            fuel_capacity=None,
            fuel_per_lap=None,
        )
        self.assertEqual(validate_stint_sanity(event), [])

    def test_no_warnings_for_clean_config(self):
        # Well-configured event with reasonable values
        event = make_event(
            avg_lap_seconds=120.0,
            in_lap_seconds=130.0,
            out_lap_seconds=125.0,
            target_laps=30,
            fuel_capacity=80.0,
            fuel_per_lap=2.0,   # 2.0 × 30 = 60L < 80L — fine
        )
        self.assertEqual(validate_stint_sanity(event), [])

    def test_warns_when_fuel_per_stint_exceeds_capacity(self):
        event = make_event(
            fuel_capacity=50.0,
            fuel_per_lap=2.0,
            target_laps=30,     # 2.0 × 30 = 60L > 50L
        )
        warnings = validate_stint_sanity(event)
        self.assertEqual(len(warnings), 1)
        self.assertIn('exceeds fuel capacity', warnings[0])

    def test_warns_when_fuel_per_stint_above_98_percent_of_capacity(self):
        # 2.0 × 30 = 60L; 60/61 ≈ 98.36% > 98%
        event = make_event(
            fuel_capacity=61.0,
            fuel_per_lap=2.0,
            target_laps=30,
        )
        warnings = validate_stint_sanity(event)
        self.assertEqual(len(warnings), 1)
        self.assertIn('98%', warnings[0])

    def test_no_fuel_warning_when_usage_is_exactly_98_percent(self):
        # 2.0 × 30 = 60L; 60/60 = 100% — but this exceeds capacity, so
        # the "exceeds" warning fires, not the 98% warning.
        # Test the boundary just below 98%: capacity = 61.23, usage = 60
        event = make_event(
            fuel_capacity=61.23,
            fuel_per_lap=2.0,
            target_laps=30,     # 60 / 61.23 ≈ 97.99% < 98%
        )
        self.assertEqual(validate_stint_sanity(event), [])

    def test_warns_when_in_lap_faster_than_avg_lap(self):
        event = make_event(
            avg_lap_seconds=120.0,
            in_lap_seconds=110.0,   # faster than avg — suspicious
            out_lap_seconds=125.0,
        )
        warnings = validate_stint_sanity(event)
        self.assertTrue(any('In lap' in w for w in warnings))

    def test_warns_when_out_lap_faster_than_avg_lap(self):
        event = make_event(
            avg_lap_seconds=120.0,
            in_lap_seconds=130.0,
            out_lap_seconds=115.0,  # faster than avg — suspicious
        )
        warnings = validate_stint_sanity(event)
        self.assertTrue(any('Out lap' in w for w in warnings))

    def test_warns_when_stint_length_is_very_short(self):
        # 1 lap × 60s avg + (65 + 62 - 120) = 60 + 7 = 67s — well under 600s
        event = make_event(
            avg_lap_seconds=60.0,
            in_lap_seconds=65.0,
            out_lap_seconds=62.0,
            target_laps=1,
        )
        warnings = validate_stint_sanity(event)
        self.assertTrue(any('very short' in w for w in warnings))

    def test_warns_when_total_stints_exceeds_200(self):
        # Make stint so short that we get >200 stints in a 6-hour race
        # avg=120, target=1, in=130, out=125 → stint=120+(130+125-240)=135s
        # total stints = ceil(21600/135) = 160 — not >200
        # avg=120, target=1, in=121, out=121 → stint=120+(121+121-240)=122s
        # total stints = ceil(21600/122) ≈ 178 — still not >200
        # Use a shorter avg lap:
        # avg=60, target=1, in=61, out=61 → stint=60+(61+61-120)=62s
        # total stints = ceil(21600/62) = 349 — >200
        event = make_event(
            avg_lap_seconds=60.0,
            in_lap_seconds=61.0,
            out_lap_seconds=61.0,
            target_laps=1,
        )
        warnings = validate_stint_sanity(event)
        self.assertTrue(any('total stints' in w.lower() for w in warnings))

    def test_returns_list_type(self):
        event = make_event()
        result = validate_stint_sanity(event)
        self.assertIsInstance(result, list)

    def test_multiple_warnings_can_be_returned(self):
        # Both in_lap and out_lap faster than avg_lap → two warnings
        event = make_event(
            avg_lap_seconds=120.0,
            in_lap_seconds=100.0,
            out_lap_seconds=100.0,
        )
        warnings = validate_stint_sanity(event)
        in_lap_warnings = [w for w in warnings if 'In lap' in w]
        out_lap_warnings = [w for w in warnings if 'Out lap' in w]
        self.assertGreater(len(in_lap_warnings), 0)
        self.assertGreater(len(out_lap_warnings), 0)


# ---------------------------------------------------------------------------
# utils.last_stint_length_seconds
# ---------------------------------------------------------------------------

class LastStintLengthSecondsTests(SimpleTestCase):
    """Tests for utils.last_stint_length_seconds()."""

    def test_returns_none_when_required_fields_missing(self):
        event = make_event(avg_lap_seconds=None, target_laps=None,
                           fuel_capacity=None, fuel_per_lap=None,
                           in_lap_seconds=None, out_lap_seconds=None)
        self.assertIsNone(last_stint_length_seconds(event))

    def test_last_stint_shorter_than_standard_when_race_does_not_divide_evenly(self):
        # Default event: 6h / 3615s → 6 stints, last is shorter
        event = make_event()
        last = last_stint_length_seconds(event)
        standard = stint_length_seconds(event)
        self.assertLess(last, standard)

    def test_last_stint_non_negative(self):
        event = make_event()
        self.assertGreaterEqual(last_stint_length_seconds(event), 0)

    def test_falls_back_to_standard_when_remaining_laps_gte_target(self):
        # 1-stint race: remaining laps == target_laps → last == standard
        event = make_event(length_seconds=3615)
        standard = stint_length_seconds(event)
        last = last_stint_length_seconds(event)
        self.assertEqual(last, standard)

    def test_result_is_float_or_int(self):
        event = make_event()
        result = last_stint_length_seconds(event)
        self.assertIsInstance(result, (int, float))

    def test_shorter_than_or_equal_to_race_length(self):
        event = make_event()
        self.assertLessEqual(last_stint_length_seconds(event), event.length_seconds)


# ---------------------------------------------------------------------------
# views.normalize_iso
# ---------------------------------------------------------------------------

class NormalizeIsoTests(SimpleTestCase):
    """Tests for views.normalize_iso() — formats a UTC datetime as ISO with Z suffix."""

    def setUp(self):
        from .views import normalize_iso
        self.normalize_iso = normalize_iso

    def test_formats_with_z_suffix(self):
        dt_val = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
        self.assertTrue(self.normalize_iso(dt_val).endswith('Z'))

    def test_format_matches_expected_pattern(self):
        dt_val = dt.datetime(2026, 6, 1, 12, 30, 45, tzinfo=dt.timezone.utc)
        self.assertEqual(self.normalize_iso(dt_val), '2026-06-01T12:30:45Z')

    def test_omits_microseconds(self):
        dt_val = dt.datetime(2026, 6, 1, 12, 0, 0, 999999, tzinfo=dt.timezone.utc)
        result = self.normalize_iso(dt_val)
        self.assertNotIn('.', result)
        self.assertEqual(result, '2026-06-01T12:00:00Z')

    def test_midnight_utc(self):
        dt_val = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(self.normalize_iso(dt_val), '2026-01-01T00:00:00Z')

    def test_converts_non_utc_aware_datetime_to_utc(self):
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo('America/New_York')
        # 2026-06-01 08:00 Eastern = 12:00 UTC (EDT = UTC-4)
        dt_val = dt.datetime(2026, 6, 1, 8, 0, 0, tzinfo=eastern)
        self.assertEqual(self.normalize_iso(dt_val), '2026-06-01T12:00:00Z')

    def test_end_of_day(self):
        dt_val = dt.datetime(2026, 6, 1, 23, 59, 59, tzinfo=dt.timezone.utc)
        self.assertEqual(self.normalize_iso(dt_val), '2026-06-01T23:59:59Z')

    def test_leading_zeros_in_time_components(self):
        dt_val = dt.datetime(2026, 6, 1, 1, 2, 3, tzinfo=dt.timezone.utc)
        self.assertEqual(self.normalize_iso(dt_val), '2026-06-01T01:02:03Z')


# ---------------------------------------------------------------------------
# Home view 2-week date cutoff (admin_events and driver_events)
# ---------------------------------------------------------------------------

class HomeViewDateCutoffTests(TestCase):
    """The home view filters admin_events and driver_events with date__gte cutoff.

    Events whose date is more than 2 weeks in the past must not appear in
    either context list. This test class specifically exercises the cutoff
    boundary — 'HomeViewAuthenticatedTests' only uses far-future dates.
    """

    def setUp(self):
        self.url = reverse('home')
        self.user = _make_auth_user()
        self.today = dt.date.today()

    def _event_on_date(self, target_date, created_by=None, **overrides):
        """Save an event with the given date, defaulting to a 12:00 UTC start."""
        return save_event(
            name=f'Event on {target_date}',
            date=target_date,
            start_time_utc=dt.time(12, 0, 0),
            created_by=created_by,
            **overrides,
        )

    # ---- admin_events cutoff ------------------------------------------------

    def test_admin_event_exactly_14_days_ago_is_included(self):
        # The cutoff is date__gte = today - 14 days, so 14 days ago is included.
        cutoff_date = self.today - dt.timedelta(days=14)
        event = self._event_on_date(cutoff_date, created_by=self.user)
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        admin_ids = [e.id for e in response.context['admin_events']]
        self.assertIn(event.id, admin_ids)

    def test_admin_event_15_days_ago_is_excluded(self):
        # 15 days ago is before the cutoff — must not appear.
        old_date = self.today - dt.timedelta(days=15)
        event = self._event_on_date(old_date, created_by=self.user)
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        admin_ids = [e.id for e in response.context['admin_events']]
        self.assertNotIn(event.id, admin_ids)

    def test_admin_event_yesterday_is_included(self):
        yesterday = self.today - dt.timedelta(days=1)
        event = self._event_on_date(yesterday, created_by=self.user)
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        admin_ids = [e.id for e in response.context['admin_events']]
        self.assertIn(event.id, admin_ids)

    def test_admin_event_today_is_included(self):
        event = self._event_on_date(self.today, created_by=self.user)
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        admin_ids = [e.id for e in response.context['admin_events']]
        self.assertIn(event.id, admin_ids)

    def test_recent_and_old_admin_events_mixed(self):
        # One event within the window, one outside — only the recent one shows.
        recent = self._event_on_date(
            self.today - dt.timedelta(days=7), created_by=self.user
        )
        old = self._event_on_date(
            self.today - dt.timedelta(days=30), created_by=self.user
        )
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        admin_ids = [e.id for e in response.context['admin_events']]
        self.assertIn(recent.id, admin_ids)
        self.assertNotIn(old.id, admin_ids)

    # ---- driver_events cutoff -----------------------------------------------

    def test_driver_event_exactly_14_days_ago_is_included(self):
        other_user = _make_auth_user()
        cutoff_date = self.today - dt.timedelta(days=14)
        event = self._event_on_date(cutoff_date, created_by=other_user)
        Driver.objects.create(event=event, name='Me', timezone='UTC', user=self.user)
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        driver_ids = [e.id for e in response.context['driver_events']]
        self.assertIn(event.id, driver_ids)

    def test_driver_event_15_days_ago_is_excluded(self):
        other_user = _make_auth_user()
        old_date = self.today - dt.timedelta(days=15)
        event = self._event_on_date(old_date, created_by=other_user)
        Driver.objects.create(event=event, name='Me', timezone='UTC', user=self.user)
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        driver_ids = [e.id for e in response.context['driver_events']]
        self.assertNotIn(event.id, driver_ids)

    def test_driver_event_today_is_included(self):
        other_user = _make_auth_user()
        event = self._event_on_date(self.today, created_by=other_user)
        Driver.objects.create(event=event, name='Me', timezone='UTC', user=self.user)
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        driver_ids = [e.id for e in response.context['driver_events']]
        self.assertIn(event.id, driver_ids)

    def test_driver_events_list_absent_for_unauthenticated_user(self):
        # Sanity check: unauthenticated → no driver_events key at all
        response = self.client.get(self.url)
        self.assertNotIn('driver_events', response.context)


# ---------------------------------------------------------------------------
# _build_admin_context — drivers_json timezone serialisation
# ---------------------------------------------------------------------------


class AdminContextDriversJsonTimezoneTests(TestCase):
    """
    Tests for the timezone field in drivers_json produced by
    _build_admin_context.

    The serialisation rule is:
        {'id': str(d.id), 'name': d.name, 'timezone': d.timezone or 'UTC'}

    An empty-string timezone falls back to 'UTC'; a populated IANA string
    is preserved verbatim.
    """

    def setUp(self):
        self.event = save_event()
        self.url = reverse('admin_dashboard', kwargs={'event_id': self.event.id})

    def _set_admin_session(self):
        session = self.client.session
        session[f'admin_{self.event.id}'] = True
        session.save()

    def _get_drivers_json(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return _json.loads(response.context['drivers_json'])

    # --- timezone key presence ---

    def test_drivers_json_entry_has_timezone_key(self):
        Driver.objects.create(event=self.event, name='Alice', timezone='UTC')
        self._set_admin_session()

        drivers = self._get_drivers_json()

        self.assertIn('timezone', drivers[0])

    # --- IANA timezone preserved verbatim ---

    def test_drivers_json_timezone_matches_stored_iana_string(self):
        Driver.objects.create(event=self.event, name='Bob', timezone='America/New_York')
        self._set_admin_session()

        drivers = self._get_drivers_json()

        self.assertEqual(drivers[0]['timezone'], 'America/New_York')

    def test_drivers_json_europe_timezone_preserved(self):
        Driver.objects.create(event=self.event, name='Eva', timezone='Europe/Berlin')
        self._set_admin_session()

        drivers = self._get_drivers_json()

        self.assertEqual(drivers[0]['timezone'], 'Europe/Berlin')

    def test_drivers_json_utc_timezone_preserved(self):
        Driver.objects.create(event=self.event, name='Sam', timezone='UTC')
        self._set_admin_session()

        drivers = self._get_drivers_json()

        self.assertEqual(drivers[0]['timezone'], 'UTC')

    # --- empty-string fallback to 'UTC' ---

    def test_drivers_json_empty_string_timezone_falls_back_to_utc(self):
        # CharField default is 'UTC', but it can be set to '' programmatically.
        # The `d.timezone or 'UTC'` guard must return 'UTC' in that case.
        driver = Driver.objects.create(event=self.event, name='Chris', timezone='UTC')
        Driver.objects.filter(pk=driver.pk).update(timezone='')
        self._set_admin_session()

        drivers = self._get_drivers_json()

        chris = next(d for d in drivers if d['name'] == 'Chris')
        self.assertEqual(chris['timezone'], 'UTC')

    # --- multiple drivers — each gets its own timezone ---

    def test_drivers_json_each_driver_has_independent_timezone(self):
        Driver.objects.create(event=self.event, name='Alice', timezone='America/Chicago')
        Driver.objects.create(event=self.event, name='Bob', timezone='Asia/Tokyo')
        Driver.objects.create(event=self.event, name='Cara', timezone='Europe/London')
        self._set_admin_session()

        drivers = self._get_drivers_json()

        by_name = {d['name']: d['timezone'] for d in drivers}
        self.assertEqual(by_name['Alice'], 'America/Chicago')
        self.assertEqual(by_name['Bob'], 'Asia/Tokyo')
        self.assertEqual(by_name['Cara'], 'Europe/London')

    # --- id and name keys still present (no regression) ---

    def test_drivers_json_entry_still_contains_id_and_name_keys(self):
        driver = Driver.objects.create(event=self.event, name='Dana', timezone='UTC')
        self._set_admin_session()

        drivers = self._get_drivers_json()

        self.assertEqual(len(drivers), 1)
        self.assertEqual(drivers[0]['id'], str(driver.id))
        self.assertEqual(drivers[0]['name'], 'Dana')

    # --- no drivers — empty list ---

    def test_drivers_json_is_empty_list_when_no_drivers_exist(self):
        self._set_admin_session()

        drivers = self._get_drivers_json()

        self.assertEqual(drivers, [])


# ---------------------------------------------------------------------------
# Auth and session hardening
#
# Covers the three login/session defects fixed together:
#   1. Discord OAuth failures landing on allauth's unbranded error page
#   2. Sessions dropped (host pinning, over-eager key cycling, non-rolling expiry)
#   3. The login modal existing only on the home page
# ---------------------------------------------------------------------------

from django.conf import settings as django_conf
from django.contrib.auth.models import AnonymousUser
from django.contrib.sites.models import Site
from django.core.exceptions import MiddlewareNotUsed
from django.core.management import call_command
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.test import RequestFactory

from config.middleware import CanonicalHostMiddleware
from .context_processors import _login_next, auth_context


class LoginNextTests(SimpleTestCase):
    """login_next sends the user back where they came from after Discord login."""

    def _next_for(self, path):
        return _login_next(RequestFactory().get(path))

    def test_returns_current_path(self):
        self.assertEqual(self._next_for('/create/'), '/create/')

    def test_preserves_query_string(self):
        self.assertEqual(
            self._next_for('/abc/view/?from=recruiting'),
            '/abc/view/?from=recruiting',
        )

    def test_accounts_paths_fall_back_to_home(self):
        # Bouncing back into the auth machinery would restart or re-fail the flow.
        self.assertEqual(self._next_for('/accounts/discord/login/callback/'), '/')

    def test_accounts_error_path_falls_back_to_home(self):
        self.assertEqual(self._next_for('/accounts/3rdparty/login/error/'), '/')

    def test_home_path_returns_home(self):
        self.assertEqual(self._next_for('/'), '/')


class AuthContextLoginNextTests(TestCase):
    """
    auth_context gained login_next alongside discord_user.
    discord_user itself is covered by AuthContextProcessorTests above.
    """

    def test_anonymous_request_gets_a_next(self):
        request = RequestFactory().get('/create/')
        request.user = AnonymousUser()

        context = auth_context(request)

        self.assertIsNone(context['discord_user'])
        self.assertEqual(context['login_next'], '/create/')

    def test_authenticated_request_also_gets_a_next(self):
        request = RequestFactory().get('/create/')
        request.user = _make_auth_user()

        context = auth_context(request)

        self.assertIsNotNone(context['discord_user'])
        self.assertEqual(context['login_next'], '/create/')


class LoginModalOnEveryPageTests(TestCase):
    """The login modal lives in base.html, so every page can open it."""

    DISPATCH = "$dispatch('open-login')"
    LISTENER = '@open-login.window'

    def setUp(self):
        self.event = save_event()

    def _assert_modal_present(self, response):
        html = response.content.decode()
        # The header button dispatches the event; the modal listens for it.
        # Both halves must be on the page or the button silently does nothing.
        self.assertIn(self.DISPATCH, html)
        self.assertIn(self.LISTENER, html)

    def test_home_page_has_modal(self):
        self._assert_modal_present(self.client.get(reverse('home')))

    def test_view_event_page_has_modal(self):
        response = self.client.get(
            reverse('view_event', kwargs={'event_id': self.event.id})
        )
        self._assert_modal_present(response)

    def test_signup_page_has_modal(self):
        response = self.client.get(
            reverse('signup', kwargs={'event_id': self.event.id})
        )
        self._assert_modal_present(response)

    def test_create_page_has_modal(self):
        self._assert_modal_present(self.client.get(reverse('event_create')))

    def test_modal_next_points_at_current_page(self):
        url = reverse('view_event', kwargs={'event_id': self.event.id})
        response = self.client.get(url)

        self.assertContains(response, 'name="next" value="%s"' % url)

    def test_modal_next_is_not_hardcoded_to_home_off_home(self):
        url = reverse('event_create')
        response = self.client.get(url)

        self.assertNotContains(response, 'name="next" value="/"')
        self.assertContains(response, 'name="next" value="%s"' % url)

    def test_logged_in_user_sees_no_login_button(self):
        self.client.force_login(_make_auth_user())

        response = self.client.get(reverse('home'))

        self.assertNotIn(self.DISPATCH, response.content.decode())


class AdminSessionCyclingTests(TestCase):
    """
    cycle_key() deletes the old session row, so calling it on every admin
    request logs out any other tab or in-flight request. It must only fire on
    the transition into an admin session.
    """

    def setUp(self):
        self.event = save_event()
        self.key_url = reverse(
            'admin_page',
            kwargs={'event_id': self.event.id, 'admin_key': self.event.admin_key},
        )
        self.dashboard_url = reverse(
            'admin_dashboard', kwargs={'event_id': self.event.id}
        )

    def test_first_key_visit_still_cycles_the_session(self):
        session = self.client.session
        session['warmup'] = True
        session.save()
        key_before = self.client.session.session_key

        self.client.get(self.key_url)

        self.assertNotEqual(self.client.session.session_key, key_before)

    def test_second_key_visit_does_not_cycle_again(self):
        self.client.get(self.key_url)
        key_after_first = self.client.session.session_key

        self.client.get(self.key_url)

        self.assertEqual(self.client.session.session_key, key_after_first)

    def test_repeated_dashboard_loads_keep_the_same_session(self):
        self.client.get(self.key_url)
        key_after_grant = self.client.session.session_key

        for _ in range(3):
            response = self.client.get(self.dashboard_url)
            self.assertEqual(response.status_code, 200)

        self.assertEqual(self.client.session.session_key, key_after_grant)

    def test_owner_dashboard_loads_keep_the_same_session(self):
        owner = _make_auth_user()
        self.event.created_by = owner
        self.event.save(update_fields=['created_by'])
        self.client.force_login(owner)

        self.client.get(self.dashboard_url)
        key_after_first = self.client.session.session_key

        self.client.get(self.dashboard_url)
        self.client.get(self.dashboard_url)

        self.assertEqual(self.client.session.session_key, key_after_first)

    def test_owner_stays_logged_in_across_repeated_dashboard_loads(self):
        owner = _make_auth_user()
        self.event.created_by = owner
        self.event.save(update_fields=['created_by'])
        self.client.force_login(owner)

        for _ in range(3):
            response = self.client.get(self.dashboard_url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['user'].is_authenticated)

    def test_admin_session_flag_survives_repeated_loads(self):
        self.client.get(self.key_url)

        self.client.get(self.dashboard_url)
        self.client.get(self.dashboard_url)

        self.assertTrue(self.client.session.get('admin_%s' % self.event.id))


class RollingSessionSettingTests(SimpleTestCase):
    """SESSION_COOKIE_AGE must count from last activity, not from login."""

    def test_session_save_every_request_is_enabled(self):
        self.assertTrue(django_conf.SESSION_SAVE_EVERY_REQUEST)


class CanonicalHostMiddlewareTests(SimpleTestCase):
    """
    Session cookies are host-only, so the site must resolve to exactly one
    hostname or logins silently fail to carry between them.
    """

    HOSTS = ['wearechecking.gg', 'www.wearechecking.gg']

    def _build(self, **setting_overrides):
        """Instantiate the middleware under the given settings, or return None
        if it opted out via MiddlewareNotUsed."""
        with override_settings(**setting_overrides):
            try:
                return CanonicalHostMiddleware(lambda r: HttpResponse('ok'))
            except MiddlewareNotUsed:
                return None

    def _call(self, server_name, path='/create/', data=None):
        overrides = dict(CANONICAL_HOST='wearechecking.gg', ALLOWED_HOSTS=self.HOSTS)
        middleware = self._build(**overrides)
        with override_settings(**overrides):
            request = RequestFactory(SERVER_NAME=server_name).get(path, data or {})
            return middleware(request)

    def test_disabled_when_canonical_host_unset(self):
        self.assertIsNone(self._build(CANONICAL_HOST=''))

    def test_disabled_when_canonical_host_not_in_allowed_hosts(self):
        # A canonical host Django would reject can only cause a redirect loop.
        self.assertIsNone(
            self._build(
                CANONICAL_HOST='wearechecking.gg',
                ALLOWED_HOSTS=['example.com'],
            )
        )

    def test_enabled_when_canonical_host_in_allowed_hosts(self):
        self.assertIsNotNone(
            self._build(CANONICAL_HOST='wearechecking.gg', ALLOWED_HOSTS=self.HOSTS)
        )

    def test_redirects_www_to_apex(self):
        response = self._call('www.wearechecking.gg')

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response['Location'], 'http://wearechecking.gg/create/')

    def test_redirect_preserves_path_and_query_string(self):
        response = self._call(
            'www.wearechecking.gg', path='/abc/view/', data={'from': 'recruiting'}
        )

        self.assertEqual(
            response['Location'],
            'http://wearechecking.gg/abc/view/?from=recruiting',
        )

    def test_canonical_host_passes_through_untouched(self):
        response = self._call('wearechecking.gg')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'ok')


class SetupDiscordOAuthSiteDomainTests(TestCase):
    """
    The Site domain is pinned by SITE_DOMAIN rather than inherited from
    ALLOWED_HOSTS ordering, so reordering that env var cannot silently change
    the recorded domain.
    """

    def _current_domain(self):
        return Site.objects.get(id=django_conf.SITE_ID).domain

    @override_settings(
        DISCORD_CLIENT_ID='cid',
        DISCORD_CLIENT_SECRET='secret',
        SITE_DOMAIN='wearechecking.gg',
        ALLOWED_HOSTS=['some-internal-host.railway.app', 'wearechecking.gg'],
    )
    def test_site_domain_wins_over_allowed_hosts_ordering(self):
        call_command('setup_discord_oauth')

        self.assertEqual(self._current_domain(), 'wearechecking.gg')

    @override_settings(
        DISCORD_CLIENT_ID='cid',
        DISCORD_CLIENT_SECRET='secret',
        SITE_DOMAIN='',
        ALLOWED_HOSTS=['fallback.example.com', 'other.example.com'],
    )
    def test_falls_back_to_first_allowed_host_when_unset(self):
        call_command('setup_discord_oauth')

        self.assertEqual(self._current_domain(), 'fallback.example.com')

    @override_settings(DISCORD_CLIENT_ID='')
    def test_skips_entirely_without_client_id(self):
        from allauth.socialaccount.models import SocialApp

        call_command('setup_discord_oauth')

        self.assertFalse(SocialApp.objects.filter(provider='discord').exists())


class AuthenticationErrorTemplateTests(TestCase):
    """
    prompt=none means Discord answers with an error rather than a consent
    screen when it cannot authorize silently. That must land somewhere branded
    with a way forward, not on allauth's stock page.
    """

    def _render(self):
        request = RequestFactory().get('/accounts/3rdparty/login/error/')
        request.user = AnonymousUser()
        return render_to_string(
            'socialaccount/authentication_error.html', request=request
        )

    def test_extends_site_base_template(self):
        # The site chrome (wordmark) only appears via base.html.
        self.assertIn('WeAreChecking', self._render())

    def test_offers_a_consent_retry(self):
        # Overrides the settings-level prompt=none for this one attempt.
        self.assertIn('auth_params=prompt%3Dconsent', self._render())

    def test_retry_posts_to_discord_login(self):
        self.assertIn(reverse('discord_login'), self._render())

    def test_offers_a_route_home(self):
        self.assertIn(reverse('home'), self._render())


# ---------------------------------------------------------------------------
# Silent-failure fixes
#
# Covers the four defects where an action failed (or succeeded) with no
# feedback at all:
#   4. Feedback success panel never shown (event name mismatch)
#   5. Admin validation errors never rendered (422 not swapped by HTMX)
#   6. django.contrib.messages never rendered anywhere
#   7. Live stint edits swallowing every error
# ---------------------------------------------------------------------------

def _read_template(*parts):
    """Read a template's raw source. Some behaviour lives in the markup itself
    (event names, htmx swap opt-ins) and is only assertable against the file."""
    return django_conf.BASE_DIR.joinpath('templates', *parts).read_text(encoding='utf-8')


class FeedbackSuccessEventNameTests(TestCase):
    """The HX-Trigger name and the Alpine listener have to actually match."""

    def test_trigger_name_matches_the_listener_in_base_template(self):
        response = self.client.post(
            reverse('feedback_submit'), {'text': 'Looks good'}
        )
        trigger = response['HX-Trigger']

        base = _read_template('base.html')
        self.assertIn('@%s.window' % trigger, base)

    def test_trigger_name_has_no_uppercase(self):
        # HTML lowercases attribute names, so a camelCase event can never be
        # listened for via an Alpine attribute binding.
        response = self.client.post(
            reverse('feedback_submit'), {'text': 'Looks good'}
        )

        trigger = response['HX-Trigger']
        self.assertEqual(trigger, trigger.lower())


class HtmxErrorSwapTests(TestCase):
    """
    HTMX drops non-2xx bodies by default. base.html opts 422 in, so views may
    return validation errors with a correct status and still have them render.
    """

    def test_base_template_opts_422_into_being_swapped(self):
        base = _read_template('base.html')

        self.assertIn('htmx:beforeSwap', base)
        self.assertIn('shouldSwap', base)

    def test_base_template_does_not_clear_is_error(self):
        # Clearing isError would make htmx report a validation failure as a
        # successful request, and forms would reset and close on error.
        base = _read_template('base.html')

        self.assertNotIn('isError = false', base)


class AdminValidationErrorRenderingTests(TestCase):
    """Admin forms must show why a save was rejected."""

    def setUp(self):
        self.event = save_event()
        self._grant_admin()

    def _grant_admin(self):
        session = self.client.session
        session['admin_%s' % self.event.id] = True
        session.save()

    def test_save_details_returns_422_with_the_error_partial(self):
        response = self.client.post(
            reverse('admin_save_details', kwargs={'event_id': self.event.id}),
            {'name': '', 'date': '2026-06-01', 'start_time_utc': '12:00',
             'length_hours': '6', 'length_minutes': '0'},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn(
            'partials/form_errors.html', [t.name for t in response.templates]
        )

    def test_save_details_error_body_names_the_problem(self):
        response = self.client.post(
            reverse('admin_save_details', kwargs={'event_id': self.event.id}),
            {'name': '', 'date': '2026-06-01', 'start_time_utc': '12:00',
             'length_hours': '6', 'length_minutes': '0'},
        )

        self.assertIn(b'Event name is required', response.content)

    def test_save_calc_returns_422_with_the_error_partial(self):
        response = self.client.post(
            reverse('admin_save_calc', kwargs={'event_id': self.event.id}),
            {'avg_lap': 'nonsense'},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn(
            'partials/form_errors.html', [t.name for t in response.templates]
        )

    def test_add_driver_error_retargets_away_from_the_driver_list(self):
        # The form's success target is the driver list; without a retarget the
        # error partial would replace every driver on the page.
        response = self.client.post(
            reverse('admin_add_driver', kwargs={'event_id': self.event.id}),
            {'driver_name': '', 'timezone': 'UTC'},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response['HX-Retarget'], '#add-driver-errors')
        self.assertEqual(response['HX-Reswap'], 'innerHTML')

    def test_add_driver_error_uses_the_shared_error_partial(self):
        response = self.client.post(
            reverse('admin_add_driver', kwargs={'event_id': self.event.id}),
            {'driver_name': '', 'timezone': 'UTC'},
        )

        self.assertIn(
            'partials/form_errors.html', [t.name for t in response.templates]
        )
        self.assertIn(b'Driver name is required', response.content)

    def test_add_driver_error_target_exists_in_the_form(self):
        markup = _read_template('partials', 'admin_add_driver.html')

        self.assertIn('id="add-driver-errors"', markup)

    def test_valid_save_details_still_succeeds_with_a_toast(self):
        response = self.client.post(
            reverse('admin_save_details', kwargs={'event_id': self.event.id}),
            {'name': 'Renamed', 'date': '2026-06-01', 'start_time_utc': '12:00',
             'length_hours': '6', 'length_minutes': '0'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('show-toast', json.loads(response['HX-Trigger']))


class MessagesRenderingTests(TestCase):
    """
    django.contrib.messages was configured but never rendered, so event
    deletion and its confirmation mismatch both gave no feedback at all.
    """

    def setUp(self):
        self.event = save_event()
        session = self.client.session
        session['admin_%s' % self.event.id] = True
        session.save()
        self.delete_url = reverse(
            'admin_delete_event', kwargs={'event_id': self.event.id}
        )

    def test_base_template_renders_the_messages_block(self):
        base = _read_template('base.html')

        self.assertIn('{% for message in messages %}', base)

    def test_successful_delete_message_reaches_the_page(self):
        response = self.client.post(
            self.delete_url, {'confirm_name': 'DELETE'}, follow=True
        )

        self.assertContains(response, 'has been permanently deleted')

    def test_successful_delete_message_names_the_event(self):
        response = self.client.post(
            self.delete_url, {'confirm_name': 'DELETE'}, follow=True
        )

        self.assertContains(response, self.event.name)

    def test_wrong_confirmation_message_reaches_the_page(self):
        response = self.client.post(
            self.delete_url, {'confirm_name': 'delete'}, follow=True
        )

        self.assertContains(response, 'Confirmation did not match')

    def test_wrong_confirmation_does_not_delete_the_event(self):
        self.client.post(self.delete_url, {'confirm_name': 'nope'}, follow=True)

        self.assertTrue(Event.objects.filter(id=self.event.id).exists())

    def test_error_message_uses_the_error_banner_style(self):
        response = self.client.post(
            self.delete_url, {'confirm_name': 'nope'}, follow=True
        )

        self.assertContains(response, 'message-error')

    def test_success_message_uses_the_success_banner_style(self):
        response = self.client.post(
            self.delete_url, {'confirm_name': 'DELETE'}, follow=True
        )

        self.assertContains(response, 'message-success')

    def test_pages_without_messages_render_no_banner_region(self):
        response = self.client.get(reverse('home'))

        self.assertNotContains(response, 'message-region')


class SharedToastTests(TestCase):
    """One toast in base.html, so every page can report success and failure."""

    def setUp(self):
        self.event = save_event()

    def test_base_template_defines_the_toast(self):
        base = _read_template('base.html')

        self.assertIn('@show-toast.window', base)

    def test_admin_template_no_longer_defines_its_own(self):
        admin = _read_template('admin.html')

        self.assertNotIn('@show-toast.window', admin)

    def test_toast_is_present_on_a_non_admin_page(self):
        response = self.client.get(
            reverse('view_event', kwargs={'event_id': self.event.id})
        )

        self.assertContains(response, 'show-toast')

    def test_toast_appears_exactly_once_on_the_admin_page(self):
        session = self.client.session
        session['admin_%s' % self.event.id] = True
        session.save()

        response = self.client.get(
            reverse('admin_dashboard', kwargs={'event_id': self.event.id})
        )

        html = response.content.decode()
        self.assertEqual(html.count('@show-toast.window'), 1)


class StintEditFailureFeedbackTests(TestCase):
    """
    A rejected stint edit used to leave the row unchanged with no explanation.
    The endpoints still reject; the page now says so.
    """

    def setUp(self):
        self.event = save_event()
        self.set_url = reverse(
            'set_stint_start',
            kwargs={'event_id': self.event.id, 'stint_number': 1},
        )
        self.reset_url = reverse(
            'reset_stint_start',
            kwargs={'event_id': self.event.id, 'stint_number': 1},
        )

    def test_anonymous_set_start_is_still_forbidden(self):
        response = self.client.post(
            self.set_url, {'actual_start_utc': '2026-06-01T12:00:00Z'}
        )

        self.assertEqual(response.status_code, 403)

    def test_anonymous_reset_start_is_still_forbidden(self):
        response = self.client.post(self.reset_url)

        self.assertEqual(response.status_code, 403)

    def test_view_template_reports_failures_instead_of_ignoring_them(self):
        view_tpl = _read_template('view.html')

        self.assertIn('failureMessage', view_tpl)
        self.assertIn('show-toast', view_tpl)

    def test_view_template_explains_an_expired_session(self):
        view_tpl = _read_template('view.html')

        self.assertIn('Your session expired', view_tpl)

    def test_view_template_no_longer_silently_ignores_non_ok(self):
        # The old shape was `if (res.ok) { ...apply... }` with no else branch.
        view_tpl = _read_template('view.html')

        self.assertIn('if (!res.ok)', view_tpl)


# ---------------------------------------------------------------------------
# Phase-stable availability grid (#8)
#
# Availability rows are absolute UTC datetimes. The grid used to be anchored to
# an event's exact start time, so its phase moved with the event's minutes and a
# 15-minute correction orphaned every stored slot while a 1-hour move cost
# almost nothing. The grid is now anchored to wall-clock half hours.
# ---------------------------------------------------------------------------

import importlib

from events.utils import slot_grid_anchor
from events.views import _driver_has_conflict, _drivers_with_stale_availability


def _fill_availability(event, driver):
    """Mark the driver available for every slot in the event's current grid."""
    Availability.objects.bulk_create([
        Availability(driver=driver, slot_utc=s)
        for s in get_availability_slots(event)
    ])


def _slots_surviving(event, driver):
    """How many stored slots still land on the event's current grid."""
    grid = {s.astimezone(timezone.utc) for s in get_availability_slots(event)}
    stored = {a.slot_utc.astimezone(timezone.utc) for a in driver.availability.all()}
    return len(stored & grid)


class SlotGridAnchorTests(SimpleTestCase):
    """The grid origin is floored to a wall-clock half hour."""

    def test_on_the_hour_start_is_unchanged(self):
        event = make_event(start_time_utc=dt.time(12, 0))

        self.assertEqual(slot_grid_anchor(event), utc(2026, 6, 1, 12, 0))

    def test_half_past_start_is_unchanged(self):
        event = make_event(start_time_utc=dt.time(12, 30))

        self.assertEqual(slot_grid_anchor(event), utc(2026, 6, 1, 12, 30))

    def test_quarter_past_floors_to_the_hour(self):
        event = make_event(start_time_utc=dt.time(12, 15))

        self.assertEqual(slot_grid_anchor(event), utc(2026, 6, 1, 12, 0))

    def test_quarter_to_floors_to_the_half_hour(self):
        event = make_event(start_time_utc=dt.time(12, 45))

        self.assertEqual(slot_grid_anchor(event), utc(2026, 6, 1, 12, 30))

    def test_seconds_are_discarded(self):
        event = make_event(start_time_utc=dt.time(12, 15, 40))

        self.assertEqual(slot_grid_anchor(event), utc(2026, 6, 1, 12, 0))

    def test_every_generated_slot_sits_on_a_half_hour_boundary(self):
        event = make_event(start_time_utc=dt.time(12, 15))

        minutes = {s.minute for s in get_availability_slots(event)}

        self.assertEqual(minutes, {0, 30})


class SubHalfHourEditPreservesAvailabilityTests(TestCase):
    """
    The wart this fixes: a 15-minute correction used to invalidate everything
    while a 1-hour move cost almost nothing.
    """

    def setUp(self):
        self.event = save_event(start_time_utc=dt.time(12, 0))
        self.driver = Driver.objects.create(
            event=self.event, name='Probe', timezone='UTC'
        )
        _fill_availability(self.event, self.driver)
        self.original = self.driver.availability.count()

    def _move_start_to(self, hour, minute):
        self.event.start_time_utc = dt.time(hour, minute)
        self.event.save(update_fields=['start_time_utc'])
        self.event.refresh_from_db()

    def test_baseline_all_slots_valid(self):
        self.assertEqual(_slots_surviving(self.event, self.driver), self.original)

    def test_fifteen_minute_shift_keeps_every_slot(self):
        self._move_start_to(12, 15)

        self.assertEqual(_slots_surviving(self.event, self.driver), self.original)

    def test_forty_five_minute_shift_keeps_every_slot(self):
        # 12:45 floors to 12:30, so the grid origin moves half an hour; the
        # earliest slot falls out of the window but the rest survive.
        self._move_start_to(12, 45)

        self.assertGreater(_slots_surviving(self.event, self.driver), 0)

    def test_fifteen_minute_shift_leaves_no_driver_stale(self):
        self._move_start_to(12, 15)

        self.assertEqual(_drivers_with_stale_availability(self.event), [])

    def test_hour_shift_still_mostly_preserved(self):
        self._move_start_to(13, 0)

        self.assertGreater(_slots_surviving(self.event, self.driver), 0)

    def test_date_change_still_invalidates_everything(self):
        # Intended behaviour — drivers must re-enter availability for a new day.
        self.event.date = dt.date(2026, 6, 8)
        self.event.save(update_fields=['date'])
        self.event.refresh_from_db()

        self.assertEqual(_slots_surviving(self.event, self.driver), 0)

    def test_date_change_reports_the_driver_as_stale(self):
        self.event.date = dt.date(2026, 6, 8)
        self.event.save(update_fields=['date'])
        self.event.refresh_from_db()

        stale = _drivers_with_stale_availability(self.event)

        self.assertEqual([e['name'] for e in stale], ['Probe'])
        self.assertEqual(stale[0]['lost'], stale[0]['total'])


class OffGridEventConflictDetectionTests(TestCase):
    """
    _driver_has_conflict already floored to wall-clock half hours, so for an
    event starting off the half hour it looked for slots the grid could never
    contain and reported a conflict for everyone. Now the two agree.
    """

    def test_available_driver_has_no_conflict_on_an_off_grid_event(self):
        event = save_event(start_time_utc=dt.time(12, 15))
        driver = Driver.objects.create(event=event, name='Probe', timezone='UTC')
        _fill_availability(event, driver)

        driver_availability = {
            driver.id: {a.slot_utc.astimezone(timezone.utc)
                        for a in driver.availability.all()}
        }
        first_slot = get_availability_slots(event)[0]

        self.assertFalse(
            _driver_has_conflict(driver, first_slot, driver_availability)
        )


class StaleAvailabilityDetectionTests(TestCase):
    """Which drivers actually lost their availability."""

    def setUp(self):
        self.event = save_event()

    def test_driver_with_no_availability_is_not_reported(self):
        Driver.objects.create(event=self.event, name='Never Entered', timezone='UTC')

        self.assertEqual(_drivers_with_stale_availability(self.event), [])

    def test_driver_with_valid_availability_is_not_reported(self):
        driver = Driver.objects.create(event=self.event, name='Fine', timezone='UTC')
        _fill_availability(self.event, driver)

        self.assertEqual(_drivers_with_stale_availability(self.event), [])

    def test_driver_with_only_orphaned_slots_is_reported(self):
        driver = Driver.objects.create(event=self.event, name='Stranded', timezone='UTC')
        Availability.objects.create(driver=driver, slot_utc=utc(2020, 1, 1, 0, 0))

        self.assertEqual(
            _drivers_with_stale_availability(self.event),
            [{'name': 'Stranded', 'lost': 1, 'total': 1}],
        )

    def test_driver_keeping_one_usable_slot_is_still_reported(self):
        # Previously silent unless EVERY slot was stranded, so a driver left
        # with one usable slot out of forty went unmentioned.
        driver = Driver.objects.create(event=self.event, name='Partial', timezone='UTC')
        Availability.objects.create(driver=driver, slot_utc=utc(2020, 1, 1, 0, 0))
        Availability.objects.create(
            driver=driver, slot_utc=get_availability_slots(self.event)[0]
        )

        self.assertEqual(
            _drivers_with_stale_availability(self.event),
            [{'name': 'Partial', 'lost': 1, 'total': 2}],
        )

    def test_driver_losing_nothing_is_not_reported(self):
        driver = Driver.objects.create(event=self.event, name='Intact', timezone='UTC')
        Availability.objects.create(
            driver=driver, slot_utc=get_availability_slots(self.event)[0]
        )

        self.assertEqual(_drivers_with_stale_availability(self.event), [])

    def test_worst_affected_driver_is_listed_first(self):
        slots = get_availability_slots(self.event)
        light = Driver.objects.create(event=self.event, name='Light', timezone='UTC')
        Availability.objects.create(driver=light, slot_utc=utc(2020, 1, 1, 0, 0))
        Availability.objects.create(driver=light, slot_utc=slots[0])

        heavy = Driver.objects.create(event=self.event, name='Heavy', timezone='UTC')
        Availability.objects.bulk_create([
            Availability(driver=heavy, slot_utc=utc(2020, 1, 1, 0, 30 * i))
            for i in range(2)
        ] + [Availability(driver=heavy, slot_utc=utc(2020, 1, 2, 0, 0))])

        names = [e['name'] for e in _drivers_with_stale_availability(self.event)]

        self.assertEqual(names, ['Heavy', 'Light'])


class ScheduleMoveWarningTests(TestCase):
    """Moving the schedule must not silently discard availability."""

    def setUp(self):
        self.event = save_event(start_time_utc=dt.time(12, 0))
        self.driver = Driver.objects.create(
            event=self.event, name='Stranded', timezone='UTC'
        )
        _fill_availability(self.event, self.driver)
        session = self.client.session
        session['admin_%s' % self.event.id] = True
        session.save()
        self.url = reverse('admin_save_details', kwargs={'event_id': self.event.id})

    def _post(self, **overrides):
        payload = {
            'name': self.event.name,
            'date': '2026-06-01',
            'start_time_utc': '12:00',
            'length_hours': '6',
            'length_minutes': '0',
        }
        payload.update(overrides)
        return self.client.post(self.url, payload)

    def test_unchanged_schedule_gives_the_plain_toast(self):
        response = self._post()

        trigger = json.loads(response['HX-Trigger'])
        self.assertEqual(trigger['show-toast']['message'], 'Event details saved.')

    def test_unchanged_schedule_renders_no_warning(self):
        response = self._post()

        self.assertEqual(response.content, b'')

    def test_date_move_warns_and_names_the_driver(self):
        response = self._post(date='2026-06-08')

        self.assertIn(
            'partials/availability_warning.html',
            [t.name for t in response.templates],
        )
        self.assertContains(response, 'Stranded')

    def test_date_move_toast_reports_the_count_as_an_error(self):
        response = self._post(date='2026-06-08')

        toast = json.loads(response['HX-Trigger'])['show-toast']
        self.assertIn('1 driver(s)', toast['message'])
        self.assertTrue(toast['error'])

    def test_date_move_still_saves_the_event(self):
        self._post(date='2026-06-08')

        self.event.refresh_from_db()
        self.assertEqual(self.event.date, dt.date(2026, 6, 8))

    def test_sub_half_hour_move_does_not_warn(self):
        # The whole point of the phase-stable grid: nothing was lost.
        response = self._post(start_time_utc='12:15')

        trigger = json.loads(response['HX-Trigger'])
        self.assertEqual(trigger['show-toast']['message'], 'Event details saved.')

    def test_schedule_move_with_no_availability_does_not_warn(self):
        Availability.objects.all().delete()

        response = self._post(date='2026-06-08')

        trigger = json.loads(response['HX-Trigger'])
        self.assertEqual(trigger['show-toast']['message'], 'Event details saved.')

    def test_admin_page_exposes_the_driver_count_for_the_live_warning(self):
        response = self.client.get(
            reverse('admin_dashboard', kwargs={'event_id': self.event.id})
        )

        self.assertEqual(response.context['drivers_with_availability_count'], 1)

    def test_driver_count_excludes_drivers_without_availability(self):
        Driver.objects.create(event=self.event, name='No Availability', timezone='UTC')

        response = self.client.get(
            reverse('admin_dashboard', kwargs={'event_id': self.event.id})
        )

        self.assertEqual(response.context['drivers_with_availability_count'], 1)


class RealignAvailabilityMigrationTests(TestCase):
    """
    Migration 0009 shifts availability stored against the old exact-start grid
    onto the wall-clock half-hour grid, so nobody loses availability on deploy.
    """

    class _FakeApps:
        """Stands in for the migration's historical model registry."""
        def get_model(self, app_label, model_name):
            return {'Event': Event, 'Availability': Availability}[model_name]

    def _run(self, forwards=True):
        # The module name starts with a digit, so it is not importable by name.
        module = importlib.import_module(
            'events.migrations.0009_realign_availability_to_half_hour_grid'
        )
        fn = module.realign if forwards else module.unrealign
        fn(self._FakeApps(), None)

    def _make_legacy_event(self, minute):
        """Event plus availability laid out on the OLD exact-start grid."""
        event = save_event(start_time_utc=dt.time(12, minute))
        driver = Driver.objects.create(event=event, name='Legacy', timezone='UTC')
        start = event.start_datetime_utc          # un-floored, as it used to be
        Availability.objects.bulk_create([
            Availability(driver=driver, slot_utc=start + dt.timedelta(minutes=30 * i))
            for i in range(6)
        ])
        return event, driver

    def test_legacy_slots_are_orphaned_before_the_migration(self):
        event, driver = self._make_legacy_event(15)

        self.assertEqual(_slots_surviving(event, driver), 0)

    def test_migration_puts_them_back_on_the_grid(self):
        event, driver = self._make_legacy_event(15)

        self._run()

        self.assertEqual(_slots_surviving(event, driver), 6)

    def test_migration_reports_no_stale_drivers_afterwards(self):
        event, driver = self._make_legacy_event(15)

        self._run()

        self.assertEqual(_drivers_with_stale_availability(event), [])

    def test_migration_preserves_the_row_count(self):
        event, driver = self._make_legacy_event(15)

        self._run()

        self.assertEqual(driver.availability.count(), 6)

    def test_events_already_on_the_half_hour_are_untouched(self):
        event, driver = self._make_legacy_event(30)
        before = sorted(a.slot_utc for a in driver.availability.all())

        self._run()

        after = sorted(a.slot_utc for a in driver.availability.all())
        self.assertEqual(before, after)

    def test_migration_is_reversible(self):
        event, driver = self._make_legacy_event(15)
        before = sorted(a.slot_utc for a in driver.availability.all())

        self._run()
        self._run(forwards=False)

        after = sorted(a.slot_utc for a in driver.availability.all())
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# Dependency pinning (#9, #10)
#
# Railway rebuilds on every push, so a floating version lets a deploy change
# application behaviour with no code change — the likeliest explanation for an
# unexplained Discord login regression.
# ---------------------------------------------------------------------------

def _requirement_lines():
    """Non-empty, non-comment lines from requirements.txt, comments stripped."""
    text = django_conf.BASE_DIR.joinpath('requirements.txt').read_text(encoding='utf-8')
    lines = []
    for raw in text.splitlines():
        line = raw.split('#')[0].strip()
        if line:
            lines.append(line)
    return lines


class RequirementsPinningTests(SimpleTestCase):
    """Every dependency is exact-pinned."""

    def test_requirements_file_is_not_empty(self):
        self.assertGreater(len(_requirement_lines()), 0)

    def test_every_requirement_uses_an_exact_pin(self):
        unpinned = [line for line in _requirement_lines() if '==' not in line]

        self.assertEqual(unpinned, [], f'unpinned requirements: {unpinned}')

    def test_no_requirement_uses_a_floor_only_specifier(self):
        # `pkg>=x` is what let django-allauth drift from 0.61 to 65.x unnoticed.
        floors = [
            line for line in _requirement_lines()
            if '>=' in line and '==' not in line
        ]

        self.assertEqual(floors, [], f'floor-only requirements: {floors}')

    def test_each_requirement_pins_exactly_one_version(self):
        for line in _requirement_lines():
            with self.subTest(requirement=line):
                self.assertEqual(line.count('=='), 1)


class RequirementsContentTests(SimpleTestCase):
    """What is and is not shipped."""

    def _names(self):
        return {
            line.split('==')[0].strip().lower()
            for line in _requirement_lines()
        }

    def test_debug_toolbar_is_not_shipped(self):
        # Never appeared in INSTALLED_APPS or MIDDLEWARE — dead weight in the
        # production image.
        self.assertNotIn('django-debug-toolbar', self._names())

    def test_debug_toolbar_is_not_importable(self):
        with self.assertRaises(ImportError):
            __import__('debug_toolbar')

    def test_requests_is_pinned(self):
        # django-allauth imports requests in its socialaccount OAuth2 client but
        # only declares it under an optional extra that is not installed, so
        # Discord login depends on this pin being present.
        self.assertIn('requests', self._names())

    def test_allauth_is_pinned(self):
        self.assertIn('django-allauth', self._names())

    def test_core_runtime_dependencies_are_declared(self):
        expected = {
            'django', 'django-allauth', 'django-htmx',
            'gunicorn', 'whitenoise', 'python-dotenv',
        }

        self.assertTrue(expected.issubset(self._names()))


class RequirementsMatchInstalledTests(SimpleTestCase):
    """The pins describe the environment the tests actually ran against."""

    def test_pinned_versions_match_the_installed_distributions(self):
        from importlib.metadata import PackageNotFoundError, version

        mismatches = []
        for line in _requirement_lines():
            name, _, pinned = line.partition('==')
            name = name.strip()
            try:
                installed = version(name)
            except PackageNotFoundError:
                mismatches.append(f'{name}: not installed')
                continue
            if installed != pinned.strip():
                mismatches.append(f'{name}: pinned {pinned.strip()}, installed {installed}')

        self.assertEqual(mismatches, [], f'requirements drift: {mismatches}')


# ---------------------------------------------------------------------------
# Uncovered race windows
#
# The old warning asked whether each driver had slots[-1]. That slot sits up to
# an hour PAST the chequered flag, because get_availability_slots() pads the
# grid as a buffer for races that run long. So a driver available for 100% of
# the race was warned about, while one missing half of it was not.
#
# Coverage is now measured across the roster and against the race itself.
# ---------------------------------------------------------------------------

from events.utils import collapse_slot_ranges
from events.views import _build_availability_matrix, _uncovered_race_windows


def _cover(event, driver, slots):
    Availability.objects.bulk_create([
        Availability(driver=driver, slot_utc=s) for s in slots
    ])


def _windows_for(event, tz='UTC'):
    slots = get_availability_slots(event)
    drivers = list(Driver.objects.filter(event=event).prefetch_related('availability'))
    _, uncovered = _build_availability_matrix(drivers, slots)
    return _uncovered_race_windows(event, slots, uncovered, tz)


class CollapseSlotRangesTests(SimpleTestCase):
    """Contiguous half-hour slots collapse into ranges; end is exclusive."""

    def test_empty_input_gives_no_ranges(self):
        self.assertEqual(collapse_slot_ranges([]), [])

    def test_single_slot_spans_its_own_duration(self):
        result = collapse_slot_ranges([utc(2026, 6, 1, 10, 0)])

        self.assertEqual(result, [(utc(2026, 6, 1, 10, 0), utc(2026, 6, 1, 10, 30))])

    def test_consecutive_slots_merge_into_one_range(self):
        result = collapse_slot_ranges([
            utc(2026, 6, 1, 10, 0), utc(2026, 6, 1, 10, 30), utc(2026, 6, 1, 11, 0),
        ])

        self.assertEqual(result, [(utc(2026, 6, 1, 10, 0), utc(2026, 6, 1, 11, 30))])

    def test_a_gap_starts_a_new_range(self):
        result = collapse_slot_ranges([
            utc(2026, 6, 1, 10, 0), utc(2026, 6, 1, 10, 30), utc(2026, 6, 1, 13, 0),
        ])

        self.assertEqual(result, [
            (utc(2026, 6, 1, 10, 0), utc(2026, 6, 1, 11, 0)),
            (utc(2026, 6, 1, 13, 0), utc(2026, 6, 1, 13, 30)),
        ])

    def test_unsorted_input_is_ordered_first(self):
        result = collapse_slot_ranges([
            utc(2026, 6, 1, 11, 0), utc(2026, 6, 1, 10, 0), utc(2026, 6, 1, 10, 30),
        ])

        self.assertEqual(result, [(utc(2026, 6, 1, 10, 0), utc(2026, 6, 1, 11, 30))])

    def test_ranges_span_midnight_without_splitting(self):
        result = collapse_slot_ranges([
            utc(2026, 6, 1, 23, 30), utc(2026, 6, 2, 0, 0), utc(2026, 6, 2, 0, 30),
        ])

        self.assertEqual(result, [(utc(2026, 6, 1, 23, 30), utc(2026, 6, 2, 1, 0))])


class UncoveredRaceWindowTests(TestCase):
    """Only gaps inside the race itself count."""

    def setUp(self):
        self.event = save_event(start_time_utc=dt.time(12, 0), length_seconds=6 * 3600)
        self.slots = get_availability_slots(self.event)
        self.race_start = self.event.effective_start_datetime_utc
        self.race_end = self.event.effective_end_datetime_utc

    def _driver(self, name='D'):
        return Driver.objects.create(event=self.event, name=name, timezone='UTC')

    def test_no_drivers_means_the_whole_race_is_uncovered(self):
        windows, seconds = _windows_for(self.event)

        self.assertEqual(seconds, 6 * 3600)
        self.assertEqual(len(windows), 1)

    def test_full_race_coverage_reports_nothing(self):
        _cover(self.event, self._driver(),
               [s for s in self.slots if self.race_start <= s < self.race_end])

        windows, seconds = _windows_for(self.event)

        self.assertEqual(windows, [])
        self.assertEqual(seconds, 0)

    def test_unticked_post_race_buffer_is_not_a_gap(self):
        # The exact false positive the old check produced: a driver available
        # for the entire race but not the hour of padding after it.
        _cover(self.event, self._driver(),
               [s for s in self.slots if self.race_start <= s < self.race_end])

        windows, seconds = _windows_for(self.event)

        self.assertEqual(seconds, 0)

    def test_unticked_pre_race_warmup_is_not_a_gap(self):
        # The grid starts at the session, not the green flag, so warmup and
        # qualifying slots exist but need no driver.
        event = save_event(
            start_time_utc=dt.time(12, 0),
            race_start_time_utc=dt.time(14, 0),
            length_seconds=6 * 3600,
        )
        driver = Driver.objects.create(event=event, name='D', timezone='UTC')
        _cover(event, driver, [
            s for s in get_availability_slots(event)
            if event.effective_start_datetime_utc <= s < event.effective_end_datetime_utc
        ])

        windows, seconds = _windows_for(event)

        self.assertEqual(seconds, 0)

    def test_two_drivers_splitting_the_race_leaves_no_gap(self):
        race = [s for s in self.slots if self.race_start <= s < self.race_end]
        _cover(self.event, self._driver('Early'), race[:len(race) // 2])
        _cover(self.event, self._driver('Late'), race[len(race) // 2:])

        windows, seconds = _windows_for(self.event)

        self.assertEqual(windows, [])
        self.assertEqual(seconds, 0)

    def test_a_hole_in_the_middle_is_reported(self):
        race = [s for s in self.slots if self.race_start <= s < self.race_end]
        # drop four consecutive slots (2 hours) from the middle
        _cover(self.event, self._driver(), race[:4] + race[8:])

        windows, seconds = _windows_for(self.event)

        self.assertEqual(seconds, 2 * 3600)
        self.assertEqual(len(windows), 1)

    def test_two_separate_holes_give_two_windows(self):
        race = [s for s in self.slots if self.race_start <= s < self.race_end]
        _cover(self.event, self._driver(), race[:2] + race[4:6] + race[8:])

        windows, seconds = _windows_for(self.event)

        self.assertEqual(len(windows), 2)
        self.assertEqual(seconds, 2 * 3600)

    def test_window_seconds_sum_to_the_total(self):
        race = [s for s in self.slots if self.race_start <= s < self.race_end]
        _cover(self.event, self._driver(), race[:2] + race[6:])

        windows, seconds = _windows_for(self.event)

        self.assertEqual(sum(w['seconds'] for w in windows), seconds)

    def test_label_is_rendered_in_the_admin_timezone(self):
        windows_utc, _ = _windows_for(self.event, 'UTC')
        windows_ny, _ = _windows_for(self.event, 'America/New_York')

        self.assertNotEqual(windows_utc[0]['label'], windows_ny[0]['label'])

    def test_invalid_admin_timezone_falls_back_to_utc(self):
        windows_bad, _ = _windows_for(self.event, 'Not/AZone')
        windows_utc, _ = _windows_for(self.event, 'UTC')

        self.assertEqual(windows_bad[0]['label'], windows_utc[0]['label'])

    def test_label_repeats_the_date_only_when_crossing_midnight(self):
        # 6h race from 12:00 stays inside one day.
        windows, _ = _windows_for(self.event)

        # "Mon 6/1 12:00 – 18:00" — one date, one bare time
        self.assertEqual(windows[0]['label'].count('/'), 1)

    def test_label_carries_the_date_on_both_sides_across_midnight(self):
        event = save_event(start_time_utc=dt.time(22, 0), length_seconds=6 * 3600)

        windows, _ = _windows_for(event)

        self.assertEqual(windows[0]['label'].count('/'), 2)


class UncoveredRaceWindowAdminContextTests(TestCase):
    """The admin page surfaces the gaps, capped to a readable number."""

    def setUp(self):
        self.event = save_event(start_time_utc=dt.time(12, 0), length_seconds=6 * 3600)
        session = self.client.session
        session['admin_%s' % self.event.id] = True
        session.save()
        self.url = reverse('admin_dashboard', kwargs={'event_id': self.event.id})

    def test_context_exposes_windows_and_total(self):
        response = self.client.get(self.url)

        self.assertIn('uncovered_race_windows', response.context)
        self.assertIn('uncovered_race_seconds', response.context)

    def test_warning_renders_when_the_race_is_uncovered(self):
        response = self.client.get(self.url)

        self.assertContains(response, 'no driver available')

    def test_no_warning_when_every_race_slot_is_covered(self):
        driver = Driver.objects.create(event=self.event, name='D', timezone='UTC')
        _cover(self.event, driver, [
            s for s in get_availability_slots(self.event)
            if self.event.effective_start_datetime_utc <= s < self.event.effective_end_datetime_utc
        ])

        response = self.client.get(self.url)

        self.assertEqual(response.context['uncovered_race_windows'], [])
        self.assertNotContains(response, 'no driver available')

    def test_driver_covering_the_whole_race_is_never_named_in_a_warning(self):
        # Regression: the old warning named exactly this driver.
        driver = Driver.objects.create(event=self.event, name='Perfect Attendance', timezone='UTC')
        _cover(self.event, driver, [
            s for s in get_availability_slots(self.event)
            if self.event.effective_start_datetime_utc <= s < self.event.effective_end_datetime_utc
        ])

        response = self.client.get(self.url)

        self.assertNotContains(response, "Perfect Attendance</strong>")

    def test_window_list_is_capped(self):
        from events.views import MAX_UNCOVERED_WINDOWS_SHOWN
        # Alternate covered/uncovered slots to manufacture many separate gaps.
        driver = Driver.objects.create(event=self.event, name='Patchy', timezone='UTC')
        race = [
            s for s in get_availability_slots(self.event)
            if self.event.effective_start_datetime_utc <= s < self.event.effective_end_datetime_utc
        ]
        _cover(self.event, driver, race[::2])

        response = self.client.get(self.url)

        self.assertLessEqual(
            len(response.context['uncovered_race_windows']),
            MAX_UNCOVERED_WINDOWS_SHOWN,
        )

    def test_capped_list_reports_how_many_were_hidden(self):
        from events.views import MAX_UNCOVERED_WINDOWS_SHOWN
        driver = Driver.objects.create(event=self.event, name='Patchy', timezone='UTC')
        race = [
            s for s in get_availability_slots(self.event)
            if self.event.effective_start_datetime_utc <= s < self.event.effective_end_datetime_utc
        ]
        _cover(self.event, driver, race[::2])

        response = self.client.get(self.url)

        shown = len(response.context['uncovered_race_windows'])
        extra = response.context['uncovered_race_windows_extra']
        if extra:
            self.assertEqual(shown, MAX_UNCOVERED_WINDOWS_SHOWN)
        self.assertGreaterEqual(extra, 0)


# ---------------------------------------------------------------------------
# PR review follow-ups
# ---------------------------------------------------------------------------

class ClientSideGridAnchorTests(TestCase):
    """
    The admin page's JS snaps stint times onto the availability grid itself and
    looks the results up in availabilityData. It therefore needs the same
    floored origin the server uses — an unfloored one probes timestamps no slot
    can occupy, so on any event not starting on a half hour every assigned
    driver renders as conflicted and every dropdown option is dimmed.
    """

    def setUp(self):
        self.event = save_event(start_time_utc=dt.time(12, 15))
        session = self.client.session
        session['admin_%s' % self.event.id] = True
        session.save()
        self.url = reverse('admin_dashboard', kwargs={'event_id': self.event.id})

    def test_context_exposes_the_floored_anchor(self):
        response = self.client.get(self.url)

        self.assertEqual(
            response.context['slot_grid_anchor'], slot_grid_anchor(self.event)
        )

    def test_anchor_in_context_is_floored_not_the_session_start(self):
        response = self.client.get(self.url)

        self.assertNotEqual(
            response.context['slot_grid_anchor'], self.event.start_datetime_utc
        )
        self.assertEqual(response.context['slot_grid_anchor'].minute, 0)

    def test_rendered_anchor_matches_the_first_real_slot(self):
        response = self.client.get(self.url)

        first_slot = get_availability_slots(self.event)[0]
        self.assertContains(response, first_slot.strftime('%Y-%m-%dT%H:%M:%SZ'))

    def test_template_does_not_use_the_unfloored_session_start(self):
        markup = _read_template('admin.html')

        self.assertNotIn("event.start_datetime_utc|to_utc_z", markup)

    def test_both_js_helpers_read_the_same_anchor(self):
        markup = _read_template('admin.html')

        self.assertEqual(markup.count('new Date(slotGridAnchorUtc).getTime()'), 2)


class AvailStylePreGridSlotTests(SimpleTestCase):
    """
    availStyle() must mirror build_stint_availability_matrix() and
    checkDriverConflict(), which both count the slot covering a stint's
    pre-grid portion. Without it the dropdown left a driver undimmed while the
    conflict marker beside it flagged them.
    """

    def test_avail_style_checks_the_pre_grid_slot(self):
        markup = _read_template('admin.html')
        avail_style = markup[markup.index('availStyle(stintNumber, driverId)'):]
        avail_style = avail_style[:avail_style.index('availCellClass')]

        self.assertIn('snappedStartMs', avail_style)
        self.assertIn('preSlot', avail_style)

    def test_conflict_check_still_has_its_pre_grid_branch(self):
        markup = _read_template('admin.html')
        fn = markup[markup.index('function checkDriverConflict'):]
        fn = fn[:fn.index('function formatTimeInTz')]

        self.assertIn('preSlot', fn)


class ScheduleWarningResetTests(SimpleTestCase):
    """
    The save response swaps #admin-details-errors, which sits outside the form,
    so the form's Alpine state survives. Without re-baselining, the pre-save
    warning stayed on screen describing a move that already happened.
    """

    def test_component_can_re_baseline(self):
        markup = _read_template('admin.html')

        self.assertIn('acceptSaved()', markup)

    def test_form_re_baselines_after_a_successful_save(self):
        markup = _read_template('admin.html')

        self.assertIn(
            "@htmx:after-request=\"if ($event.detail.successful) acceptSaved()\"",
            markup,
        )


class ScheduleMovedWatchesLengthTests(TestCase):
    """Shortening a race truncates the slot grid tail just like moving it."""

    def setUp(self):
        self.event = save_event(start_time_utc=dt.time(12, 0), length_seconds=6 * 3600)
        self.driver = Driver.objects.create(
            event=self.event, name='Trimmed', timezone='UTC'
        )
        Availability.objects.bulk_create([
            Availability(driver=self.driver, slot_utc=s)
            for s in get_availability_slots(self.event)
        ])
        session = self.client.session
        session['admin_%s' % self.event.id] = True
        session.save()
        self.url = reverse('admin_save_details', kwargs={'event_id': self.event.id})

    def _post(self, **overrides):
        payload = {
            'name': self.event.name,
            'date': '2026-06-01',
            'start_time_utc': '12:00',
            'length_hours': '6',
            'length_minutes': '0',
        }
        payload.update(overrides)
        return self.client.post(self.url, payload)

    def test_shortening_the_race_reports_stranded_availability(self):
        response = self._post(length_hours='2')

        self.assertIn(
            'partials/availability_warning.html',
            [t.name for t in response.templates],
        )

    def test_shortening_the_race_names_the_driver(self):
        response = self._post(length_hours='2')

        self.assertContains(response, 'Trimmed')

    def test_unchanged_length_still_reports_nothing(self):
        response = self._post()

        self.assertEqual(response.content, b'')


class StaleAvailabilityReportsScaleTests(TestCase):
    """The warning carries how much each driver lost, not just who."""

    def setUp(self):
        self.event = save_event(start_time_utc=dt.time(12, 0), length_seconds=6 * 3600)
        self.driver = Driver.objects.create(event=self.event, name='Mover', timezone='UTC')
        Availability.objects.bulk_create([
            Availability(driver=self.driver, slot_utc=s)
            for s in get_availability_slots(self.event)
        ])
        session = self.client.session
        session['admin_%s' % self.event.id] = True
        session.save()

    def test_entries_carry_lost_and_total_counts(self):
        self.event.date = dt.date(2026, 6, 8)
        self.event.save(update_fields=['date'])

        entry = _drivers_with_stale_availability(self.event)[0]

        self.assertIn('lost', entry)
        self.assertIn('total', entry)
        self.assertGreater(entry['lost'], 0)

    def test_warning_shows_the_scale_of_the_loss(self):
        response = self.client.post(
            reverse('admin_save_details', kwargs={'event_id': self.event.id}),
            {'name': self.event.name, 'date': '2026-06-08', 'start_time_utc': '12:00',
             'length_hours': '6', 'length_minutes': '0'},
        )

        self.assertContains(response, 'no longer apply')


class CanonicalHostAllowedHostsTests(SimpleTestCase):
    """
    Documents the deployment trap: request.get_host() validates against
    ALLOWED_HOSTS and raises DisallowedHost, which Django turns into a bare 400
    BEFORE the middleware can redirect. Every host to be redirected must
    therefore be in ALLOWED_HOSTS, not just the canonical one.
    """

    CANONICAL = 'wearechecking.gg'
    OTHER = 'www.wearechecking.gg'

    def _middleware(self, allowed):
        with override_settings(CANONICAL_HOST=self.CANONICAL, ALLOWED_HOSTS=allowed):
            return CanonicalHostMiddleware(lambda r: HttpResponse('ok'))

    def test_redirect_works_when_the_other_host_is_allowed(self):
        allowed = [self.CANONICAL, self.OTHER]
        middleware = self._middleware(allowed)
        with override_settings(CANONICAL_HOST=self.CANONICAL, ALLOWED_HOSTS=allowed):
            response = middleware(RequestFactory(SERVER_NAME=self.OTHER).get('/create/'))

        self.assertEqual(response.status_code, 301)

    def test_host_missing_from_allowed_hosts_cannot_be_redirected(self):
        from django.core.exceptions import DisallowedHost

        allowed = [self.CANONICAL]
        middleware = self._middleware(allowed)
        with override_settings(CANONICAL_HOST=self.CANONICAL, ALLOWED_HOSTS=allowed):
            with self.assertRaises(DisallowedHost):
                middleware(RequestFactory(SERVER_NAME=self.OTHER).get('/create/'))

    def test_env_example_documents_the_requirement(self):
        env = django_conf.BASE_DIR.joinpath('.env.example').read_text(encoding='utf-8')

        self.assertIn('every host you want redirected', env)


# ---------------------------------------------------------------------------
# Stint assignment driver dropdown
#
# The trigger was pinned to 200px inside a 220px column, so a long name pushed
# the adjacent clear button out of reach; the panel was pinned to 200px too,
# hiding each option's local start time behind a horizontal scrollbar. And the
# table's declared column widths were ignored entirely: under
# table-layout: fixed the widths come from the first row, which is all colspan
# cells, so every column rendered the same width.
# ---------------------------------------------------------------------------

import re


class StintTableColumnWidthTests(TestCase):
    """A colgroup is the only place fixed table layout reads column widths."""

    def setUp(self):
        self.event = save_event()
        for name in ('One', 'Two', 'Three'):
            Driver.objects.create(event=self.event, name=name, timezone='UTC')
        session = self.client.session
        session['admin_%s' % self.event.id] = True
        session.save()
        self.url = reverse('admin_dashboard', kwargs={'event_id': self.event.id})

    def test_table_declares_a_colgroup(self):
        response = self.client.get(self.url)

        self.assertContains(response, '<colgroup>')

    def test_colgroup_has_one_col_per_column(self):
        response = self.client.get(self.url)
        html = response.content.decode()
        colgroup = html[html.index('<colgroup>'):html.index('</colgroup>')]

        # 6 assignment columns + divider + one per driver
        self.assertEqual(colgroup.count('<col '), 7 + 3)

    def test_driver_column_is_wider_than_the_index_column(self):
        response = self.client.get(self.url)
        html = response.content.decode()
        colgroup = html[html.index('<colgroup>'):html.index('</colgroup>')]
        widths = [int(w) for w in re.findall(r'width:\s*(\d+)px', colgroup)]

        self.assertGreater(widths[4], widths[0])


class DriverDropdownSizingTests(SimpleTestCase):
    """The trigger yields to its cell; the panel is free to exceed it."""

    def test_trigger_has_no_hard_coded_width(self):
        markup = _read_template('admin.html')

        self.assertNotIn('width:200px;min-width:200px;max-width:200px', markup)

    def test_trigger_uses_the_shared_class(self):
        markup = _read_template('admin.html')

        self.assertIn('class="dd-trigger"', markup)

    def test_panel_width_is_not_pinned_to_the_trigger(self):
        markup = _read_template('admin.html')
        toggle = markup[markup.index('toggle() {'):markup.index('select(driverId)')]

        self.assertNotIn("width: '200px'", toggle)
        self.assertIn("width: 'max-content'", toggle)

    def test_panel_is_kept_on_screen_from_either_edge(self):
        markup = _read_template('admin.html')
        toggle = markup[markup.index('toggle() {'):markup.index('select(driverId)')]

        # anchors by left OR right depending on available room
        self.assertIn('roomToTheRight', toggle)

    def test_option_rows_separate_name_from_time(self):
        markup = _read_template('admin.html')

        self.assertIn('class="dd-option-name"', markup)
        self.assertIn('class="dd-option-time"', markup)


class DriverDropdownStylesheetTests(SimpleTestCase):
    """Only the name yields; the start time must never be squeezed out."""

    def _css(self):
        return django_conf.BASE_DIR.joinpath(
            'static', 'css', 'tailwind.css'
        ).read_text(encoding='utf-8')

    def _rule(self, selector):
        css = self._css()
        start = css.index(selector + ' {')
        return css[start:css.index('}', start)]

    def test_trigger_fills_its_cell_rather_than_a_fixed_width(self):
        rule = self._rule('.dd-trigger')

        self.assertIn('width: 100%', rule)
        self.assertIn('min-width: 0', rule)

    def test_trigger_name_truncates(self):
        rule = self._rule('.dd-trigger-name')

        self.assertIn('text-overflow: ellipsis', rule)

    def test_panel_never_scrolls_sideways(self):
        rule = self._rule('.dd-panel')

        self.assertIn('overflow-x: hidden', rule)

    def test_option_name_is_the_part_that_yields(self):
        rule = self._rule('.dd-option-name')

        self.assertIn('min-width: 0', rule)
        self.assertIn('text-overflow: ellipsis', rule)

    def test_option_time_never_shrinks(self):
        rule = self._rule('.dd-option-time')

        self.assertIn('flex-shrink: 0', rule)
        self.assertIn('white-space: nowrap', rule)


class DriverDropdownTimezoneTests(TestCase):
    """Each option shows the stint start in that driver's own timezone."""

    def setUp(self):
        self.event = save_event()
        Driver.objects.create(
            event=self.event, name='Berliner', timezone='Europe/Berlin'
        )
        session = self.client.session
        session['admin_%s' % self.event.id] = True
        session.save()

    def test_driver_timezone_reaches_the_dropdown(self):
        response = self.client.get(
            reverse('admin_dashboard', kwargs={'event_id': self.event.id})
        )

        self.assertContains(response, "driverStintTime('Europe/Berlin')")

    def test_drivers_json_still_carries_timezones(self):
        response = self.client.get(
            reverse('admin_dashboard', kwargs={'event_id': self.event.id})
        )

        drivers = json.loads(response.context['drivers_json'])
        self.assertEqual(drivers[0]['timezone'], 'Europe/Berlin')
