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
