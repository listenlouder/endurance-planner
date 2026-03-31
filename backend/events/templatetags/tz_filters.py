import logging

from django import template
from zoneinfo import ZoneInfo

register = template.Library()
logger = logging.getLogger(__name__)


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

    WARNING: Do not pipe the result into Django's |date filter. With USE_TZ=True
    and TIME_ZONE='UTC', Django's date filter will re-convert the datetime back to
    UTC before formatting. Use |datetime_in_tz instead.
    """
    try:
        tz = ZoneInfo(timezone_str)
        return dt.astimezone(tz)
    except Exception:
        logger.warning("to_tz filter failed for timezone %r", timezone_str, exc_info=True)
        return dt


@register.filter
def datetime_in_tz(dt, timezone_str):
    """
    Convert a UTC-aware datetime to the given timezone and return a formatted string.
    Bypasses Django's |date filter, which re-converts aware datetimes to TIME_ZONE='UTC'.
    Output format: 'Mar 31 2025, 14:00'
    Usage: {{ driver.signed_up_at|datetime_in_tz:admin_tz }}
    """
    try:
        tz = ZoneInfo(timezone_str)
        local = dt.astimezone(tz)
        # Avoid %-d / %#d platform differences — use .day directly for no-padding
        return f"{local.strftime('%b')} {local.day} {local.strftime('%Y, %H:%M')}"
    except Exception:
        logger.warning("datetime_in_tz failed for timezone %r", timezone_str, exc_info=True)
        return str(dt)


@register.filter
def to_utc_z(dt):
    """
    Format a UTC datetime as an ISO string with Z suffix for JavaScript consumption.
    Omits microseconds/milliseconds so strings match server-generated availability data.
    Usage: {{ some_utc_datetime|to_utc_z }}
    """
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


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
