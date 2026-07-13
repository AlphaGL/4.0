# movies/context_processors.py
from django.conf import settings
from django.core.cache import cache
from .models import Category

_CACHE_KEY = 'movies_categories_v2'   # bumped: now excludes adult/18+
_CACHE_TTL = 60 * 60  # 1 hour — categories change rarely


def categories_processor(request):
    cats = cache.get(_CACHE_KEY)
    if cats is None:
        # Adult/18+ hidden from public browse (ad-network + SEO safety).
        cats = list(Category.objects.exclude(name__icontains='18+'))
        cache.set(_CACHE_KEY, cats, _CACHE_TTL)
    return {
        'categories': cats,
        # Monetag Telegram Mini App zone id (empty until configured) — read by
        # the Mini App script in base.html.
        'MONETAG_MINIAPP_ZONE': getattr(settings, 'MONETAG_MINIAPP_ZONE', ''),
    }