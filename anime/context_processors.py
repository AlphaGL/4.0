# anime/context_processors.py
from django.core.cache import cache
from .models import AnimeCategory, AnimeGenre

_CATS_KEY  = 'anime_categories_v1'
_GENRE_KEY = 'anime_genres_v1'
_CACHE_TTL = 60 * 60  # 1 hour


def anime_context(request):
    cats = cache.get(_CATS_KEY)
    if cats is None:
        cats = list(AnimeCategory.objects.filter(is_active=True)[:10])
        cache.set(_CATS_KEY, cats, _CACHE_TTL)

    genres = cache.get(_GENRE_KEY)
    if genres is None:
        genres = list(AnimeGenre.objects.all()[:15])
        cache.set(_GENRE_KEY, genres, _CACHE_TTL)

    return {
        'anime_categories': cats,
        'anime_genres': genres,
    }