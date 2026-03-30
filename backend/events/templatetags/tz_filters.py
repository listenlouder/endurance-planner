from django import template
from zoneinfo import ZoneInfo

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """Dict lookup by variable key. Usage: {{ my_dict|get_item:key_var }}"""
    return dictionary.get(key)


@register.filter
def format_hours(value):
    """
    Format a float hours value for display.
    12.0 → '12h', 2.667 → '2h 40m'
    """
    if value == '' or value is None:
        return '—'
    try:
        total_minutes = round(float(value) * 60)
        hours = total_minutes // 60
        minutes = total_minutes % 60
        if minutes == 0:
            return f"{hours}h"
        return f"{hours}h {minutes}m"
    except (TypeError, ValueError):
        return str(value)


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
