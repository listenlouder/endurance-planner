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


def last_stint_length_seconds(event):
    """
    Calculate the duration of the final stint in seconds.
    The last stint covers only the remaining laps after all preceding full stints,
    so it is typically shorter than a standard stint.

    Returns None if required fields are not set or remaining laps <= 0.
    Falls back to stint_length_seconds() if remaining laps >= target_laps
    (edge case where the race divides evenly).
    """
    if not event.has_required_stint_fields:
        return None

    total = total_race_laps(event)
    ts = total_stints(event)

    if total is None or ts is None:
        return None

    remaining = max(0, total - ((ts - 1) * event.target_laps))

    if remaining == 0:
        return None

    if remaining >= event.target_laps:
        return stint_length_seconds(event)

    last_seconds = (
        (remaining * event.avg_lap_seconds)
        + event.in_lap_seconds
        + event.out_lap_seconds
        - (event.avg_lap_seconds * 2)
    )
    return max(last_seconds, 0)


def format_stint_duration(seconds):
    """
    Format a duration in seconds as a human-readable string.
    e.g. 3720 → "62m", 3661 → "61m 1s"
    """
    if seconds is None:
        return '—'
    total = int(round(seconds))
    mins = total // 60
    secs = total % 60
    if secs:
        return f"{mins}m {secs}s"
    return f"{mins}m"


def total_stints(event):
    """
    Calculate total number of stints for the event.

    = ceil(event.length_seconds / stint_length_seconds(event))

    Returns int.
    """
    return math.ceil(event.length_seconds / stint_length_seconds(event))


def total_race_laps(event):
    """
    Estimated total laps in the race based on average lap time.
    Returns None if required fields are not set.

    When all stint fields are configured, uses total_stints × target_laps —
    this is the correct planned lap count because each stint does exactly
    target_laps laps and total_stints already accounts for in/out lap
    overhead. Dividing raw race time by avg_lap would overcount because
    the slower in/out laps consume more time than avg_lap implies.

    Falls back to floor(race_length / avg_lap) as a rough estimate when
    only avg_lap_seconds and length_seconds are set.
    """
    if event.has_required_stint_fields:
        n = total_stints(event)
        sl = stint_length_seconds(event)
        last_stint_time = event.length_seconds - (n - 1) * sl
        return (n - 1) * event.target_laps + math.floor(last_stint_time / event.avg_lap_seconds)
    if not event.avg_lap_seconds or not event.length_seconds:
        return None
    return math.floor(event.length_seconds / event.avg_lap_seconds)


def laps_remaining_after_stint(event, stint_number):
    """
    Estimated laps remaining in the race after stint N pits.
    stint_number is 1-indexed. Returns None if required fields not set.
    Returns 0 minimum (never negative).
    Returns None for the last stint — caller signals that as FINISH.
    """
    total = total_race_laps(event)
    if total is None or not event.target_laps:
        return None
    remaining = total - (stint_number * event.target_laps)
    return max(0, remaining)


def stint_start_time(event, stint_number):
    """
    Calculate the UTC start datetime for a given stint.

    stint_number is 1-indexed. Stint 1 starts at
    event.effective_start_datetime_utc (race_start_time_utc when set,
    otherwise session start_time_utc).

    Returns timezone-aware datetime.
    """
    offset = (stint_number - 1) * stint_length_seconds(event)
    return event.effective_start_datetime_utc + timedelta(seconds=offset)


def stint_end_time(event, stint_number):
    """
    Calculate the UTC end datetime for a given stint.

    The end time of stint N is the start time of stint N+1.
    For the final stint, it is event.effective_end_datetime_utc.

    Returns timezone-aware datetime.
    """
    n = total_stints(event)
    if stint_number >= n:
        return event.effective_end_datetime_utc
    return stint_start_time(event, stint_number + 1)


def get_stint_windows(event, assignment_overrides=None):
    """
    Returns a list of dicts for all stints, including per-stint duration.

    assignment_overrides: optional dict of { stint_number (int): StintAssignment }.
    When provided, stints with actual_start_utc set use that as their start time.
    All subsequent stints cascade from the overridden time unless they also have
    their own override.

    Each dict contains:
        stint_number, start_utc, end_utc, duration_seconds, is_last, is_overridden
    """
    if not event.has_required_stint_fields:
        return []

    ts = total_stints(event)
    standard_duration = stint_length_seconds(event)
    last_duration = last_stint_length_seconds(event)

    overrides = assignment_overrides or {}
    previous_end = event.effective_start_datetime_utc
    windows = []

    for n in range(1, ts + 1):
        is_last = (n == ts)
        duration = last_duration if is_last else standard_duration

        assignment = overrides.get(n)
        is_overridden = bool(assignment and assignment.actual_start_utc)

        start = assignment.actual_start_utc if is_overridden else previous_end
        end = start + timedelta(seconds=duration) if duration else start

        previous_end = end

        windows.append({
            'stint_number':     n,
            'start_utc':        start,
            'end_utc':          end,
            'duration_seconds': duration,
            'is_last':          is_last,
            'is_overridden':    is_overridden,
        })

    return windows


def get_availability_slots(event):
    """
    Returns a list of UTC datetimes representing every 30-minute
    availability slot from session start to one hour past effective
    race end.

    Always anchors to start_datetime_utc (session start) so warmup
    and qualifying slots are included even when race_start_time_utc
    differs. Extends one hour past end_datetime_utc as a buffer for
    races that run long and to cover the race_start_time_utc offset.
    """
    start = event.start_datetime_utc
    end = event.end_datetime_utc + timedelta(hours=1)
    slots = []
    current = start
    while current < end:
        slots.append(current)
        current += timedelta(minutes=30)
    return slots


def _snap_to_grid(start_utc, grid_anchor, slot_duration=timedelta(minutes=30)):
    """
    Snap start_utc forward to the first 30-min grid boundary >= start_utc.

    Availability slots live on a grid anchored at the event start time.
    When stint length is not a multiple of 30 min, a stint's start_utc
    falls off that grid. Ceiling-snapping ensures we only check slots that
    can actually exist in the availability table.
    """
    offset_slots = math.ceil((start_utc - grid_anchor) / slot_duration)
    return grid_anchor + offset_slots * slot_duration


def build_stint_availability_matrix(drivers, stint_windows, grid_anchor=None):
    """
    For each driver and each stint window, determine availability status.

    grid_anchor must be the session start datetime (event.start_datetime_utc),
    NOT the race start. Availability slots are always anchored to the session
    start, so snapping must use the same origin. Defaults to
    stint_windows[0]['start_utc'] for backward compatibility, but callers
    should always pass event.start_datetime_utc explicitly.

    Returns a dict:
    {
        driver_id (str): {
            stint_number (int): 'full' | 'partial' | 'none' | 'empty'
        }
    }
    """
    if not stint_windows:
        return {}

    result = {}
    slot_duration = timedelta(minutes=30)
    if grid_anchor is None:
        grid_anchor = stint_windows[0]['start_utc']

    for driver in drivers:
        driver_avail = set(a.slot_utc for a in driver.availability.all())
        result[str(driver.id)] = {}

        for sw in stint_windows:
            start = sw['start_utc']
            end = sw['end_utc']

            snapped_start = _snap_to_grid(start, grid_anchor, slot_duration)
            total_slots = []
            # If the stint starts before the first grid slot, include the slot
            # that covers the pre-grid portion (snapped_start - 30min).
            if start < snapped_start:
                total_slots.append(snapped_start - slot_duration)
            current = snapped_start
            while current < end:
                total_slots.append(current)
                current += slot_duration

            if not total_slots:
                result[str(driver.id)][sw['stint_number']] = 'empty'
                continue

            available_count = sum(1 for slot in total_slots if slot in driver_avail)

            if available_count == len(total_slots):
                result[str(driver.id)][sw['stint_number']] = 'full'
            elif available_count == 0:
                result[str(driver.id)][sw['stint_number']] = 'none'
            else:
                result[str(driver.id)][sw['stint_number']] = 'partial'

    return result


def check_driver_conflict(driver, stint_window, grid_anchor=None):
    """
    Returns True if the driver has a conflict for the given stint window.

    A conflict exists if any 30-minute slot in [first_grid_slot, end_utc) is
    NOT in the driver's availability, where first_grid_slot is the first 30-min
    grid boundary >= start_utc (using the same ceil-snap as
    build_stint_availability_matrix).

    grid_anchor defaults to stint_window['start_utc'] (i.e. the window itself
    is on-grid). Pass the event's race-start datetime when checking stints
    whose start times may fall off the 30-min grid.

    stint_window is a dict with 'start_utc' and 'end_utc'.
    """
    start = stint_window['start_utc']
    end = stint_window['end_utc']
    anchor = grid_anchor if grid_anchor is not None else start
    slot_duration = timedelta(minutes=30)

    available_slots = set(
        driver.availability.values_list('slot_utc', flat=True)
    )

    snapped_start = _snap_to_grid(start, anchor, slot_duration)
    # If the stint starts before the first grid slot, also check the slot
    # covering the pre-grid portion (snapped_start - 30min).
    if start < snapped_start:
        if (snapped_start - slot_duration) not in available_slots:
            return True

    current = snapped_start
    while current < end:
        if current not in available_slots:
            return True
        current += slot_duration

    return False

