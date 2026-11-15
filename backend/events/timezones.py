"""
The timezones offered by every timezone picker on the site.

A curated list rather than the full IANA database: a racer scanning for
"Eastern" should not have to wade through 400 entries. Anything submitted to
the server is checked against VALID_TIMEZONES, so a picker cannot be used to
store a zone that is not listed here.
"""

CURATED_TIMEZONES = [
    {
        'region': 'UTC',
        'zones': [('UTC', 'UTC')],
    },
    {
        'region': 'United States & Canada',
        'zones': [
            ('Hawaii (HST)', 'Pacific/Honolulu'),
            ('Alaska (AKST/AKDT)', 'America/Anchorage'),
            ('Pacific (PST/PDT)', 'America/Los_Angeles'),
            ('Mountain (MST/MDT)', 'America/Denver'),
            ('Mountain - no DST (MST)', 'America/Phoenix'),
            ('Central (CST/CDT)', 'America/Chicago'),
            ('Eastern (EST/EDT)', 'America/New_York'),
            ('Atlantic (AST/ADT)', 'America/Halifax'),
            ('Newfoundland (NST/NDT)', 'America/St_Johns'),
        ],
    },
    {
        'region': 'Central & South America',
        'zones': [
            ('Mexico City', 'America/Mexico_City'),
            ('Colombia & Peru', 'America/Bogota'),
            ('Brazil - Brasilia', 'America/Sao_Paulo'),
            ('Argentina', 'America/Argentina/Buenos_Aires'),
            ('Chile', 'America/Santiago'),
        ],
    },
    {
        'region': 'Europe',
        'zones': [
            ('UK & Ireland (GMT/BST)', 'Europe/London'),
            ('Portugal (WET/WEST)', 'Europe/Lisbon'),
            ('Western Europe (CET/CEST)', 'Europe/Paris'),
            ('Central Europe (CET/CEST)', 'Europe/Berlin'),
            ('Eastern Europe (EET/EEST)', 'Europe/Helsinki'),
            ('Greece & Romania (EET/EEST)', 'Europe/Athens'),
            ('Turkey (TRT)', 'Europe/Istanbul'),
            ('Russia - Moscow (MSK)', 'Europe/Moscow'),
        ],
    },
    {
        'region': 'Middle East & Africa',
        'zones': [
            ('South Africa (SAST)', 'Africa/Johannesburg'),
            ('East Africa (EAT)', 'Africa/Nairobi'),
            ('Egypt (EET)', 'Africa/Cairo'),
            ('UAE & Oman (GST)', 'Asia/Dubai'),
            ('Saudi Arabia (AST)', 'Asia/Riyadh'),
            ('Israel (IST/IDT)', 'Asia/Jerusalem'),
        ],
    },
    {
        'region': 'Asia',
        'zones': [
            ('India (IST)', 'Asia/Kolkata'),
            ('Pakistan (PKT)', 'Asia/Karachi'),
            ('Bangladesh (BST)', 'Asia/Dhaka'),
            ('Thailand & Vietnam (ICT)', 'Asia/Bangkok'),
            ('China, HK & Taiwan (CST)', 'Asia/Shanghai'),
            ('Singapore & Malaysia (SGT)', 'Asia/Singapore'),
            ('South Korea (KST)', 'Asia/Seoul'),
            ('Japan (JST)', 'Asia/Tokyo'),
            ('Philippines (PST)', 'Asia/Manila'),
        ],
    },
    {
        'region': 'Australia & Pacific',
        'zones': [
            ('Australia - Perth (AWST)', 'Australia/Perth'),
            ('Australia - Darwin (ACST)', 'Australia/Darwin'),
            ('Australia - Adelaide (ACST/ACDT)', 'Australia/Adelaide'),
            ('Australia - Brisbane (AEST)', 'Australia/Brisbane'),
            ('Australia - Sydney (AEST/AEDT)', 'Australia/Sydney'),
            ('New Zealand (NZST/NZDT)', 'Pacific/Auckland'),
        ],
    },
]

VALID_TIMEZONES = frozenset(
    value
    for group in CURATED_TIMEZONES
    for _label, value in group['zones']
)
