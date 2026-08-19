from pathlib import Path
from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')

SECRET_KEY = os.environ['DJANGO_SECRET_KEY']

DEBUG = os.environ.get('DJANGO_DEBUG', 'False').lower() == 'true'

_allowed = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1')
ALLOWED_HOSTS = [h.strip() for h in _allowed.split(',') if h.strip()]

# The one hostname users should end up on. Session and CSRF cookies are
# host-only, so serving the same site at both the apex and www domains means a
# login on one host does not carry to the other. When set, every request on
# another host is 301'd here. Leave unset to disable (local dev, single-host
# deploys).
CANONICAL_HOST = os.getenv('CANONICAL_HOST', '').strip()

# Domain recorded on the django.contrib.sites Site row. allauth builds the
# Discord callback from the request host, so this does not steer the OAuth
# round-trip; it is the domain used for absolute URLs built without a request,
# and it keeps the Site row from being whatever happens to sit first in
# ALLOWED_HOSTS. Defaults to CANONICAL_HOST, then to the first ALLOWED_HOSTS
# entry.
SITE_DOMAIN = os.getenv('SITE_DOMAIN', '').strip() or CANONICAL_HOST

INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_htmx',
    'events',
    'django.contrib.sites',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.discord',
]

SITE_ID = 1

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    # Must run before session/auth so the session cookie is only ever read and
    # written on the canonical host.
    'config.middleware.CanonicalHostMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'django_htmx.middleware.HtmxMiddleware',
]

# Trust Railway's reverse proxy
CSRF_TRUSTED_ORIGINS = os.environ.get(
    'CSRF_TRUSTED_ORIGINS',
    'http://localhost:8000'
).split(',')

# Tell Django it's behind a proxy
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'events.context_processors.auth_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.getenv('DB_NAME', 'endurance_planner'),
        'USER': os.getenv('DB_USER', 'endurance_user'),
        'PASSWORD': os.getenv('DB_PASSWORD', ''),
        'HOST': os.getenv('DB_HOST', 'localhost'),
        'PORT': os.getenv('DB_PORT', '3306'),
        'OPTIONS': {
            'charset': 'utf8mb4',
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

SESSION_ENGINE = 'django.contrib.sessions.backends.db'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = not DEBUG
# Re-stamp the expiry on every request so SESSION_COOKIE_AGE counts from last
# activity rather than from login. Without this an active user is still logged
# out 30 days after signing in.
SESSION_SAVE_EVERY_REQUEST = True
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

FEEDBACK_PASSWORD = os.environ.get('FEEDBACK_PASSWORD', '')

AUTH_USER_MODEL = 'events.User'

# Allauth — Discord OAuth
# prompt=none asks Discord to skip the authorize screen for users who have
# already granted these scopes — the fast path for returning users. Discord
# answers with an error rather than a consent screen when silent auth is not
# possible, so templates/socialaccount/authentication_error.html catches that
# and offers a retry that overrides this with prompt=consent via allauth's
# ?auth_params= query parameter.
SOCIALACCOUNT_PROVIDERS = {
    'discord': {
        'SCOPE': ['identify'],
        'AUTH_PARAMS': {'prompt': 'none'},
        'VERIFIED_EMAIL': False,
    }
}

SOCIALACCOUNT_ONLY = True
SOCIALACCOUNT_LOGIN_ON_GET = True
ACCOUNT_EMAIL_VERIFICATION = 'none'
SOCIALACCOUNT_AUTO_SIGNUP = True

SOCIALACCOUNT_ADAPTER = 'events.adapters.DiscordAccountAdapter'

SESSION_COOKIE_AGE = 60 * 60 * 24 * 30

LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'

ACCOUNT_DEFAULT_HTTP_PROTOCOL = 'http' if DEBUG else 'https'

DISCORD_CLIENT_ID = os.environ.get('DISCORD_CLIENT_ID', '')
DISCORD_CLIENT_SECRET = os.environ.get('DISCORD_CLIENT_SECRET', '')

SILENCED_SYSTEM_CHECKS = [
    'models.W036',
]