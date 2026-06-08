from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError(
        'SECRET_KEY env var is not set. Generate one with:\n'
        '  python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"'
    )

SITE_URL = os.environ.get('SITE_URL', 'https://web-production-f7f96.up.railway.app')

DEBUG = os.environ.get('DEBUG', 'false').lower() == 'true'

ALLOWED_HOSTS = ['*']  # Safe behind Railway's proxy — CSRF_TRUSTED_ORIGINS handles origin security

CSRF_TRUSTED_ORIGINS = [
    'https://wvvy.pro',
    'https://www.wvvy.pro',
    'https://web-production-f7f96.up.railway.app',
] + [
    o.strip() for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
]

# Always trust Railway's proxy for HTTPS detection
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST    = True

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'

# Production security — only active when DEBUG=False
if not DEBUG:
    SESSION_COOKIE_SECURE          = True
    CSRF_COOKIE_SECURE             = True
    SECURE_SSL_REDIRECT            = False   # Railway terminates TLS at the edge
    SECURE_HSTS_SECONDS            = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD            = True

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'crm',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'crm.context_processors.workspace_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

import dj_database_url

# Railway may expose the Postgres URL under several variable names
_db_url = (
    os.environ.get('DATABASE_URL') or
    os.environ.get('POSTGRES_URL') or
    os.environ.get('DATABASE_PRIVATE_URL') or
    os.environ.get('DATABASE_PUBLIC_URL') or
    ''
)
if _db_url:
    DATABASES = {'default': dj_database_url.parse(_db_url, conn_max_age=600)}
elif not DEBUG:
    raise RuntimeError(
        'No database URL is configured. In production you MUST set DATABASE_URL '
        '(or POSTGRES_URL / DATABASE_PRIVATE_URL / DATABASE_PUBLIC_URL) to a '
        'PostgreSQL connection string. Railway: add a Postgres service to your '
        'project — it will inject DATABASE_URL automatically.'
    )
else:
    DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': BASE_DIR / 'db.sqlite3'}}

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

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── Celery ───────────────────────────────────────────────────────────────────
CELERY_BROKER_URL     = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
CELERY_ACCEPT_CONTENT     = ['json']
CELERY_TASK_SERIALIZER    = 'json'
CELERY_RESULT_SERIALIZER  = 'json'
CELERY_TIMEZONE           = 'UTC'
CELERY_TASK_TRACK_STARTED = True

LOGIN_URL = '/auth/login/'

MEDIA_URL  = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

MASTER_EMAIL = os.environ.get('MASTER_EMAIL', '')

GOOGLE_LOGIN_CLIENT_ID     = os.environ.get('GOOGLE_LOGIN_CLIENT_ID', '')
GOOGLE_LOGIN_CLIENT_SECRET = os.environ.get('GOOGLE_LOGIN_CLIENT_SECRET', '')

APIFY_API_TOKEN       = os.environ.get('APIFY_API_TOKEN', '')
APIFY_WEBHOOK_SECRET  = os.environ.get('APIFY_WEBHOOK_SECRET', '')
ZEROBOUNCE_API_KEY    = os.environ.get('ZEROBOUNCE_API_KEY', '')
