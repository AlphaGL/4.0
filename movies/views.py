# movies/views.py
from django.shortcuts import render, redirect, get_object_or_404
from django.core.paginator import Paginator
from django.views.generic import ListView, DetailView, CreateView
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import reverse_lazy
from django.contrib.auth import login
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator
from .models import Movie, Category, Comment
from .forms import MovieForm, CommentForm, DownloadLinkFormSet
from django.db.models import Q, Prefetch, Count
from django.templatetags.static import static
import random
from django.http import JsonResponse
from django.views.decorators.cache import cache_page
from django.http import HttpResponse
from django.views.generic import UpdateView, DeleteView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.conf import settings
from django.http import Http404
from django.forms import modelformset_factory
from .models import DownloadLink
from django.core.cache import cache
from django.db.models import F

from django.template.loader import render_to_string
from django.views.decorators.http import require_POST, require_GET
import requests
import re
from django.db import models as django_models

from django.http import StreamingHttpResponse
import requests

import re as _re
import django.db.models as django_models

# ── Cache TTL constants ───────────────────────────────────────────────────────
MOVIES_HOME_CACHE_TTL       = 60 * 5      # 5 minutes
SIDEBAR_CACHE_TTL           = 60 * 60 * 4  # 4 hours
CATEGORY_PAGE_CACHE_TTL     = 60 * 30     # 30 minutes

# Cache key constants
SIDEBAR_CATEGORIES_CACHE_KEY = 'sidebar_categories_v2'
CACHE_VERSION = 1


def _build_movies_home_context():
    """
    Heavy, cacheable part of HomeView context.
    Returns a plain dict — no request-specific data.
    """
    ctx = {}

    # Blockbusters
    ctx['blockbusters'] = list(
        Movie.objects
        .only('id', 'title', 'slug', 'image_url', 'created_at', 'views')
        .filter(views__gte=1000)
        .order_by('-views', '-created_at')[:12]
    )

    # Trending
    ctx['trending'] = list(
        Movie.objects
        .only('id', 'title', 'slug', 'image_url', 'views', 'created_at')
        .filter(views__gt=0)
        .order_by('-views', '-created_at')[:24]
    )

    # Sidebar categories (already cached separately for 4 h)
    ctx['categories'] = get_sidebar_categories()

    # All categories — deduplicated
    _STOP = _re.compile(
        r'\b(movie|movies|film|films|tv|series|drama|show|shows|watch|free|hd|'
        r'and|the|of|a|an)\b|[^a-z0-9 ]', _re.I
    )
    def _norm(name):
        n = _re.sub(r'[^\w\s]', '', name.lower())
        n = _STOP.sub(' ', n)
        return ' '.join(n.split())

    raw_cats = list(
        Category.objects.annotate(
            movie_count=django_models.Count('movies')
        ).filter(movie_count__gt=0).order_by('-movie_count')
    )
    seen_keys = {}
    deduped = []
    for cat in raw_cats:
        key = _norm(cat.name)
        if key and key not in seen_keys:
            seen_keys[key] = True
            deduped.append(cat)
    deduped.sort(key=lambda c: _re.sub(r'[^\w\s]', '', c.name).strip().lower())
    ctx['all_categories'] = deduped

    return ctx


def get_sidebar_categories():
    """
    Cached sidebar categories — 4 hour TTL.
    Zero DB queries on cache hit.
    """
    categories = cache.get(SIDEBAR_CATEGORIES_CACHE_KEY, version=CACHE_VERSION)
    if not categories:
        target_categories = [
            'Hollywood movies',
            'Korean drama',
            'TV Series',
        ]

        from django.db.models import Count as _Count, Q as _Q
        import functools as _ft, operator as _op

        name_filter = _ft.reduce(
            _op.or_,
            [_Q(name__iexact=name) for name in target_categories]
        )

        categories_qs = Category.objects.filter(name_filter).prefetch_related(
            Prefetch(
                'movies',
                queryset=Movie.objects.select_related().only(
                    'id', 'title', 'image_url', 'created_at'
                ).order_by('-created_at')[:12],
                to_attr='latest_movies'
            )
        )

        category_order = {name.lower(): i for i, name in enumerate(target_categories)}
        categories_list = [cat for cat in categories_qs if cat.latest_movies]
        categories_list.sort(key=lambda cat: category_order.get(cat.name.lower(), 999))

        cache.set(SIDEBAR_CATEGORIES_CACHE_KEY, categories_list,
                  SIDEBAR_CACHE_TTL, version=CACHE_VERSION)
        categories = categories_list

    return categories


def invalidate_sidebar_cache():
    """
    Call this when adding/updating movies to refresh all movie caches.
    Called from movies/admin.py on save/delete.
    """
    cache.delete(SIDEBAR_CATEGORIES_CACHE_KEY, version=CACHE_VERSION)
    cache.delete('movies_home_ctx_v1')
    cache.delete('movies_categories_v1')


def robots_txt(request):
    lines = [
        "User-agent: *",
        "",
        "# Public pages",
        "Allow: /$",
        "Allow: /movie/",
        "Allow: /movies/movie/",
        "Allow: /movies/category/",
        "Allow: /category/",
        "Allow: /anime/",
        "Allow: /manga/",
        "Allow: /read/",
        "Allow: /watch/",
        "",
        "# /movies/ homepage redirects to / — crawlers should use / directly",
        "Disallow: /movies/$",
        "",
        "# /main/ redirects to / — no indexable content here",
        "Disallow: /main/",
        "",
        "# Admin",
        "Disallow: /watch2d/watch2d_admin/",
        "",
        "# Auth",
        "Disallow: /accounts/",
        "Disallow: /logout/",
        "",
        "# AJAX / API",
        "Disallow: /ajax/",
        "Disallow: /api/",
        "Disallow: /resolve-download/",
        "Disallow: /check-streamable/",
        "Disallow: /stream/",
        "Disallow: /access/",
        "Disallow: /wp_auth_encrypt_ping/",
        "",
        "# Management",
        "Disallow: /anime/management/",
        "Disallow: /manga/management/",
        "",
        "# Action endpoints",
        "Disallow: /movie/*/like/",
        "Disallow: /movie/*/watchlist/",
        "Disallow: /movie/*/comment/",
        "Disallow: /comment/*/delete/",
        "",
        "# PWA internals",
        "Disallow: /sw.js",
        "Disallow: /offline.html",
        "Disallow: /api/push-subscribe/",
        "",
        "# Assets",
        "Disallow: /static/",
        "Disallow: /media/",
        "",
        "# Search result pages (avoid crawling paginated/filtered duplicates)",
        "Disallow: /movies/search/",
        "Disallow: /anime/search/",
        "Disallow: /manga/search/",
        "",
        "Sitemap: https://watch2d.org/sitemap.xml",
        "",
        "Crawl-delay: 2",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


def custom_404_view(request, exception):
    context = {
        'categories': get_sidebar_categories(),
    }
    return render(request, 'movies/404.html', context, status=404)


def ping_view(request):
    return JsonResponse({"status": "OK"})


# ── Streamable host lists ─────────────────────────────────────────────────────
STREAMABLE_HOSTS = [
    'mylulutv.com',
    'kissorgrab.com',
    'ma27b.kissorgrab.com',
]

MANUAL_HOSTS = [
    'ww1.sabishares.com',
    'downloadwella.com',
    'meetdownload.com',
]


@require_GET
def check_streamable(request):
    url = request.GET.get('url', '').strip()
    if not url:
        return JsonResponse({'streamable': False, 'reason': 'no_url'})

    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    lower = url.lower()

    direct_exts = ('.mp4', '.mkv', '.webm', '.avi', '.mov')
    if any(lower.endswith(ext) for ext in direct_exts) or '?pt=' in lower:
        return JsonResponse({'streamable': True, 'reason': 'direct_file'})

    if 'sabishares.com' in host and '/file/' in lower and 'preview' in lower:
        return JsonResponse({'streamable': True, 'reason': 'sabishares_preview'})

    if any(h in host for h in STREAMABLE_HOSTS):
        return JsonResponse({'streamable': True, 'reason': 'known_streamable_host'})

    if any(h in host for h in MANUAL_HOSTS):
        return JsonResponse({'streamable': False, 'reason': 'landing_page_host'})

    return JsonResponse({'streamable': True, 'reason': 'unknown'})


@require_GET
def resolve_download_link(request):
    landing_url = request.GET.get('url', '').strip()
    debug = request.GET.get('debug') == '1' and request.user.is_staff

    if not landing_url:
        return JsonResponse({'error': 'No URL provided'}, status=400)

    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(landing_url)
    host = parsed.netloc.lower()
    lower = landing_url.lower()

    if 'sabishares.com' in host and 'preview' in parsed.query:
        direct = urlunparse(parsed._replace(query='', fragment=''))
        if debug:
            return JsonResponse({'method': 'sabishares_preview', 'download_url': direct})
        return JsonResponse({'download_url': direct})

    direct_exts = ('.mp4', '.mkv', '.webm', '.avi', '.mov', '.zip', '.rar')
    if '?pt=' in lower or any(lower.endswith(ext) for ext in direct_exts):
        return JsonResponse({'download_url': landing_url})

    if 'mylulutv.com' in host:
        return JsonResponse({'download_url': landing_url})

    if 'downloadwella.com' in host:
        result, dbg = _resolve_downloadwella(landing_url, parsed, debug)
        if result:
            if debug:
                return JsonResponse({'method': 'downloadwella_post', 'download_url': result, 'debug': dbg})
            return JsonResponse({'download_url': result})
        if debug:
            return JsonResponse({'method': 'downloadwella_failed', 'fallback': landing_url, 'debug': dbg})
        return JsonResponse({'download_url': landing_url})

    html, fetch_err = _fetch_html_safe(landing_url)
    if not html:
        if debug:
            return JsonResponse({'method': 'fetch_failed', 'error': fetch_err, 'fallback': landing_url})
        return JsonResponse({'download_url': landing_url})

    download_url = _extract_download_url(html, host)
    if download_url:
        if debug:
            return JsonResponse({'method': 'html_extract', 'download_url': download_url, 'html_length': len(html)})
        return JsonResponse({'download_url': download_url})

    if debug:
        return JsonResponse({
            'method': 'extract_failed',
            'fallback': landing_url,
            'html_length': len(html),
            'cloudflare_block': 'cf-browser-verification' in html or 'Checking your browser' in html,
            'has_pt_token': '?pt=' in html,
            'has_kissorgrab': 'kissorgrab' in html,
            'html_snippet': html[:3000],
        })
    return JsonResponse({'download_url': landing_url})


def _resolve_downloadwella(landing_url, parsed, debug=False):
    dbg = {}
    try:
        path_parts = [p for p in parsed.path.split('/') if p]
        if not path_parts:
            return None, {'error': 'no_path_parts'}
        file_code = path_parts[0]
        dbg['file_code'] = file_code

        scraper = _get_scraper()
        base = f"{parsed.scheme}://{parsed.netloc}"

        get_resp = scraper.get(landing_url, timeout=12)
        dbg['get_status'] = get_resp.status_code

        post_data = {
            'op': 'download2',
            'id': file_code,
            'rand': '',
            'referer': '',
            'method_free': '',
            'method_premium': '',
        }
        resp = scraper.post(base + '/', data=post_data, timeout=15,
                            headers={'Referer': landing_url})
        html = resp.text
        dbg['post_status'] = resp.status_code
        dbg['post_html_length'] = len(html)
        if debug:
            dbg['post_html_snippet'] = html[:2000]

        m = re.search(
            r"location\.href\s*=\s*[\x27\x22]"
            r"(https?://[^\x27\x22]+\.(?:mp4|mkv|webm|avi|zip|rar)[^\x27\x22]*)[\x27\x22]",
            html, re.IGNORECASE
        )
        if m:
            dbg['pattern'] = 'location_href_ext'
            return m.group(1), dbg

        m = re.search(r"location\.href\s*=\s*[\x27\x22]"
                      r"(https?://[^\x27\x22]{30,})[\x27\x22]", html)
        if m:
            url = m.group(1)
            if any(x in url.lower() for x in ['/dl/', 'kissorgrab', 'cdn']):
                dbg['pattern'] = 'location_href_cdn'
                return url, dbg

        m = re.search(
            r'href=["|\x27]((https?://)[^"|\x27?\s]{10,}\.(?:mp4|mkv|webm|avi|zip|rar))["|\x27]',
            html, re.IGNORECASE
        )
        if m:
            dbg['pattern'] = 'href_ext'
            return m.group(1), dbg

        dbg['error'] = 'no_pattern_matched'
        return None, dbg

    except Exception as e:
        return None, {'exception': str(e)}


def _fetch_html_safe(url):
    try:
        scraper = _get_scraper()
        resp = scraper.get(url, timeout=15, allow_redirects=True)
        return resp.text, None
    except Exception as e1:
        try:
            headers = {
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
                'Accept-Language': 'en-US,en;q=0.9',
            }
            resp = requests.get(url, headers=headers, timeout=12, allow_redirects=True)
            return resp.text, None
        except Exception as e2:
            return None, f"cloudscraper: {e1} | requests: {e2}"


def _extract_download_url(html, host):
    m = re.search(
        r"\.html\(['\"].*?href=[\\'\"]+((https?://)[^\'\"\\ ]+\?pt=[^\'\"\\ ]+)[\\'\"]\",",
        html, re.DOTALL
    )
    if m: return m.group(1)

    m = re.search(r"href=['\"](https?://[^'\">\s]+\?pt=[^'\">\s]+)['\"]", html)
    if m: return m.group(1)

    m = re.search(r"[\x27\x22]((https?://)[^\x27\x22]{5,}\?pt=[^\x27\x22]{10,})[\x27\x22]", html)
    if m: return m.group(1)

    m = re.search(r"location\.href\s*=\s*['\"]"
                  r"(https?://[^'\"]{20,})['\"]", html)
    if m:
        url = m.group(1)
        if any(x in url.lower() for x in ['/dl/', 'kissorgrab', '.mkv', '.mp4', '.avi', '.zip']):
            return url

    m = re.search(
        r"window\.location(?:\.href)?\s*=\s*['\"]"
        r"(https?://[^'\"]+\.(?:mp4|mkv|webm|avi|zip|rar)[^'\"]*)['\"]\",",
        html, re.IGNORECASE
    )
    if m: return m.group(1)

    m = re.search(
        r"[\x27\x22](https?://[^\x27\x22?\s]{10,}\.(?:mp4|mkv|webm|avi|zip|rar))[\x27\x22]",
        html, re.IGNORECASE
    )
    if m: return m.group(1)

    return None


def _get_scraper():
    try:
        import cloudscraper
        return cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
    except Exception:
        session = requests.Session()
        session.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
        })
        return session


def _fetch_html(url):
    try:
        scraper = _get_scraper()
        resp = scraper.get(url, timeout=15, allow_redirects=True)
        return resp.text
    except Exception:
        return None


@require_GET
def stream_proxy(request):
    landing_url = request.GET.get('url', '').strip()
    if not landing_url:
        return JsonResponse({'error': 'No URL provided'}, status=400)

    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(landing_url)
    host = parsed.netloc.lower()
    lower = landing_url.lower()

    direct_exts = ('.mp4', '.mkv', '.webm', '.avi', '.mov')
    already_direct = '?pt=' in lower or any(lower.endswith(ext) for ext in direct_exts)

    if already_direct:
        direct_url = landing_url
    elif 'sabishares.com' in host and 'preview' in parsed.query:
        direct_url = urlunparse(parsed._replace(query='', fragment=''))
    elif 'downloadwella.com' in host:
        resolved, _ = _resolve_downloadwella(landing_url, parsed)
        direct_url = resolved if resolved else landing_url
    elif 'mylulutv.com' in host or 'kissorgrab.com' in host:
        direct_url = landing_url
    else:
        html, _ = _fetch_html_safe(landing_url)
        extracted = _extract_download_url(html, host) if html else None
        direct_url = extracted if extracted else landing_url

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Referer': landing_url,
        'Accept': '*/*',
    }

    range_header = request.META.get('HTTP_RANGE')
    if range_header:
        headers['Range'] = range_header

    try:
        upstream = requests.get(
            direct_url,
            headers=headers,
            stream=True,
            timeout=20,
            allow_redirects=True,
        )
    except Exception as e:
        return HttpResponse(f'Failed to connect to source: {e}', status=502)

    content_type = upstream.headers.get('Content-Type', 'video/mp4')
    if direct_url.lower().endswith('.mkv') or 'mkv' in content_type:
        content_type = 'video/x-matroska'

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=1024 * 512):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    status_code = upstream.status_code

    response = StreamingHttpResponse(
        generate(),
        status=status_code,
        content_type=content_type,
    )

    for header in ('Content-Length', 'Content-Range', 'Accept-Ranges'):
        value = upstream.headers.get(header)
        if value:
            response[header] = value

    if 'Accept-Ranges' not in upstream.headers:
        response['Accept-Ranges'] = 'bytes'

    response['Access-Control-Allow-Origin'] = '*'
    return response


# ── Views ─────────────────────────────────────────────────────────────────────

class HomeView(ListView):
    model = Movie
    template_name = 'movies/home.html'
    context_object_name = 'movies'
    paginate_by = 12

    def get_queryset(self):
        return (
            Movie.objects
            .only('id', 'title', 'slug', 'image_url', 'created_at', 'title_b', 'vi_year')
            .filter(
                Q(is_series=False),
                Q(title_b__isnull=True) | Q(title_b=''),
            )
            .order_by('-created_at')
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # ── Cached heavy queries ──────────────────────────────────────────────
        cached = cache.get('movies_home_ctx_v1')
        if cached is None:
            cached = _build_movies_home_context()
            cache.set('movies_home_ctx_v1', cached, MOVIES_HOME_CACHE_TTL)
        context.update(cached)

        # ── Series sections — cached separately (not user-specific) ──────────
        ongoing_cached = cache.get('home_ongoing_series_v1')
        if ongoing_cached is None:
            ongoing_cached = list(
                Movie.objects
                .only('id', 'title', 'slug', 'title_b', 'image_url',
                      'title_b_updated_at', 'created_at')
                .filter(
                    Q(is_series=True) | (Q(title_b__isnull=False) & ~Q(title_b='')),
                    completed=False,
                )
                .order_by('-title_b_updated_at', '-created_at')[:27]  # 3 pages × 9
            )
            cache.set('home_ongoing_series_v1', ongoing_cached, MOVIES_HOME_CACHE_TTL)

        comp_cached = cache.get('home_completed_series_v1')
        if comp_cached is None:
            comp_cached = list(
                Movie.objects
                .only('id', 'title', 'slug', 'title_b', 'image_url',
                      'title_b_updated_at', 'created_at')
                .filter(
                    Q(is_series=True) | (Q(title_b__isnull=False) & ~Q(title_b='')),
                    completed=True,
                )
                .order_by('-title_b_updated_at', '-created_at')[:27]
            )
            cache.set('home_completed_series_v1', comp_cached, MOVIES_HOME_CACHE_TTL)

        context['ongoing_series'] = Paginator(ongoing_cached, 9).get_page(
            self.request.GET.get('ongoing_page', 1)
        )
        context['completed_series'] = Paginator(comp_cached, 9).get_page(
            self.request.GET.get('completed_page', 1)
        )

        return context


class CategoryMoviesView(ListView):
    """
    Per-category movie listing.
    No @cache_page — that decorator caches the full HTTP response globally,
    meaning one user's 404 or redirect could be served to everyone.
    Query-level caching (30 min) is used instead.
    """
    model = Movie
    template_name = 'movies/movie_list_by_cat.html'
    context_object_name = 'movies'
    paginate_by = 12

    def get(self, request, *args, **kwargs):
        self.category = get_object_or_404(Category, id=self.kwargs['cat_id'])
        if self.kwargs.get('slug') != self.category.slug:
            return redirect(self.category.get_absolute_url(), permanent=True)
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        self.category = get_object_or_404(Category, id=self.kwargs['cat_id'])
        cache_key = f'cat_movies_{self.category.pk}_v1'
        qs = cache.get(cache_key)
        if qs is None:
            qs = list(
                Movie.objects
                .only('id', 'title', 'slug', 'image_url', 'created_at', 'description', 'vi_year')
                .filter(categories=self.category)
                .order_by('-created_at')
            )
            cache.set(cache_key, qs, CATEGORY_PAGE_CACHE_TTL)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['category'] = self.category
        context['categories'] = get_sidebar_categories()
        return context


def old_movie_redirect(request, pk):
    movie = get_object_or_404(Movie, pk=pk)
    return redirect(movie.get_absolute_url(), permanent=True)


def old_category_redirect(request, cat_id):
    category = get_object_or_404(Category, pk=cat_id)
    return redirect(category.get_absolute_url(), permanent=True)


class MovieDetailView(DetailView):
    model = Movie
    template_name = 'movies/movie_detail.html'

    def get_queryset(self):
        return Movie.objects.prefetch_related(
            'liked_by', 'watchlisted_by', 'categories', 'comments__user'
        )

    def get_object(self, queryset=None):
        if queryset is None:
            queryset = self.get_queryset()
        obj = get_object_or_404(queryset, pk=self.kwargs['pk'])
        Movie.objects.filter(pk=obj.pk).update(views=F('views') + 1)
        obj.refresh_from_db(fields=['views'])
        return obj

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        url_slug = kwargs.get('slug', '')
        if url_slug != self.object.slug:
            return redirect(self.object.get_absolute_url(), permanent=True)
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        movie = context['object']
        request = self.request
        user = request.user

        # ── SEO: compute from already-prefetched categories (ONE query total) ─
        # categories were prefetched in get_queryset — no extra DB hit here
        movie_categories = list(movie.categories.all())   # uses prefetch cache
        category_names = [c.name.lower() for c in movie_categories]
        country = (movie.vi_country or '').lower()

        if 'chinese drama' in category_names or 'chinese' in country:
            seo_type = 'Chinese Drama'
        elif 'korean drama' in category_names or 'k drama' in category_names or 'korean' in country:
            seo_type = 'Korean Drama'
        elif 'thai drama' in category_names or 'thai' in country:
            seo_type = 'Thai Drama'
        elif 'turkish drama' in category_names or 'turkish' in country:
            seo_type = 'Turkish Drama'
        elif 'spanish drama' in category_names or 'spanish' in country:
            seo_type = 'Spanish Drama'
        elif 'filipino drama' in category_names or 'filipino' in category_names:
            seo_type = 'Filipino Drama'
        elif 'anime' in category_names:
            seo_type = 'Anime Series'
        elif 'nollywood tv series' in category_names:
            seo_type = 'Nollywood Series'
        elif 'hollywood tv series' in category_names:
            seo_type = 'Hollywood TV Series'
        elif 'sa series' in category_names or 'south africa' in category_names:
            seo_type = 'South African Series'
        elif 'tv series' in category_names or 'series' in category_names:
            seo_type = 'TV Series'
        elif 'japanese movie' in category_names:
            seo_type = 'Japanese Movie'
        elif 'animation movie' in category_names:
            seo_type = 'Animation Movie'
        elif 'bollywood' in category_names or 'bollywood movies' in category_names:
            seo_type = 'Bollywood Movie'
        elif 'nollywood movie' in category_names or 'nollywood movies' in category_names or 'nollywood' in category_names:
            seo_type = 'Nollywood Movie'
        elif 'hollywood movie' in category_names or 'hollywood movies' in category_names or 'hollywood' in category_names:
            seo_type = 'Hollywood Movie'
        elif '18plus' in category_names or '18+ movie' in category_names or 'adult' in category_names:
            seo_type = 'Adult Movie'
        else:
            seo_type = 'Movie'

        is_series = any(word in seo_type.lower() for word in ['drama', 'series', 'anime'])
        completion_label = ('(Complete)' if movie.completed else '(Ongoing)') if is_series else ''

        context['seo_type'] = seo_type
        context['is_series'] = is_series
        context['completion_label'] = completion_label

        # ── Like / watchlist — use prefetched sets, no extra queries ──────────
        if user.is_authenticated:
            liked_ids      = {u.pk for u in movie.liked_by.all()}
            watchlisted_ids = {u.pk for u in movie.watchlisted_by.all()}
            context['is_liked']       = user.pk in liked_ids
            context['is_watchlisted'] = user.pk in watchlisted_ids
        else:
            context['is_liked']       = False
            context['is_watchlisted'] = False

        # ── Comments (prefetched in get_queryset) ─────────────────────────────
        context['comments'] = movie.comments.filter(
            parent__isnull=True
        ).select_related('user').prefetch_related(
            'replies__user'
        ).order_by('-created_at')

        context['comment_form'] = CommentForm()

        # ── Related movies — by category, deterministic order (NO order_by('?'))
        # order_by('?') = ORDER BY RANDOM() = full table scan every request.
        # Use pk descending (fast index scan) filtered by same category instead.
        if movie_categories:
            related_movies = list(
                Movie.objects
                .only('id', 'title', 'slug', 'image_url', 'created_at')
                .filter(categories__in=movie_categories)
                .exclude(id=movie.id)
                .distinct()
                .order_by('-created_at')[:12]
            )
        else:
            related_movies = list(
                Movie.objects
                .only('id', 'title', 'slug', 'image_url', 'created_at')
                .exclude(id=movie.id)
                .order_by('-created_at')[:12]
            )

        context['related_movies'] = related_movies
        context['categories']     = get_sidebar_categories()
        context['full_image_url'] = request.build_absolute_uri(movie.image_url)
        context['full_video_url'] = request.build_absolute_uri(movie.video_url)
        context['logo_url']       = request.build_absolute_uri(static('img/logo.png'))

        return context

    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')

        movie = self.get_object()
        form = CommentForm(request.POST)
        if form.is_valid():
            comment = form.save(commit=False)
            comment.movie = movie
            comment.user = request.user
            comment.save()
            messages.success(request, "Comment added.")

        return redirect(movie.get_absolute_url())


@login_required
def toggle_like(request, pk):
    movie = get_object_or_404(Movie, pk=pk)
    user = request.user
    if movie.liked_by.filter(pk=user.pk).exists():
        movie.liked_by.remove(user)
    else:
        movie.liked_by.add(user)
    return redirect(movie.get_absolute_url())


@login_required
def toggle_watchlist(request, pk):
    movie = get_object_or_404(Movie, pk=pk)
    user = request.user
    if movie.watchlisted_by.filter(pk=user.pk).exists():
        movie.watchlisted_by.remove(user)
    else:
        movie.watchlisted_by.add(user)
    return redirect(movie.get_absolute_url())


class SearchResultsView(ListView):
    """
    Search results — no @cache_page (it would cache one user's results for all).
    Query-level caching per search term instead.
    """
    model = Movie
    template_name = 'movies/search_results.html'
    context_object_name = 'movies'
    paginate_by = 12

    def get_queryset(self):
        query = self.request.GET.get('q', '').strip()
        if not query:
            return Movie.objects.none()

        search_cache_key = f'search_{hash(query.lower())}'
        cached_results = cache.get(search_cache_key)
        if cached_results is not None:
            return cached_results

        base_qs = Movie.objects.only(
            'id', 'title', 'slug', 'description', 'image_url', 'created_at'
        )

        exact_q = Q(title__icontains=query) | Q(description__icontains=query)
        exact_matches = list(base_qs.filter(exact_q).distinct())

        if exact_matches:
            cache.set(search_cache_key, exact_matches, 60 * 30)
            return exact_matches

        keywords = query.split()
        fallback_q = Q()
        for kw in keywords:
            fallback_q |= Q(title__icontains=kw) | Q(description__icontains=kw)

        keyword_results = list(base_qs.filter(fallback_q).distinct())

        def count_matches(movie):
            text = f"{movie.title} {movie.description}".lower()
            return sum(kw.lower() in text for kw in keywords)

        sorted_results = sorted(keyword_results, key=count_matches, reverse=True)
        cache.set(search_cache_key, sorted_results, 60 * 30)
        return sorted_results

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['query'] = self.request.GET.get('q', '')
        context['categories'] = get_sidebar_categories()
        return context


from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
import json


@csrf_exempt
def pwa_install_tracking(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            PWAInstallation.objects.create(
                user=request.user if request.user.is_authenticated else None,
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
                platform=data.get('platform', 'unknown')
            )
            return JsonResponse({'success': True})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    return JsonResponse({'success': False, 'error': 'Invalid method'})


@login_required
def sync_offline_actions(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            actions = data.get('actions', [])
            for action_data in actions:
                OfflineAction.objects.create(
                    user=request.user,
                    action_type=action_data.get('type'),
                    action_data=action_data.get('data', {}),
                    synced=True
                )
            return JsonResponse({'success': True, 'synced': len(actions)})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    return JsonResponse({'success': False, 'error': 'Invalid method'})


@require_POST
def add_comment(request, pk):
    movie = get_object_or_404(Movie, pk=pk)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    content = request.POST.get('content', '').strip()

    if not content:
        if is_ajax:
            return JsonResponse({'success': False, 'message': 'Comment cannot be empty'})
        messages.error(request, 'Comment cannot be empty')
        return redirect(movie.get_absolute_url())

    comment = Comment()
    comment.movie = movie
    comment.content = content

    if request.user.is_authenticated:
        comment.user = request.user
    else:
        guest_name = request.POST.get('name', '').strip()
        if not guest_name:
            if is_ajax:
                return JsonResponse({'success': False, 'message': 'Please provide your name'})
            messages.error(request, 'Please provide your name')
            return redirect(movie.get_absolute_url())
        comment.guest_name = guest_name

    comment.save()

    if is_ajax:
        html = render_to_string('movies/components/comment_item.html', {
            'comment': comment,
            'movie': movie,
            'user': request.user
        })
        return JsonResponse({
            'success': True,
            'message': 'Comment posted successfully!',
            'html': html,
            'comment_id': comment.id
        })

    messages.success(request, 'Comment posted successfully!')
    return redirect(movie.get_absolute_url() + '#comments-section')


@require_POST
def add_reply(request, movie_pk, comment_pk):
    movie = get_object_or_404(Movie, pk=movie_pk)
    parent_comment = get_object_or_404(Comment, pk=comment_pk)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    content = request.POST.get('content', '').strip()

    if not content:
        if is_ajax:
            return JsonResponse({'success': False, 'message': 'Reply cannot be empty'})
        messages.error(request, 'Reply cannot be empty')
        return redirect(movie.get_absolute_url())

    reply = Comment()
    reply.movie = movie
    reply.parent = parent_comment
    reply.content = content

    if request.user.is_authenticated:
        reply.user = request.user
    else:
        guest_name = request.POST.get('name', '').strip()
        if not guest_name:
            if is_ajax:
                return JsonResponse({'success': False, 'message': 'Please provide your name'})
            messages.error(request, 'Please provide your name')
            return redirect(movie.get_absolute_url())
        reply.guest_name = guest_name

    reply.save()

    if is_ajax:
        html = render_to_string('movies/components/comment_item.html', {
            'comment': reply,
            'movie': movie,
            'user': request.user
        })
        return JsonResponse({
            'success': True,
            'message': 'Reply posted successfully!',
            'html': html,
            'comment_id': reply.id
        })

    messages.success(request, 'Reply posted successfully!')
    return redirect(movie.get_absolute_url() + '#comments-section')


@require_POST
def delete_comment(request, pk):
    comment = get_object_or_404(Comment, pk=pk)
    movie = comment.movie

    if request.user.is_authenticated and (request.user == comment.user or request.user.is_staff):
        comment.delete()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': True, 'message': 'Comment deleted successfully'})
        messages.success(request, 'Comment deleted successfully')
    else:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'message': 'You do not have permission to delete this comment'})
        messages.error(request, 'You do not have permission to delete this comment')

    return redirect(movie.get_absolute_url() + '#comments-section')