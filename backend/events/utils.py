import math
from datetime import timedelta


def stint_length_seconds(event):
    """
    Calculate the length of a single stint in seconds.

    Formula: (avg_lap * target_laps) + (in_lap + out_lap - (avg_lap * 2))

    The in_lap and out_lap replace the first and last racing laps of the stint,
    so we subtract two average laps and add the actual in/out lap times.

    Requires event.has_required_stint_fields to be True.
    Returns float (seconds).
    """
    racing_time = event.avg_lap_seconds * event.target_laps
    transition_delta = (event.in_lap_seconds + event.out_lap_seconds) - (event.avg_lap_seconds * 2)
    return racing_time + transition_delta


def total_stints(event):
    """
    Calculate total number of stints for the event.

    = ceil(event.length_seconds / stint_length_seconds(event))

    Returns int.
    """
    return math.ceil(event.length_seconds / stint_length_seconds(event))


def stint_start_time(event, stint_number):
    """
    Calculate the UTC start datetime for a given stint.

    stint_number is 1-indexed. Stint 1 starts at event.start_datetime_utc.
    Each subsequent stint starts one stint_length_seconds after the previous.

    Returns timezone-aware datetime.
    """
    offset = (stint_number - 1) * stint_length_seconds(event)
    return event.start_datetime_utc + timedelta(seconds=offset)


def stint_end_time(event, stint_number):
    """
    Calculate the UTC end datetime for a given stint.

    The end time of stint N is the start time of stint N+1.
    For the final stint, it is event.end_datetime_utc.

    Returns timezone-aware datetime.
    """
    n = total_stints(event)
    if stint_number >= n:
        return event.end_datetime_utc
    return stint_start_time(event, stint_number + 1)


def get_stint_windows(event):
    """
    Returns a list of dicts for all stints:

    [
        {
            'stint_number': 1,
            'start_utc': datetime,
            'end_utc': datetime,
        },
        ...
    ]
    """
    return [
        {
            'stint_number': n,
            'start_utc': stint_start_time(event, n),
            'end_utc': stint_end_time(event, n),
        }
        for n in range(1, total_stints(event) + 1)
    ]


def get_availability_slots(event):
    """
    Returns a list of UTC datetimes representing every 30-minute
    availability slot from event start to event end.

    Each slot represents the start of a 30-minute block. The final slot
    included is the last one that begins before event.end_datetime_utc.
    """
    slots = []
    current = event.start_datetime_utc
    end = event.end_datetime_utc
    while current < end:
        slots.append(current)
        current += timedelta(minutes=30)
    return slots


def check_driver_conflict(driver, stint_window):
    """
    Returns True if the driver has a conflict for the given stint window.

    A conflict exists if any 30-minute slot in [start_utc, end_utc) is NOT
    in the driver's availability. In other words, the driver must be available
    for every 30-minute block that overlaps with this stint.

    stint_window is a dict with 'start_utc' and 'end_utc'.
    """
    start = stint_window['start_utc']
    end = stint_window['end_utc']

    available_slots = set(
        driver.availability.values_list('slot_utc', flat=True)
    )

    current = start
    while current < end:
        if current not in available_slots:
            return True
        current += timedelta(minutes=30)

    return False


def get_pit_windows(event):
    """
    Returns a list of stint numbers (1-indexed) where a pit stop is required.

    Logic:
    - Track cumulative fuel used: stint_number * fuel_per_lap * target_laps
    - A pit window occurs when remaining fuel after a stint would be less than
      fuel_per_lap * target_laps (can't complete the next stint on current fuel).
    - Also flag any stint where remaining_fuel < tire_change_fuel_min
      (forced combined stop: tires require a fuel top-up to meet minimum).

    Returns list of dicts:
    [{'stint_number': N, 'reason': 'fuel' | 'tires_require_fuel'}, ...]
    """
    fuel_per_stint = event.fuel_per_lap * event.target_laps
    n = total_stints(event)
    pit_windows = []
    fuel_remaining = event.fuel_capacity

    for stint in range(1, n + 1):
        fuel_remaining -= fuel_per_stint

        if stint == n:
            # No pit required after the final stint
            break

        needs_fuel = fuel_remaining < fuel_per_stint
        needs_tire_top_up = (
            event.tire_change_fuel_min is not None
            and fuel_remaining < event.tire_change_fuel_min
        )

        if needs_tire_top_up:
            pit_windows.append({'stint_number': stint, 'reason': 'tires_require_fuel'})
            fuel_remaining = event.fuel_capacity
        elif needs_fuel:
            pit_windows.append({'stint_number': stint, 'reason': 'fuel'})
            fuel_remaining = event.fuel_capacity

    return pit_windows
