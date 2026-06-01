# manga/context_processors.py
from django.core.cache import cache
from .models import MangaCategory, MangaGenre

_CATS_KEY  = 'manga_categories_v1'
_GENRE_KEY = 'manga_genres_v1'
_CACHE_TTL = 60 * 60  # 1 hour

_MANGA_TYPES = [
    {'value': 'manga',   'label': 'Manga'},
    {'value': 'manhwa',  'label': 'Manhwa'},
    {'value': 'manhua',  'label': 'Manhua'},
    {'value': 'webtoon', 'label': 'Webtoon'},
]


def manga_context(request):
    cats = cache.get(_CATS_KEY)
    if cats is None:
        cats = list(MangaCategory.objects.filter(is_active=True)[:10])
        cache.set(_CATS_KEY, cats, _CACHE_TTL)

    genres = cache.get(_GENRE_KEY)
    if genres is None:
        genres = list(MangaGenre.objects.all()[:15])
        cache.set(_GENRE_KEY, genres, _CACHE_TTL)

    return {
        'manga_categories': cats,
        'manga_genres':     genres,
        'manga_types':      _MANGA_TYPES,
    }