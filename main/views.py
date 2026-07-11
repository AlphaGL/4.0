# main/views.py  — full replacement
# Changes vs original:
#   1. UnifiedHomeView: all DB work wrapped in a single cache key (5-min TTL)
#   2. exists()+fetch pairs collapsed into one sliced query each
#   3. select_related / only() tightened so only needed columns travel over wire
#   4. Error handlers unchanged

from django.shortcuts import render
from django.views.generic import TemplateView
from django.db.models import Q, Prefetch
from django.http import JsonResponse
from django.core.cache import cache

from movies.models import Movie, Category as MovieCategory
from anime.models import Anime, Episode
from manga.models import Manga, Chapter

# Cache TTLs
HOME_CACHE_TTL   = 60 * 5    # 5 minutes  — homepage feels fresh, DB barely touched
STATIC_CACHE_TTL = 60 * 60   # 1 hour     — category/genre lists rarely change


def ping_view(request):
    return JsonResponse({"status": "OK"})


# ============================================================
# Custom Error Handlers
# ============================================================

def _suggested_movies(limit=12):
    """A few recent movies to show on error pages so users aren't dead-ended.
    Cached + wrapped in try/except so an error page NEVER fails itself — this
    matters most for 500, which may itself be caused by a DB problem."""
    try:
        from django.core.cache import cache
        from movies.models import Movie
        movies = cache.get('error_page_movies_v1')
        if movies is None:
            movies = list(
                Movie.objects.only('id', 'title', 'slug', 'image_url')
                .order_by('-created_at')[:limit]
            )
            cache.set('error_page_movies_v1', movies, 60 * 30)
        return movies
    except Exception:
        return []


def custom_404_view(request, exception):
    context = {'exception': str(exception) if exception else None,
               'suggested_movies': _suggested_movies()}
    return render(request, '404.html', context, status=404)

def custom_500_view(request):
    return render(request, '500.html', {'suggested_movies': _suggested_movies()}, status=500)

def custom_403_view(request, exception):
    context = {'exception': str(exception) if exception else None,
               'suggested_movies': _suggested_movies()}
    return render(request, '403.html', context, status=403)

def custom_400_view(request, exception):
    context = {'exception': str(exception) if exception else None,
               'suggested_movies': _suggested_movies()}
    return render(request, '400.html', context, status=400)

def custom_503_view(request):
    return render(request, '503.html', {'suggested_movies': _suggested_movies()}, status=503)


# ============================================================
# Helpers
# ============================================================

def _get_featured(Model, related_model, related_field, related_order, to_attr):
    """
    Fetch up to 3 featured items for a given model.
    Falls back to 3 most-recent active items if none are featured.
    Single query — no exists() check needed.
    """
    prefetch = Prefetch(
        related_field,
        queryset=related_model.objects.filter(is_active=True).order_by(related_order)[:5],
        to_attr=to_attr,
    )
    base_qs = (
        Model.objects
        .filter(is_active=True)
        .prefetch_related(prefetch)
        .select_related('category')
        .prefetch_related('genres')
        .order_by('-created_at')
    )
    # Try featured first, fall back transparently
    items = list(base_qs.filter(is_featured=True)[:3])
    if not items:
        items = list(base_qs[:3])
    return items


def _get_trending(Model):
    """
    Fetch up to 12 trending items.
    Falls back to 12 most-recent active items if none flagged trending.
    Single query.
    """
    items = list(
        Model.objects.filter(is_active=True, is_trending=True).order_by('-views')[:12]
    )
    if not items:
        items = list(Model.objects.filter(is_active=True).order_by('-created_at')[:12])
    return items


def _build_home_context():
    """
    All heavy DB work lives here so it can be cached atomically.
    Returns a plain dict (no request-specific data).
    """
    ctx = {}

    # ── MOVIES ──────────────────────────────────────────────────────────────
    ctx['featured_movies'] = list(
        Movie.objects
        .filter(is_blockbuster=True)
        .prefetch_related('categories')
        .only('id', 'title', 'slug', 'image_url', 'description', 'created_at', 'views')
        .order_by('-created_at')[:12]
    )

    ctx['trending_movies'] = list(
        Movie.objects
        .filter(views__gt=0)
        .only('id', 'title', 'slug', 'image_url', 'views', 'created_at')
        .order_by('-views', '-created_at')[:24]
    )

    ctx['latest_movies'] = list(
        Movie.objects
        .filter(Q(title_b__isnull=True) | Q(title_b=''))
        .only('id', 'title', 'slug', 'image_url', 'created_at')
        .order_by('-created_at')[:12]
    )

    ctx['movie_categories'] = list(MovieCategory.objects.all())

    # ── ANIME ────────────────────────────────────────────────────────────────
    ctx['featured_anime'] = _get_featured(
        Anime, Episode, 'episodes', '-episode_number', 'latest_episodes_list'
    )

    ctx['trending_anime'] = _get_trending(Anime)

    ctx['latest_episodes'] = list(
        Episode.objects
        .filter(is_active=True, anime__is_active=True)
        .select_related('anime', 'anime__category')
        .only(
            'id', 'episode_number', 'title', 'created_at',
            'anime__id', 'anime__slug', 'anime__title', 'anime__poster_url',
            'anime__category__name',
        )
        .order_by('-created_at')[:12]
    )

    # ── MANGA ────────────────────────────────────────────────────────────────
    ctx['featured_manga'] = _get_featured(
        Manga, Chapter, 'chapters', '-chapter_number', 'latest_chapters_list'
    )

    ctx['trending_manga'] = _get_trending(Manga)

    ctx['latest_chapters'] = list(
        Chapter.objects
        .filter(is_active=True, manga__is_active=True)
        .select_related('manga', 'manga__category')
        .only(
            'id', 'chapter_number', 'title', 'created_at',
            'manga__id', 'manga__slug', 'manga__title', 'manga__cover_url',
            'manga__category__name',
        )
        .order_by('-created_at')[:12]
    )

    # ── STATS ────────────────────────────────────────────────────────────────
    ctx['total_movies'] = Movie.objects.count()
    ctx['total_anime']  = Anime.objects.filter(is_active=True).count()
    ctx['total_manga']  = Manga.objects.filter(is_active=True).count()

    return ctx


# ============================================================
# Views
# ============================================================

class UnifiedHomeView(TemplateView):
    """
    Unified homepage. All heavy queries are cached for HOME_CACHE_TTL seconds.
    On a warm cache this view makes zero DB queries.
    """
    template_name = 'main/home.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        cached = cache.get('home_context_v1')
        if cached is None:
            cached = _build_home_context()
            cache.set('home_context_v1', cached, HOME_CACHE_TTL)

        context.update(cached)
        return context