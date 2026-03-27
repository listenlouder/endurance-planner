from django import template
from zoneinfo import ZoneInfo

register = template.Library()


@register.filter
def to_tz(dt, timezone_str):
    """
    Convert a UTC-aware datetime to the given IANA timezone string.
    Usage in templates: {{ some_datetime|to_tz:user_timezone }}
    """
    try:
        tz = ZoneInfo(timezone_str)
        return dt.astimezone(tz)
    except Exception:
        return dt


@register.filter
def time_in_tz(dt, timezone_str):
    """
    Same as to_tz but returns only the time portion formatted as HH:MM.
    Usage: {{ slot_utc|time_in_tz:user_timezone }}
    """
    try:
        tz = ZoneInfo(timezone_str)
        local_dt = dt.astimezone(tz)
        return local_dt.strftime('%H:%M')
    except Exception:
        return dt.strftime('%H:%M')
