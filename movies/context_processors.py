# movies/context_processors.py
from django.conf import settings
from django.core.cache import cache
from .models import Category

_CACHE_KEY = 'movies_categories_v1'
_CACHE_TTL = 60 * 60  # 1 hour — categories change rarely


def categories_processor(request):
    cats = cache.get(_CACHE_KEY)
    if cats is None:
        cats = list(Category.objects.all())
        cache.set(_CACHE_KEY, cats, _CACHE_TTL)
    return {
        'categories': cats,
        # Monetag Telegram Mini App zone id (empty until configured) — read by
        # the Mini App script in base.html.
        'MONETAG_MINIAPP_ZONE': getattr(settings, 'MONETAG_MINIAPP_ZONE', ''),
    }