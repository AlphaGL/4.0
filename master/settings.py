"""
Django settings for master project.
"""

from pathlib import Path
import os
import dj_database_url
from decouple import config

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY', default='your-secret-key-here')

# DEBUG = config('DEBUG', default=False, cast=bool)

DEBUG = True

# Add your actual domain
ALLOWED_HOSTS = [
    'watch2d.vercel.app',
    'watch2d.org',
    'localhost',
    '127.0.0.1',
    '.org',
    '.onrender.com',
]

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # Your apps
    'main',
    'movies',
    'anime',
    'manga',
    'apk_store',
    'pc_games',
    'news',
    'automation',
    
    # Other apps
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

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # ← Must be here, right after SecurityMiddleware
    "allauth.account.middleware.AccountMiddleware",
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'main.middleware.PWAMiddleware',
]

# Provider specific settings
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
            BASE_DIR / 'main' / 'templates',  # This is the key one!
            BASE_DIR / 'movies' / 'templates',
            BASE_DIR / 'anime' / 'templates',
            BASE_DIR / 'manga' / 'templates',
            BASE_DIR / 'apk_store' / 'templates',
            BASE_DIR / 'pc_games' / 'templates',
            BASE_DIR / 'news' / 'templates',  # ← ADD THIS
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
    'name': 'Watch2D - Movies, Anime, Manga & Apps',
    'short_name': 'Watch2D',
    'description': 'All entertainment in one place. Stream movies, watch anime, read manga, and download premium APKs.',
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
CSP_SCRIPT_SRC = ("'self'", "'unsafe-inline'", "https://cdn.tailwindcss.com", "https://cdnjs.cloudflare.com")
CSP_STYLE_SRC = ("'self'", "'unsafe-inline'", "https://fonts.googleapis.com", "https://cdnjs.cloudflare.com")
CSP_FONT_SRC = ("'self'", "https://fonts.gstatic.com", "https://cdnjs.cloudflare.com")
CSP_IMG_SRC = ("'self'", "data:", "https:", "blob:")
CSP_CONNECT_SRC = ("'self'", "https:")
CSP_MANIFEST_SRC = ("'self'",)

# Database
DATABASES = {
    'default': dj_database_url.parse(config('DATABASE_URL'))
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# =============================================================================
# STATIC FILES CONFIGURATION (CRITICAL FOR DEPLOYMENT)
# =============================================================================

# Add these settings if not already present:
STATICFILES_FINDERS = [
    'django.contrib.staticfiles.finders.FileSystemFinder',
    'django.contrib.staticfiles.finders.AppDirectoriesFinder',  # ← This is crucial for admin
]

# Make sure these are correct:
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

STATICFILES_DIRS = [
    BASE_DIR / 'main' / 'static',
    BASE_DIR / 'movies' / 'static',
    BASE_DIR / 'pwa_static',
]

# Static files storage - WhiteNoise configuration
# Use ManifestStaticFilesStorage for better caching and versioning
# To this:
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}


# WhiteNoise settings for better performance
WHITENOISE_USE_FINDERS = True
WHITENOISE_MANIFEST_STRICT = False  # Don't fail if a file is missing from manifest
WHITENOISE_ALLOW_ALL_ORIGINS = True

# Cache settings
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'Watch2D-cache',
        'OPTIONS': {
            'MAX_ENTRIES': 1000
        }
    }
}

# PWA Caching headers
CACHE_CONTROL_MAX_AGE = 31536000  # 1 year for static files

# Add offline page URL
OFFLINE_URL = '/offline.html'

# Push notification settings
WEBPUSH_SETTINGS = {
    "VAPID_PUBLIC_KEY": "your-vapid-public-key-here",
    "VAPID_PRIVATE_KEY": "your-vapid-private-key-here",
    "VAPID_ADMIN_EMAIL": "admin@watch2d.net"
}

# Crispy Forms
CRISPY_ALLOWED_TEMPLATE_PACKS = ["bootstrap5"]
CRISPY_TEMPLATE_PACK = "bootstrap5"

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'



WP_SITE_URL     = 'https://naijadeleys.com.ng'
WP_USERNAME     = 'AlphaDev_'
WP_APP_PASSWORD = 'scK9 fIaZ FUmY tDWo Mhqb rXbq'

# HOSTING EMAILS:
    # udsn.defence@gmail.com

# DATABASES EMAIL
    # nchukwugozirim@gmail.com