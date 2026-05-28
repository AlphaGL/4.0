# main/pwa_views.py

from django.http import JsonResponse, HttpResponse
from django.views.generic import TemplateView
from django.conf import settings
import json


def manifest_view(request):
    """Serve the PWA manifest.json"""
    manifest_data = {
        "name": "Watch2D - Movies, Anime & Manga",
        "short_name": "Watch2D",
        "description": "Stream HD movies, watch anime, and read manga — all in one place!",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "theme_color": "#3b82f6",
        "background_color": "#ffffff",
        "categories": ["entertainment", "multimedia", "movies", "anime", "manga"],
        "lang": "en",
        "dir": "ltr",
        "icons": [
            {"src": "/static/img/icons/icon-72x72.png",   "sizes": "72x72",   "type": "image/png", "purpose": "maskable any"},
            {"src": "/static/img/icons/icon-96x96.png",   "sizes": "96x96",   "type": "image/png", "purpose": "maskable any"},
            {"src": "/static/img/icons/icon-128x128.png", "sizes": "128x128", "type": "image/png", "purpose": "maskable any"},
            {"src": "/static/img/icons/icon-144x144.png", "sizes": "144x144", "type": "image/png", "purpose": "maskable any"},
            {"src": "/static/img/icons/icon-152x152.png", "sizes": "152x152", "type": "image/png", "purpose": "maskable any"},
            {"src": "/static/img/icons/icon-192x192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable any"},
            {"src": "/static/img/icons/icon-384x384.png", "sizes": "384x384", "type": "image/png", "purpose": "maskable any"},
            {"src": "/static/img/icons/icon-512x512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable any"},
        ],
        "shortcuts": [
            {
                "name": "Movies",
                "short_name": "Movies",
                "description": "Browse latest HD movies",
                "url": "/movies/",
                "icons": [{"src": "/static/img/icons/icon-96x96.png", "sizes": "96x96"}]
            },
            {
                "name": "Anime",
                "short_name": "Anime",
                "description": "Watch anime series",
                "url": "/anime/",
                "icons": [{"src": "/static/img/icons/icon-96x96.png", "sizes": "96x96"}]
            },
            {
                "name": "Manga",
                "short_name": "Manga",
                "description": "Read manga online",
                "url": "/manga/",
                "icons": [{"src": "/static/img/icons/icon-96x96.png", "sizes": "96x96"}]
            },
        ]
    }

    response = JsonResponse(manifest_data)
    response['Content-Type'] = 'application/manifest+json'
    response['Cache-Control'] = 'public, max-age=86400'
    return response


def service_worker_view(request):
    """Serve the service worker"""
    try:
        sw_path = settings.BASE_DIR / 'main' / 'static' / 'js' / 'sw.js'
        if not sw_path.exists():
            sw_path = settings.BASE_DIR / 'movies' / 'static' / 'js' / 'sw.js'
        with open(sw_path, 'r') as f:
            content = f.read()
        response = HttpResponse(content, content_type='application/javascript')
        response['Cache-Control'] = 'public, max-age=3600'
        response['Service-Worker-Allowed'] = '/'
        return response
    except FileNotFoundError:
        return HttpResponse("Service worker not found", status=404)


def offline_view(request):
    return TemplateView.as_view(template_name='main/offline.html')(request)


def push_subscribe_view(request):
    if request.method == 'POST':
        try:
            subscription_data = json.loads(request.body)
            return JsonResponse({'success': True})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    return JsonResponse({'success': False, 'error': 'Invalid method'})