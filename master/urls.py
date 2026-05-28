# master/urls.py (project-level URLs)
from django.contrib import admin
from django.urls import path, include
from django.contrib.sitemaps.views import sitemap
from movies.sitemaps import (
    HomeSitemap, SearchSitemap,
    CategorySitemap, mastermap,
    AnimeSitemap, MangaSitemap, APKSitemap, PCGamesSitemap  # Add these
)
from movies.views import robots_txt
from django.contrib.auth.views import LogoutView
from django.conf import settings
from django.conf.urls.static import static

# Sitemaps
sitemaps = {
    'home': HomeSitemap(),
    'search': SearchSitemap(),
    'categories': CategorySitemap(),
    'movies': mastermap(),
    'anime': AnimeSitemap(),      # Add
    'manga': MangaSitemap(),      # Add
}

# =============================================================================
# MAIN URL PATTERNS
# =============================================================================

urlpatterns = [
    # Admin
    path('watch2d/watch2d_admin/admin/', admin.site.urls),
    
    # ⭐ UNIFIED HOMEPAGE (Main app) - Now includes PWA URLs
    path('main/', include('main.urls')),
    
    # 🎬 Movies App (has its own home page at /movies/)
    path('', include(('movies.urls', 'movies'), namespace='movies')),
    
    # 🎭 Anime App
    path('anime/', include('anime.urls')),
    
    # 📚 Manga App  
    path('manga/', include('manga.urls')),

    
    # Authentication
    path('logout/', LogoutView.as_view(), name='logout'),
    path('accounts/', include('allauth.urls')),
    
    # SEO
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='sitemap'),
    path("robots.txt", robots_txt),

    
    # PWA URLs are now handled by main.urls (included above with path('', include('main.urls')))
]

# Custom error handlers
handler404 = 'main.views.custom_404_view'
handler500 = 'main.views.custom_500_view'
handler403 = 'main.views.custom_403_view'
handler400 = 'main.views.custom_400_view'

# Static/Media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)