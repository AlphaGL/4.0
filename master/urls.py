# master/urls.py
from django.contrib import admin
from django.urls import path, include, re_path
from django.contrib.sitemaps.views import sitemap
from django.views.generic import RedirectView
from django.http import HttpResponsePermanentRedirect
from django.contrib.auth.views import LogoutView
from django.conf import settings
from django.conf.urls.static import static

from movies.views import robots_txt, app_ads_txt
from master.sitemaps import sitemaps


# =============================================================================
# Redirect helpers
# =============================================================================

def strip_main_prefix(request, rest=''):
    """
    /main/          →  /
    /main/<path>/   →  /<path>/
    Only used for the main/ prefix — movies/ sub-paths are kept as-is.
    """
    destination = '/' + rest if rest else '/'
    return HttpResponsePermanentRedirect(destination)


# =============================================================================
# URL PATTERNS
# =============================================================================

urlpatterns = [
    # ── Admin ─────────────────────────────────────────────────────────────────
    path('watch2d/watch2d_admin/admin/', admin.site.urls),

    # ── PWA / utility endpoints ───────────────────────────────────────────────
    # Declared before the /main/ redirect so service-worker and manifest
    # are always served from their canonical root paths.
    path('manifest.json',  include('main.urls')),
    path('sw.js',          include('main.urls')),
    path('offline.html',   include('main.urls')),
    path('api/',           include('main.urls')),   # /api/push-subscribe/
    path('access/',        include('main.urls')),   # ping view

    # ── Permanent 301: /main/ → / ────────────────────────────────────────────
    # The main app no longer has its own URL prefix.
    path('main/',          RedirectView.as_view(url='/', permanent=True)),
    re_path(r'^main/(?P<rest>.+)$', strip_main_prefix),

    # ── Permanent 301: /movies/ homepage → / ─────────────────────────────────
    # Only the bare /movies/ homepage redirects to /.
    # All sub-paths (/movies/movie/123/slug/, /movies/category/..., etc.)
    # are kept intact — they have SEO history and are served normally below.
    path('movies/',        RedirectView.as_view(url='/', permanent=True)),

    # ── Canonical apps ────────────────────────────────────────────────────────

    # 🎬 Movies — root "/" is the movies homepage; /movies/* sub-paths also work
    path('',        include(('movies.urls', 'movies'), namespace='movies')),
    path('movies/', include(('movies.urls', 'movies'), namespace='movies')),

    # 🎭 Anime
    path('anime/', include('anime.urls')),

    # 📚 Manga
    path('manga/', include('manga.urls')),

    # ── Auth ──────────────────────────────────────────────────────────────────
    path('logout/',   LogoutView.as_view(), name='logout'),
    path('accounts/', include('allauth.urls')),

    # ── SEO ───────────────────────────────────────────────────────────────────
    path(
        'sitemap.xml',
        sitemap,
        {'sitemaps': sitemaps},
        name='django.contrib.sitemaps.views.sitemap',
    ),
    path(
        'sitemap-<section>.xml',
        sitemap,
        {'sitemaps': sitemaps},
        name='django.contrib.sitemaps.views.sitemap',
    ),
    path('robots.txt', robots_txt, name='robots_txt'),
    path('app-ads.txt', app_ads_txt, name='app_ads_txt'),
]

# ── Custom error handlers ─────────────────────────────────────────────────────
handler404 = 'main.views.custom_404_view'
handler500 = 'main.views.custom_500_view'
handler403 = 'main.views.custom_403_view'
handler400 = 'main.views.custom_400_view'

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL,  document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)