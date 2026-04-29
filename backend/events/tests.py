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
import uuid
from datetime import timezone
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .forms import EventCreateForm
from .models import Availability, Driver, Event, Feedback, StintAssignment
from .templatetags.tz_filters import (
    datetime_in_tz,
    dict_get,
    get_item,
    seconds_to_hours_display,
    time_in_tz,
    to_tz,
    to_utc_z,
)
from .utils import (
    build_stint_availability_matrix,
    check_driver_conflict,
    get_availability_slots,
    get_stint_windows,
    stint_end_time,
    stint_length_seconds,
    stint_start_time,
    total_stints,
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


class StintStartTimeTests(SimpleTestCase):
    """Tests for utils.stint_start_time()."""

    def test_stint_1_starts_at_event_start(self):
        event = make_event()
        start = stint_start_time(event, 1)
        self.assertEqual(start, event.effective_start_datetime_utc)

    def test_stint_1_is_utc_aware(self):
        event = make_event()
        start = stint_start_time(event, 1)
        self.assertIsNotNone(start.tzinfo)
        self.assertEqual(start.utcoffset(), dt.timedelta(0))

    def test_stint_2_offset_by_one_stint(self):
        event = make_event()
        sl = stint_length_seconds(event)
        expected = event.start_datetime_utc + dt.timedelta(seconds=sl)
        self.assertEqual(stint_start_time(event, 2), expected)

    def test_stint_3_offset_by_two_stints(self):
        event = make_event()
        sl = stint_length_seconds(event)
        expected = event.start_datetime_utc + dt.timedelta(seconds=2 * sl)
        self.assertEqual(stint_start_time(event, 3), expected)

    def test_start_times_are_equally_spaced(self):
        event = make_event()
        sl = stint_length_seconds(event)
        n = total_stints(event)
        for i in range(2, n + 1):
            gap = stint_start_time(event, i) - stint_start_time(event, i - 1)
            self.assertAlmostEqual(gap.total_seconds(), sl, places=3)

    def test_start_time_midnight_utc(self):
        event = make_event(date=dt.date(2026, 1, 1), start_time_utc=dt.time(0, 0, 0))
        start = stint_start_time(event, 1)
        self.assertEqual(start, utc(2026, 1, 1, 0, 0, 0))

    def test_start_time_crosses_midnight(self):
        # Start at 23:00, stint = 3615 s (60 min 15 s) → stint 2 crosses midnight
        event = make_event(date=dt.date(2026, 6, 1), start_time_utc=dt.time(23, 0, 0))
        s2 = stint_start_time(event, 2)
        self.assertEqual(s2.date(), dt.date(2026, 6, 2))


class StintEndTimeTests(SimpleTestCase):
    """Tests for utils.stint_end_time()."""

    def test_intermediate_stint_ends_at_next_start(self):
        event = make_event()
        n = total_stints(event)
        for i in range(1, n):  # all but last
            self.assertEqual(stint_end_time(event, i), stint_start_time(event, i + 1))

    def test_final_stint_ends_at_event_end(self):
        event = make_event()
        n = total_stints(event)
        self.assertEqual(stint_end_time(event, n), event.end_datetime_utc)

    def test_last_stint_may_be_shorter_than_others(self):
        # When race doesn't divide evenly the final stint is truncated
        event = make_event()   # 21600 / 3615 = 5.976 → last stint is partial
        n = total_stints(event)
        sl = stint_length_seconds(event)
        last_duration = (stint_end_time(event, n) - stint_start_time(event, n)).total_seconds()
        # The last stint duration must be ≤ full stint length
        self.assertLessEqual(last_duration, sl)
        # And it must be positive
        self.assertGreater(last_duration, 0)

    def test_when_race_divides_evenly_all_stints_equal_length(self):
        event = make_event(
            avg_lap_seconds=120.0, in_lap_seconds=120.0, out_lap_seconds=120.0,
            target_laps=30, length_seconds=7_200,   # exactly 2 stints of 3600 s
        )
        n = total_stints(event)
        sl = stint_length_seconds(event)
        for i in range(1, n + 1):
            duration = (stint_end_time(event, i) - stint_start_time(event, i)).total_seconds()
            self.assertAlmostEqual(duration, sl, places=3)

    def test_end_time_is_utc_aware(self):
        event = make_event()
        end = stint_end_time(event, 1)
        self.assertIsNotNone(end.tzinfo)

    def test_stint_end_time_for_number_beyond_total_treated_as_final(self):
        # Calling with number >= total_stints returns event.end_datetime_utc
        event = make_event()
        n = total_stints(event)
        self.assertEqual(stint_end_time(event, n + 5), event.end_datetime_utc)


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

    def test_last_window_ends_at_event_end(self):
        event = make_event()
        windows = get_stint_windows(event)
        self.assertEqual(windows[-1]['end_utc'], event.end_datetime_utc)

    def test_consecutive_windows_are_contiguous(self):
        # end of window N == start of window N+1
        event = make_event()
        windows = get_stint_windows(event)
        for i in range(len(windows) - 1):
            self.assertEqual(windows[i]['end_utc'], windows[i + 1]['start_utc'])

    def test_start_utc_matches_stint_start_time_helper(self):
        event = make_event()
        windows = get_stint_windows(event)
        for w in windows:
            expected = stint_start_time(event, w['stint_number'])
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
        # 6 hours = 12 slots (00:00, 00:30, …, 05:30)
        event = make_event(length_seconds=21_600)
        slots = get_availability_slots(event)
        self.assertEqual(len(slots), 12)

    def test_slot_count_for_1_hour_race(self):
        event = make_event(length_seconds=3_600)
        slots = get_availability_slots(event)
        self.assertEqual(len(slots), 2)

    def test_last_slot_is_before_event_end(self):
        event = make_event()
        slots = get_availability_slots(event)
        self.assertLess(slots[-1], event.end_datetime_utc)

    def test_no_slot_at_or_after_event_end(self):
        event = make_event()
        slots = get_availability_slots(event)
        for slot in slots:
            self.assertLess(slot, event.end_datetime_utc)

    def test_all_slots_are_utc_aware(self):
        event = make_event()
        for slot in get_availability_slots(event):
            self.assertIsNotNone(slot.tzinfo)
            self.assertEqual(slot.utcoffset(), dt.timedelta(0))

    def test_24_hour_race_has_48_slots(self):
        event = make_event(length_seconds=86_400)
        slots = get_availability_slots(event)
        self.assertEqual(len(slots), 48)

    def test_45_minute_race_has_two_slots(self):
        # 45 min (2700 s): loop adds slot at +0 min (< 45 min end) and +30 min
        # (also < 45 min end), but stops before +60 min. So 2 slots total.
        event = make_event(length_seconds=2_700)
        slots = get_availability_slots(event)
        self.assertEqual(len(slots), 2)
        self.assertEqual(slots[0], event.start_datetime_utc)
        self.assertEqual(slots[1], event.start_datetime_utc + dt.timedelta(minutes=30))

    def test_30_minute_race_has_one_slot(self):
        # 30 min (1800 s): slot at +0 min satisfies current < end,
        # but slot at +30 min equals end so the loop stops. Exactly 1 slot.
        event = make_event(length_seconds=1_800)
        slots = get_availability_slots(event)
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0], event.start_datetime_utc)


class CheckDriverConflictTests(TestCase):
    """Tests for utils.check_driver_conflict().

    Requires the database because Availability uses a ForeignKey through Driver.
    """

    def setUp(self):
        self.event = save_event()
        self.driver = Driver.objects.create(
            event=self.event,
            name='Alice',
            timezone='UTC',
        )

    def _add_slots(self, start_utc, count):
        """Add `count` consecutive 30-min availability slots for self.driver."""
        for i in range(count):
            slot = start_utc + dt.timedelta(minutes=30 * i)
            Availability.objects.create(driver=self.driver, slot_utc=slot)

    def _make_window(self, start_utc, end_utc):
        return {'start_utc': start_utc, 'end_utc': end_utc}

    # --- No conflict scenarios ---

    def test_full_coverage_no_conflict(self):
        # Driver covers every 30-min slot in the window
        start = utc(2026, 6, 1, 12, 0)
        end = utc(2026, 6, 1, 14, 0)   # 2-hour window → 4 slots
        self._add_slots(start, 4)
        window = self._make_window(start, end)
        self.assertFalse(check_driver_conflict(self.driver, window))

    def test_single_slot_window_driver_available(self):
        slot = utc(2026, 6, 1, 12, 0)
        Availability.objects.create(driver=self.driver, slot_utc=slot)
        window = self._make_window(slot, slot + dt.timedelta(minutes=30))
        self.assertFalse(check_driver_conflict(self.driver, window))

    # --- Conflict scenarios ---

    def test_no_availability_at_all_is_conflict(self):
        window = self._make_window(
            utc(2026, 6, 1, 12, 0),
            utc(2026, 6, 1, 13, 0),
        )
        self.assertTrue(check_driver_conflict(self.driver, window))

    def test_partial_coverage_is_conflict(self):
        # Driver has first slot but not second
        start = utc(2026, 6, 1, 12, 0)
        Availability.objects.create(driver=self.driver, slot_utc=start)
        window = self._make_window(start, start + dt.timedelta(hours=1))
        self.assertTrue(check_driver_conflict(self.driver, window))

    def test_gap_in_middle_is_conflict(self):
        # Slots at 12:00, 13:00 but missing 12:30
        Availability.objects.create(driver=self.driver, slot_utc=utc(2026, 6, 1, 12, 0))
        Availability.objects.create(driver=self.driver, slot_utc=utc(2026, 6, 1, 13, 0))
        window = self._make_window(
            utc(2026, 6, 1, 12, 0),
            utc(2026, 6, 1, 13, 30),
        )
        self.assertTrue(check_driver_conflict(self.driver, window))

    def test_availability_outside_window_does_not_help(self):
        # Driver available from 14:00 onwards, but window is 12:00–13:00
        self._add_slots(utc(2026, 6, 1, 14, 0), 4)
        window = self._make_window(
            utc(2026, 6, 1, 12, 0),
            utc(2026, 6, 1, 13, 0),
        )
        self.assertTrue(check_driver_conflict(self.driver, window))

    def test_coverage_extends_beyond_window_still_no_conflict(self):
        # Driver covers more slots than the window requires
        start = utc(2026, 6, 1, 12, 0)
        self._add_slots(start, 10)   # 5 hours of availability
        window = self._make_window(start, start + dt.timedelta(hours=1))
        self.assertFalse(check_driver_conflict(self.driver, window))

    def test_different_driver_availability_does_not_affect_result(self):
        # A second driver is available; self.driver is not
        other = Driver.objects.create(event=self.event, name='Bob', timezone='UTC')
        self._add_slots(utc(2026, 6, 1, 12, 0), 4)  # slots for self.driver
        window = self._make_window(
            utc(2026, 6, 1, 12, 0),
            utc(2026, 6, 1, 14, 0),
        )
        # Check conflict for `other` — he has no availability
        self.assertTrue(check_driver_conflict(other, window))
        # self.driver still has no conflict
        self.assertFalse(check_driver_conflict(self.driver, window))


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

class ToTzFilterTests(SimpleTestCase):
    """Tests for templatetags.tz_filters.to_tz."""

    def _utc_dt(self, hour=12, minute=0):
        return dt.datetime(2026, 6, 15, hour, minute, 0, tzinfo=timezone.utc)

    def test_utc_to_eastern_standard(self):
        # UTC 12:00 → America/New_York is EDT (UTC-4) in June → 08:00
        result = to_tz(self._utc_dt(12), 'America/New_York')
        self.assertEqual(result.hour, 8)
        self.assertEqual(result.minute, 0)

    def test_utc_to_pacific_standard(self):
        # UTC 12:00 → America/Los_Angeles is PDT (UTC-7) in June → 05:00
        result = to_tz(self._utc_dt(12), 'America/Los_Angeles')
        self.assertEqual(result.hour, 5)

    def test_utc_to_london_bst(self):
        # UTC 12:00 → Europe/London is BST (UTC+1) in June → 13:00
        result = to_tz(self._utc_dt(12), 'Europe/London')
        self.assertEqual(result.hour, 13)

    def test_utc_to_tokyo(self):
        # UTC 12:00 → Asia/Tokyo is JST (UTC+9) always → 21:00
        result = to_tz(self._utc_dt(12), 'Asia/Tokyo')
        self.assertEqual(result.hour, 21)

    def test_utc_to_utc_unchanged(self):
        original = self._utc_dt(15, 30)
        result = to_tz(original, 'UTC')
        self.assertEqual(result.hour, 15)
        self.assertEqual(result.minute, 30)

    def test_invalid_timezone_returns_original_datetime(self):
        original = self._utc_dt(10)
        result = to_tz(original, 'Not/ATimezone')
        # Falls back to returning the original dt unchanged
        self.assertEqual(result, original)

    def test_result_is_timezone_aware(self):
        result = to_tz(self._utc_dt(12), 'America/New_York')
        self.assertIsNotNone(result.tzinfo)

    def test_dst_transition_march_us(self):
        # 2026-03-08 06:30 UTC → before and after DST transition in New York
        # America/New_York goes to EDT at 02:00 local on 2026-03-08
        # 06:30 UTC = 01:30 EST (before switch) - but the switch happens at 2am local
        # Actually: DST 2026 is March 8. At 2am EST = 7am UTC.
        # So 6:30 UTC = 1:30 AM EST (still standard time)
        before_dst = dt.datetime(2026, 3, 8, 6, 30, 0, tzinfo=timezone.utc)
        result = to_tz(before_dst, 'America/New_York')
        # 6:30 UTC - 5h = 1:30 EST
        self.assertEqual(result.hour, 1)
        self.assertEqual(result.minute, 30)

    def test_dst_transition_march_us_after(self):
        # 8:00 UTC on March 8 = 4:00 AM EDT (after DST switch at 7 AM UTC)
        after_dst = dt.datetime(2026, 3, 8, 8, 0, 0, tzinfo=timezone.utc)
        result = to_tz(after_dst, 'America/New_York')
        self.assertEqual(result.hour, 4)


class DatetimeInTzFilterTests(SimpleTestCase):
    """Tests for templatetags.tz_filters.datetime_in_tz."""

    def test_output_format_eastern(self):
        # 2026-06-15 16:00 UTC → America/New_York EDT (UTC-4) → 12:00
        source = dt.datetime(2026, 6, 15, 16, 0, 0, tzinfo=timezone.utc)
        result = datetime_in_tz(source, 'America/New_York')
        # Expected format: "Jun 15 2026, 12:00"
        self.assertEqual(result, 'Jun 15 2026, 12:00')

    def test_output_format_utc(self):
        source = dt.datetime(2026, 3, 1, 9, 5, 0, tzinfo=timezone.utc)
        result = datetime_in_tz(source, 'UTC')
        # Day 1 with no leading zero: "Mar 1 2026, 09:05"
        self.assertEqual(result, 'Mar 1 2026, 09:05')

    def test_no_leading_zero_on_day(self):
        source = dt.datetime(2026, 4, 5, 14, 0, 0, tzinfo=timezone.utc)
        result = datetime_in_tz(source, 'UTC')
        self.assertIn('Apr 5 ', result)
        # Should NOT have 'Apr 05'
        self.assertNotIn('Apr 05', result)

    def test_double_digit_day(self):
        source = dt.datetime(2026, 11, 23, 10, 0, 0, tzinfo=timezone.utc)
        result = datetime_in_tz(source, 'UTC')
        self.assertIn('Nov 23 ', result)

    def test_invalid_timezone_returns_str_of_dt(self):
        source = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = datetime_in_tz(source, 'Bad/Zone')
        # Falls back to str(dt)
        self.assertEqual(result, str(source))

    def test_tokyo_conversion(self):
        # UTC 15:00 → JST 00:00 next day
        source = dt.datetime(2026, 6, 15, 15, 0, 0, tzinfo=timezone.utc)
        result = datetime_in_tz(source, 'Asia/Tokyo')
        self.assertEqual(result, 'Jun 16 2026, 00:00')

    def test_contains_correct_year(self):
        source = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        result = datetime_in_tz(source, 'UTC')
        self.assertIn('2026', result)


class TimeInTzFilterTests(SimpleTestCase):
    """Tests for templatetags.tz_filters.time_in_tz."""

    def test_utc_to_eastern_time_portion(self):
        # UTC 12:00 → America/New_York EDT (UTC-4) → 08:00
        source = dt.datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = time_in_tz(source, 'America/New_York')
        self.assertEqual(result, '08:00')

    def test_utc_to_tokyo_time_portion(self):
        source = dt.datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = time_in_tz(source, 'Asia/Tokyo')
        self.assertEqual(result, '21:00')

    def test_output_format_is_hhmm(self):
        source = dt.datetime(2026, 6, 15, 9, 5, 0, tzinfo=timezone.utc)
        result = time_in_tz(source, 'UTC')
        self.assertEqual(result, '09:05')

    def test_midnight_utc(self):
        source = dt.datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        result = time_in_tz(source, 'UTC')
        self.assertEqual(result, '00:00')

    def test_invalid_timezone_returns_utc_time(self):
        source = dt.datetime(2026, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        result = time_in_tz(source, 'Garbage/Zone')
        # Falls back to dt.strftime('%H:%M') of the original UTC dt
        self.assertEqual(result, '14:30')

    def test_london_bst_summer(self):
        # UTC 12:00 → Europe/London BST (UTC+1) → 13:00
        source = dt.datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = time_in_tz(source, 'Europe/London')
        self.assertEqual(result, '13:00')

    def test_london_gmt_winter(self):
        # UTC 12:00 → Europe/London GMT (UTC+0) in January → 12:00
        source = dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = time_in_tz(source, 'Europe/London')
        self.assertEqual(result, '12:00')


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
        self.assertEqual(response['HX-Trigger'], 'feedbackSuccess')

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
        # 60 s = 0 h 1 m
        self.assertEqual(seconds_to_hours_display(60), '0h 1m')

    def test_fifty_nine_minutes(self):
        # 3540 s = 0 h 59 m
        self.assertEqual(seconds_to_hours_display(3540), '0h 59m')

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

class GetItemFilterTests(SimpleTestCase):
    """Tests for templatetags.tz_filters.get_item.

    get_item is a simple dict.get() wrapper for template use.
    Unlike dict_get it does NOT fall back to str(key) coercion.
    """

    def test_string_key_present_returns_value(self):
        self.assertEqual(get_item({'a': 1}, 'a'), 1)

    def test_string_key_absent_returns_none(self):
        self.assertIsNone(get_item({'a': 1}, 'b'))

    def test_integer_key_present_returns_value(self):
        self.assertEqual(get_item({1: 'one'}, 1), 'one')

    def test_key_with_falsy_value_returns_falsy_value(self):
        # Must distinguish "missing key" from "key with falsy value"
        self.assertEqual(get_item({'x': 0}, 'x'), 0)
        self.assertEqual(get_item({'x': False}, 'x'), False)
        self.assertEqual(get_item({'x': ''}, 'x'), '')

    def test_empty_dict_returns_none(self):
        self.assertIsNone(get_item({}, 'anything'))


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

        self.assertEqual(response['HX-Trigger'], 'show-toast')

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
        self.assertIn('partials/admin_details_errors.html', template_names)

    def test_invalid_date_format_returns_error_partial(self):
        self._set_admin_session()

        response = self.client.post(self.url, self._valid_post(date='not-a-date'))

        self.assertEqual(response.status_code, 422)
        self.assertNotIn('HX-Trigger', response)
        template_names = [t.name for t in response.templates]
        self.assertIn('partials/admin_details_errors.html', template_names)

    def test_invalid_start_time_returns_error_partial(self):
        self._set_admin_session()

        response = self.client.post(self.url, self._valid_post(start_time_utc='25:99'))

        self.assertEqual(response.status_code, 422)
        self.assertNotIn('HX-Trigger', response)
        template_names = [t.name for t in response.templates]
        self.assertIn('partials/admin_details_errors.html', template_names)

    def test_zero_length_race_returns_error_partial(self):
        self._set_admin_session()

        response = self.client.post(
            self.url,
            self._valid_post(length_hours='0', length_minutes='0'),
        )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn('HX-Trigger', response)
        template_names = [t.name for t in response.templates]
        self.assertIn('partials/admin_details_errors.html', template_names)

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

        self.assertEqual(response['HX-Trigger'], 'show-toast')
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

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('HX-Trigger', response)
        template_names = [t.name for t in response.templates]
        self.assertIn('partials/admin_calc_errors.html', template_names)

    def test_mmss_with_seconds_gte_60_returns_error_partial(self):
        self._set_admin_session()

        response = self.client.post(self.url, {'avg_lap': '2:60'})

        self.assertEqual(response.status_code, 200)
        template_names = [t.name for t in response.templates]
        self.assertIn('partials/admin_calc_errors.html', template_names)

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

        self.assertEqual(response.status_code, 200)
        template_names = [t.name for t in response.templates]
        self.assertIn('partials/admin_calc_errors.html', template_names)


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

        self.assertEqual(response['HX-Trigger'], 'show-toast')

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

    def test_slot_count_is_based_on_session_start_and_end(self):
        # 6-hour race starting at 10:00 → end_datetime is 16:00
        # Slots: 10:00, 10:30, ..., 15:30 = 12 slots (based on start_datetime_utc)
        event = make_event(
            date=dt.date(2026, 6, 1),
            start_time_utc=dt.time(10, 0, 0),
            race_start_time_utc=dt.time(12, 0, 0),
            length_seconds=21_600,
        )
        slots = get_availability_slots(event)
        self.assertEqual(len(slots), 12)


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
        self.assertIn('partials/admin_calc_errors.html', template_names)

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

        self.assertEqual(response['HX-Trigger'], 'show-toast')


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
