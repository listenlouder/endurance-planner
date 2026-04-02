import math
from datetime import timedelta


def seconds_to_mmss(seconds):
    """Convert a number of seconds to M:SS format string (e.g. 105 -> '1:45')."""
    if seconds is None:
        return ''
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def validate_stint_sanity(event):
    """
    Returns a list of warning strings if the stint configuration seems
    inconsistent. Returns empty list if all looks good or fields missing.
    Does not raise — these are warnings only.
    """
    warnings = []

    if not event.has_required_stint_fields:
        return warnings

    fuel_per_stint = event.fuel_per_lap * event.target_laps

    # Check 1: Fuel per stint exceeds tank size
    if fuel_per_stint > event.fuel_capacity:
        warnings.append(
            f"Target laps ({event.target_laps}) × fuel per lap "
            f"({event.fuel_per_lap}L) = {fuel_per_stint:.2f}L per stint, "
            f"which exceeds fuel capacity ({event.fuel_capacity}L). "
            f"Reduce target laps or fuel per lap."
        )
    # Check 2: Fuel per stint uses > 95% of tank
    elif fuel_per_stint > event.fuel_capacity * 0.98:
        warnings.append(
            f"Fuel per stint ({fuel_per_stint:.2f}L) uses more than 98% "
            f"of the tank ({event.fuel_capacity}L). "
            f"Consider reducing target laps to leave a safety margin."
        )

    # Check 3: In/out lap faster than average lap
    if event.in_lap_seconds < event.avg_lap_seconds:
        warnings.append(
            f"In lap time ({seconds_to_mmss(event.in_lap_seconds)}) is "
            f"faster than average lap ({seconds_to_mmss(event.avg_lap_seconds)}). "
            f"In lap is typically slower — please verify."
        )
    if event.out_lap_seconds < event.avg_lap_seconds:
        warnings.append(
            f"Out lap time ({seconds_to_mmss(event.out_lap_seconds)}) is "
            f"faster than average lap ({seconds_to_mmss(event.avg_lap_seconds)}). "
            f"Out lap is typically slower — please verify."
        )

    # Check 4: Stint length sanity
    sl = stint_length_seconds(event)
    if sl <= 0:
        warnings.append(
            "Stint length calculation results in zero or negative value. "
            "Check your lap time inputs."
        )
        return warnings

    if sl < 600:
        warnings.append(
            f"Calculated stint length is {int(sl // 60)}m {int(sl % 60)}s. "
            f"This seems very short — check your lap time and target laps."
        )

    # Check 5: Total stints sanity
    ts = total_stints(event)
    if ts > 200:
        warnings.append(
            f"Configuration results in {ts} total stints. "
            f"This seems high — check race length and stint length inputs."
        )

    return warnings


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

