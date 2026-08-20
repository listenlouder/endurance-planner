"""
Test settings for WeAreChecking.

Inherits from the main settings module but overrides the database
to use SQLite in-memory so tests run without a MySQL server.
"""
from .settings import *  # noqa: F401 F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

# Speed up password hashing in tests
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]

# Suppress whitenoise complaints about missing staticfiles manifest in tests
STATICFILES_STORAGE = 'django.contrib.staticfiles.storage.StaticFilesStorage'

# Silence allauth system checks that require a database Site object
SILENCED_SYSTEM_CHECKS = [
    'models.W036',
    'sites.E101',
]

# Route logging to a null handler. The middleware emits a line per request and
# the suite makes thousands of them, which would bury a real traceback in
# scroll-back — and a clean test run is the signal that nothing regressed.
# assertLogs() installs its own handler, so tests that assert on log output
# are unaffected by this.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'null': {'class': 'logging.NullHandler'},
    },
    'root': {
        'handlers': ['null'],
        'level': 'WARNING',
    },
}
