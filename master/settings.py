"""
Django settings for master project.
"""

from pathlib import Path
import os
import dj_database_url
from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config('SECRET_KEY', default='your-secret-key-here')
DEBUG = config('DEBUG', default=False, cast=bool)

ALLOWED_HOSTS = [
    'watch2d.vercel.app',
    'watch2d.org',
    'localhost',
    '127.0.0.1',
    '.org',
    '.onrender.com',
]


WHITENOISE_MIMETYPES = {
    '.mp4': 'video/mp4',
    '.webm': 'video/webm',
    '.mov': 'video/quicktime',
}


WHITENOISE_SKIP_COMPRESS_EXTENSIONS = [
    'jpg', 'jpeg', 'png', 'gif', 'webp',
    'zip', 'gz', 'tgz', 'bz2', 'tbz', 'xz', 'br',
    'swf', 'flv', 'woff', 'woff2',
    'mp4', 'webm', 'mp3', 'wav',  # add these
]

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Core apps
    'main',
    'movies',
    'anime',
    'automation',
    'manga',

    # Third-party
    'django_crontab',
    'crispy_forms',
    'crispy_bootstrap5',
    'django.contrib.sites',
    'django.contrib.sitemaps',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    'pwa',
]

SITE_ID = 1

# ── Error logging (production) ────────────────────────────────────────────────
# When DEBUG=False, Django swallows errors silently. This logs them to console
# so Render/Vercel can capture them in their dashboard logs.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
    },
}

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',    # ← must be before AccountMiddleware
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'allauth.account.middleware.AccountMiddleware',            # ← moved after Session + Auth
    'main.middleware.PWAMiddleware',
]

SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'APP': {
            'client_id': '998536136119-hhihes5q9b3e6qim325j5sk8t7i7oq7a.apps.googleusercontent.com',
            'secret': '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',
            'key': ''
        }
    }
}

ROOT_URLCONF = 'master.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [
            BASE_DIR / 'main' / 'templates',
            BASE_DIR / 'movies' / 'templates',
            BASE_DIR / 'anime' / 'templates',
            BASE_DIR / 'manga' / 'templates',
        ],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'movies.context_processors.categories_processor',
            ],
        },
    },
]

LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'
SOCIALACCOUNT_LOGIN_ON_GET = True

AUTHENTICATION_BACKENDS = [
    'allauth.account.auth_backends.AuthenticationBackend',
]

WSGI_APPLICATION = 'master.wsgi.application'

# ============================================================
# TELEGRAM
# ============================================================
TELEGRAM_BOT_TOKEN      = config('TELEGRAM_BOT_TOKEN', default='')
TELEGRAM_MOVIES_CHANNEL = config('TELEGRAM_MOVIES_CHANNEL', default='')
TELEGRAM_ANIME_CHANNEL  = config('TELEGRAM_ANIME_CHANNEL', default='')
TELEGRAM_MANGA_CHANNEL  = config('TELEGRAM_MANGA_CHANNEL', default='')

# ============================================================
# TELETHON — Private file upload channel
# Same Telegram account as above, but uploads files directly
# to a separate private channel via MTProto (supports up to 2 GB).
#
# How to get TELETHON_API_ID and TELETHON_API_HASH:
#   1. Go to https://my.telegram.org
#   2. Log in with your Telegram phone number
#   3. Click "API development tools"
#   4. Create an app (any name) → copy api_id and api_hash
#
# How to get TELETHON_PRIVATE_CHANNEL:
#   1. Open https://web.telegram.org
#   2. Open your private channel
#   3. Copy the number from the URL (include the -100 prefix)
#      e.g. https://web.telegram.org/k/#-1001234567890
#          → TELETHON_PRIVATE_CHANNEL=-1001234567890
#
# One-time login (run this ONCE on your server):
#   python manage.py scrape_thenkiri --telethon-login
# ============================================================
TELETHON_API_ID          = config('TELETHON_API_ID',          default=0,        cast=lambda v: int(v) if str(v).strip() else 0)
TELETHON_API_HASH        = config('TELETHON_API_HASH',        default='')
TELETHON_SESSION_NAME    = config('TELETHON_SESSION_NAME',    default='uploader')
TELETHON_PRIVATE_CHANNEL = config('TELETHON_PRIVATE_CHANNEL', default=0,        cast=lambda v: int(v) if str(v).strip() else 0)

# ============================================================
# TWITTER / X (OAuth 2.0)
# ============================================================
TWITTER_CLIENT_ID     = config('TWITTER_CLIENT_ID',     default='')
TWITTER_CLIENT_SECRET = config('TWITTER_CLIENT_SECRET', default='')
TWITTER_REFRESH_TOKEN = config('TWITTER_REFRESH_TOKEN', default='')

# ============================================================
# FACEBOOK
# ============================================================
FB_PAGE_ID      = config('FB_PAGE_ID',      default='')
FB_ACCESS_TOKEN = config('FB_ACCESS_TOKEN', default='')

# PWA Settings
PWA_SETTINGS = {
    'name': 'Watch2D - Movies, Anime & Manga',
    'short_name': 'Watch2D',
    'description': 'Stream HD movies, watch anime, and read manga — all in one place!',
    'theme_color': '#3b82f6',
    'background_color': '#ffffff',
    'display': 'standalone',
    'scope': '/',
    'start_url': '/',
    'orientation': 'portrait-primary',
    'icons': [
        {
            'src': 'https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEgGDg63ESTUKkQx6xcxK4dBd8LDkHo5VjiLkh1drq5WGGSG1dLVGQdwY7eXuVQ6Rxtz2mVSkcVvK7f7pFk5_4UVQc8uuX5HI_2J5IUZxR7uhvdmjxb-LEBmqR7zDjqiwjJVSmzv1fKtAt6nHr0EiDAMNPTNMq1yUnkdcMsA_9Z4Dasfc8bxJ0pnFLwafJk/s320/logo%20(3).png',
            'sizes': '192x192',
            'type': 'image/png',
        },
        {
            'src': 'https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEgGDg63ESTUKkQx6xcxK4dBd8LDkHo5VjiLkh1drq5WGGSG1dLVGQdwY7eXuVQ6Rxtz2mVSkcVvK7f7pFk5_4UVQc8uuX5HI_2J5IUZxR7uhvdmjxb-LEBmqR7zDjqiwjJVSmzv1fKtAt6nHr0EiDAMNPTNMq1yUnkdcMsA_9Z4Dasfc8bxJ0pnFLwafJk/s320/logo%20(3).png',
            'sizes': '512x512',
            'type': 'image/png',
        }
    ]
}

SECURE_REFERRER_POLICY = 'same-origin'
CSP_DEFAULT_SRC = ("'self'",)
CSP_SCRIPT_SRC  = ("'self'", "'unsafe-inline'", "https://cdn.tailwindcss.com", "https://cdnjs.cloudflare.com")
CSP_STYLE_SRC   = ("'self'", "'unsafe-inline'", "https://fonts.googleapis.com", "https://cdnjs.cloudflare.com")
CSP_FONT_SRC    = ("'self'", "https://fonts.gstatic.com", "https://cdnjs.cloudflare.com")
CSP_IMG_SRC     = ("'self'", "data:", "https:", "blob:")
CSP_CONNECT_SRC = ("'self'", "https:")
CSP_MANIFEST_SRC = ("'self'",)

DATABASES = {
    'default': dj_database_url.parse(config('DATABASE_URL'))
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

STATICFILES_FINDERS = [
    'django.contrib.staticfiles.finders.FileSystemFinder',
    'django.contrib.staticfiles.finders.AppDirectoriesFinder',
]

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL  = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

STATICFILES_DIRS = [
    BASE_DIR / 'main' / 'static',
    BASE_DIR / 'movies' / 'static',
    BASE_DIR / 'pwa_static',
]

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

WHITENOISE_USE_FINDERS = True
WHITENOISE_MANIFEST_STRICT = False
WHITENOISE_ALLOW_ALL_ORIGINS = True
WHITENOISE_AUTOREFRESH = True

_REDIS_URL = config('REDIS_URL', default='')

if _REDIS_URL and _REDIS_URL not in ('', 'redis://localhost:6379/0'):
    # ── Redis Cloud (or any real Redis) ──────────────────────────────
    # Requires:  pip install django-redis
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': _REDIS_URL,
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
                # Silently ignore Redis errors — app keeps working if Redis goes down
                'IGNORE_EXCEPTIONS': True,
            },
            'TIMEOUT': 300,
        }
    }
    SESSION_ENGINE      = 'django.contrib.sessions.backends.cache'
    SESSION_CACHE_ALIAS = 'default'
else:
    # ── Fallback: file-based cache (local dev / no Redis configured) ──
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
            'LOCATION': '/tmp/watch2d_cache',
            'TIMEOUT': 300,
            'OPTIONS': {
                'MAX_ENTRIES': 3000,
            },
        }
    }

CACHE_CONTROL_MAX_AGE = 31536000
OFFLINE_URL = '/offline.html'

WEBPUSH_SETTINGS = {
    "VAPID_PUBLIC_KEY": "your-vapid-public-key-here",
    "VAPID_PRIVATE_KEY": "your-vapid-private-key-here",
    "VAPID_ADMIN_EMAIL": "admin@watch2d.org"
}

CRISPY_ALLOWED_TEMPLATE_PACKS = ["bootstrap5"]
CRISPY_TEMPLATE_PACK = "bootstrap5"

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

WP_SITE_URL     = 'https://naijadeleys.com.ng'
WP_USERNAME     = 'AlphaDev_'
WP_APP_PASSWORD = 'scK9 fIaZ FUmY tDWo Mhqb rXbq'

# SESSION_ENGINE and SESSION_CACHE_ALIAS are set inside the CACHES block above
# (only applied when a real Redis URL is configured)

CRONJOBS = [
    # Every 10 minutes — keeps Render free instance awake
    ('*/10 * * * *', 'main.cron.keep_alive_ping'),
]


SITE_URL = 'https://watch2d.org'



# ============================================================
# BREVO (email notifications — broken link reports)
# ============================================================
BREVO_API_KEY      = config('BREVO_API_KEY', default='')
# Your admin email — where broken-link reports are SENT TO
BREVO_ADMIN_EMAIL  = config('BREVO_ADMIN_EMAIL', default='')

# The FROM address — must be a verified sender in your Brevo account
BREVO_SENDER_EMAIL = config('BREVO_SENDER_EMAIL', default='')

BREVO_SENDER_NAME  = config('BREVO_SENDER_NAME', default='Watch2D Alerts')