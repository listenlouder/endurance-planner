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
"""

import datetime as dt
from datetime import timezone

from django.test import SimpleTestCase, TestCase

from .forms import EventCreateForm
from .models import Availability, Driver, Event
from .templatetags.tz_filters import (
    datetime_in_tz,
    time_in_tz,
    to_tz,
    to_utc_z,
)
from .utils import (
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
        self.assertEqual(start, event.start_datetime_utc)

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
        form = EventCreateForm(data=self._valid_data(length_hours=0))
        self.assertFalse(form.is_valid())
        self.assertIn('length_hours', form.errors)

    def test_length_hours_at_minimum_accepted(self):
        form = EventCreateForm(data=self._valid_data(length_hours=1))
        self.assertTrue(form.is_valid(), form.errors)

    def test_length_hours_above_maximum_rejected(self):
        form = EventCreateForm(data=self._valid_data(length_hours=169))
        self.assertFalse(form.is_valid())
        self.assertIn('length_hours', form.errors)

    def test_length_hours_at_maximum_accepted(self):
        form = EventCreateForm(data=self._valid_data(length_hours=168))
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

    def test_very_long_name_passes_validation(self):
        # _validate_signup_post does not enforce max length; the model field does
        long_name = 'X' * 500
        cleaned, errors = _validate_signup_post(self._valid_post(driver_name=long_name))
        self.assertEqual(errors, {})
        self.assertEqual(cleaned['driver_name'], long_name)

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
        error = self._call('avg_lap_seconds', '95.5')
        self.assertIsNone(error)
        self._refresh()
        self.assertAlmostEqual(self.event.avg_lap_seconds, 95.5)

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
        error = self._call('avg_lap_seconds', 'fast')
        self.assertIsNotNone(error)
        self.assertIn('valid number', error.lower())

    def test_required_number_empty_returns_error(self):
        # 'length_hours' is required
        error = self._call('length_hours', '')
        self.assertIsNotNone(error)
        self.assertIn('required', error.lower())

    def test_optional_number_empty_sets_none(self):
        # 'avg_lap_seconds' is optional
        error = self._call('avg_lap_seconds', '')
        self.assertIsNone(error)
        self._refresh()
        self.assertIsNone(self.event.avg_lap_seconds)

    def test_number_below_min_returns_error(self):
        # 'avg_lap_seconds' min=1
        error = self._call('avg_lap_seconds', '0.5')
        self.assertIsNotNone(error)
        self.assertIn('at least', error.lower())

    def test_number_at_min_is_accepted(self):
        error = self._call('avg_lap_seconds', '1')
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
        # value_str is stripped before parsing
        error = self._call('avg_lap_seconds', '  100  ')
        self.assertIsNone(error)
        self._refresh()
        self.assertAlmostEqual(self.event.avg_lap_seconds, 100.0)
