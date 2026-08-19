from django import template

register = template.Library()


@register.filter
def to_utc_z(dt):
    """
    Format a UTC datetime as an ISO string with Z suffix for JavaScript consumption.
    Omits microseconds/milliseconds so strings match server-generated availability data.
    Usage: {{ some_utc_datetime|to_utc_z }}
    """
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


@register.filter
def dict_get(d, key):
    """
    Dict lookup by variable key for use in templates.
    Usage: {{ my_dict|dict_get:variable_key }}
    Tries the key as-is first, then str(key) as a fallback (int → str coercion
    only). Uses 'key in d' to distinguish a missing key from a key whose value
    is None or another falsy value.
    """
    if d is None:
        return None
    if key in d:
        return d[key]
    str_key = str(key)
    if str_key in d:
        return d[str_key]
    return None


@register.filter
def seconds_to_hours_display(seconds):
    """
    Converts seconds to a human readable duration string.
    Examples: 86400 -> "24h", 23400 -> "6h 30m", 1800 -> "30m"

    Sub-hour durations omit the hours part entirely — "0h 30m" reads as a
    mistake, and this is used for gap durations that are routinely under an
    hour.
    """
    if not seconds:
        return '—'
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


@register.filter
def seconds_to_mmss(seconds):
    """Convert seconds (int or float) to MM:SS string."""
    if seconds is None:
        return ''
    total = int(round(seconds))
    m = total // 60
    s = total % 60
    return f"{m:02d}:{s:02d}"
