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
from django.db.models import Q, Prefetch
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

# Add these imports at the top of views.py
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST, require_GET
import requests
import re
from django.db import models as django_models

from django.http import StreamingHttpResponse
import requests


# Cache key constants
SIDEBAR_CATEGORIES_CACHE_KEY = 'sidebar_categories_v2'
CACHE_VERSION = 1

def get_sidebar_categories():
    """
    Cached function to get sidebar categories - reduces repeated database queries
    """
    categories = cache.get(SIDEBAR_CATEGORIES_CACHE_KEY, version=CACHE_VERSION)
    if not categories:
        # Names matched case-insensitively so "Tv series" / "TV Series" both work.
        # We pick the category with the MOST movies for each slot.
        target_categories = [
            'Hollywood movies',
            # 'Nollywood movies',
            'Korean drama',
            'TV Series',
        ]

        from django.db.models import Count as _Count, Q as _Q
        import functools as _ft, operator as _op

        # Build a case-insensitive OR filter across all target names
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
        
        # Order categories as specified (case-insensitive lookup)
        category_order = {name.lower(): i for i, name in enumerate(target_categories)}
        categories_list = [cat for cat in categories_qs if cat.latest_movies]
        categories_list.sort(key=lambda cat: category_order.get(cat.name.lower(), 999))
        
        # Cache for 4 hours
        cache.set(SIDEBAR_CATEGORIES_CACHE_KEY, categories_list, 60 * 60 * 4, version=CACHE_VERSION)
        categories = categories_list
    
    return categories

def invalidate_sidebar_cache():
    """
    Call this when adding/updating movies to refresh sidebar cache
    """
    cache.delete(SIDEBAR_CATEGORIES_CACHE_KEY, version=CACHE_VERSION)

def robots_txt(request):
    lines = [
        "User-agent: *",
        "Disallow: /admin/",
        "Sitemap: https://watch2d.net/sitemap.xml",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")

def custom_404_view(request, exception):
    """
    Custom 404 view that shows only specific categories
    """
    context = {
        'categories': get_sidebar_categories(),
    }
    
    return render(request, 'movies/404.html', context, status=404)

def ping_view(request):
    return JsonResponse({"status": "OK"})


# Hosts whose URLs play directly in the browser video player
STREAMABLE_HOSTS = [
    'mylulutv.com',
    'kissorgrab.com',
    'ma27b.kissorgrab.com',
]

# Hosts that require manual redirect — cannot stream directly
MANUAL_HOSTS = [
    'ww1.sabishares.com',
    'downloadwella.com',
    'meetdownload.com',
]


@require_GET
def check_streamable(request):
    """
    Returns whether a URL is directly streamable or needs manual redirect.
    Called by frontend to decide whether to show/hide the stream player.
    """
    url = request.GET.get('url', '').strip()
    if not url:
        return JsonResponse({'streamable': False, 'reason': 'no_url'})

    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    lower = url.lower()

    # Already a direct resolved file
    direct_exts = ('.mp4', '.mkv', '.webm', '.avi', '.mov')
    if any(lower.endswith(ext) for ext in direct_exts) or '?pt=' in lower:
        return JsonResponse({'streamable': True, 'reason': 'direct_file'})

    # sabishares.com/file/?preview → direct after stripping ?preview
    if 'sabishares.com' in host and '/file/' in lower and 'preview' in lower:
        return JsonResponse({'streamable': True, 'reason': 'sabishares_preview'})

    # Known streamable hosts
    if any(h in host for h in STREAMABLE_HOSTS):
        return JsonResponse({'streamable': True, 'reason': 'known_streamable_host'})

    # Known landing-page hosts
    if any(h in host for h in MANUAL_HOSTS):
        return JsonResponse({'streamable': False, 'reason': 'landing_page_host'})

    # Unknown — optimistically try
    return JsonResponse({'streamable': True, 'reason': 'unknown'})


@require_GET
def resolve_download_link(request):
    """
    Server-side proxy. Handles all known link formats:
      1. sabishares.com ?preview  -> strip ?preview = direct URL
      2. ww1.sabishares.com       -> fetch page, extract ?pt= token from JS
      3. meetdownload.com         -> fetch page, extract kissorgrab.com/dl/ URL
      4. downloadwella.com        -> POST form (op=download2) to get direct URL
      5. mylulutv.com             -> already streamable, return as-is
    Add ?debug=1 (staff only) to see full diagnostic output.
    """
    landing_url = request.GET.get('url', '').strip()
    debug = request.GET.get('debug') == '1' and request.user.is_staff

    if not landing_url:
        return JsonResponse({'error': 'No URL provided'}, status=400)

    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(landing_url)
    host = parsed.netloc.lower()
    lower = landing_url.lower()

    # 1. sabishares.com ?preview — the URL itself (minus ?preview) IS the download
    if 'sabishares.com' in host and 'preview' in parsed.query:
        direct = urlunparse(parsed._replace(query='', fragment=''))
        if debug:
            return JsonResponse({'method': 'sabishares_preview', 'download_url': direct})
        return JsonResponse({'download_url': direct})

    # Already a resolved direct URL
    direct_exts = ('.mp4', '.mkv', '.webm', '.avi', '.mov', '.zip', '.rar')
    if '?pt=' in lower or any(lower.endswith(ext) for ext in direct_exts):
        return JsonResponse({'download_url': landing_url})

    # mylulutv.com — direct, return as-is
    if 'mylulutv.com' in host:
        return JsonResponse({'download_url': landing_url})

    # 4. downloadwella.com — POST form
    if 'downloadwella.com' in host:
        result, dbg = _resolve_downloadwella(landing_url, parsed, debug)
        if result:
            if debug:
                return JsonResponse({'method': 'downloadwella_post', 'download_url': result, 'debug': dbg})
            return JsonResponse({'download_url': result})
        if debug:
            return JsonResponse({'method': 'downloadwella_failed', 'fallback': landing_url, 'debug': dbg})
        return JsonResponse({'download_url': landing_url})

    # 2 & 3. ww1.sabishares.com / meetdownload.com — fetch page + parse
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
    """
    downloadwella.com: POST op=download2 to get real download URL.
    File code = first path segment: /w2osk0e4fg0c/File.mkv.html -> w2osk0e4fg0c
    Returns (url_or_None, debug_dict).
    """
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

        # location.href with file extension
        m = re.search(
            r"location\.href\s*=\s*[\x27\x22]"
            r"(https?://[^\x27\x22]+\.(?:mp4|mkv|webm|avi|zip|rar)[^\x27\x22]*)[\x27\x22]",
            html, re.IGNORECASE
        )
        if m:
            dbg['pattern'] = 'location_href_ext'
            return m.group(1), dbg

        # location.href with CDN/kissorgrab path
        m = re.search(r"location\.href\s*=\s*[\x27\x22]"
                      r"(https?://[^\x27\x22]{30,})[\x27\x22]", html)
        if m:
            url = m.group(1)
            if any(x in url.lower() for x in ['/dl/', 'kissorgrab', 'cdn']):
                dbg['pattern'] = 'location_href_cdn'
                return url, dbg

        # href with file extension
        m = re.search(
            r'href=["\x27]((https?://)[^"\x27?\s]{10,}\.(?:mp4|mkv|webm|avi|zip|rar))["\x27]',
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
    """Fetch page HTML. Returns (html_or_None, error_string)."""
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
    """Extract real download URL from fetched page HTML."""

    # ww1.sabishares.com — ?pt= token inside jQuery .html("...<a href='...?pt=...'>...")
    # The token is hardcoded in the page JS, just needs a direct regex match
    m = re.search(r"href='(https?://[^']+\?pt=[^']+)'", html)
    if m: return m.group(1)

    m = re.search(r'href="(https?://[^"]+\?pt=[^"]+)"', html)
    if m: return m.group(1)

    # Any string containing ?pt= (catches it inside .html() JS calls too)
    m = re.search(r"[\x27\x22]((https?://)[^\x27\x22]{5,}\?pt=[^\x27\x22]{10,})[\x27\x22]", html)
    if m: return m.group(1)

    # meetdownload.com — location.href = 'https://ma27b.kissorgrab.com/dl/...'
    # This URL is inside a <div class="bezende"> hidden offscreen
    m = re.search(r"location\.href\s*=\s*'(https?://[^']{20,})'", html)
    if m:
        url = m.group(1)
        if any(x in url.lower() for x in ['/dl/', 'kissorgrab', '.mkv', '.mp4', '.avi']):
            return url

    m = re.search(r'location\.href\s*=\s*"(https?://[^"]{20,})"', html)
    if m:
        url = m.group(1)
        if any(x in url.lower() for x in ['/dl/', 'kissorgrab', '.mkv', '.mp4', '.avi']):
            return url

    # window.location with file extension
    m = re.search(
        r"window\.location(?:\.href)?\s*=\s*[\x27\x22]"
        r"(https?://[^\x27\x22]+\.(?:mp4|mkv|webm|avi|zip|rar)[^\x27\x22]*)[\x27\x22]",
        html, re.IGNORECASE
    )
    if m: return m.group(1)

    # Direct file URL in any JS string
    m = re.search(
        r"[\x27\x22](https?://[^\x27\x22?\s]{10,}\.(?:mp4|mkv|webm|avi|zip|rar))[\x27\x22]",
        html, re.IGNORECASE
    )
    if m: return m.group(1)

    return None


def _get_scraper():
    """Return a cloudscraper instance, fall back to a requests Session."""
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
    """GET a page using cloudscraper (Cloudflare bypass), fall back to requests."""
    try:
        scraper = _get_scraper()
        resp = scraper.get(url, timeout=15, allow_redirects=True)
        return resp.text
    except Exception:
        return None


@require_GET
def stream_proxy(request):
    """
    Range-aware streaming proxy.

    Flow:
      1. Receives a landing URL (e.g. downloadwella.com link)
      2. Resolves it to a fresh direct file URL using the same logic as download
      3. Opens a connection to that URL, forwarding any Range header from the browser
      4. Streams the response bytes back to the browser chunk by chunk

    This means:
      - The file never touches your disk
      - Seeking works (if the source server supports range requests)
      - The link is always freshly resolved so expiry is not an issue
      - Works for .mkv, .mp4, .avi — whatever the source serves
    """
    landing_url = request.GET.get('url', '').strip()
    if not landing_url:
        from django.http import JsonResponse
        return JsonResponse({'error': 'No URL provided'}, status=400)

    # ── Step 1: Resolve the landing URL to a fresh direct URL ────────────────
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
        # Generic: fetch page and extract
        html, _ = _fetch_html_safe(landing_url)
        extracted = _extract_download_url(html, host) if html else None
        direct_url = extracted if extracted else landing_url

    # ── Step 2: Forward range header from browser (enables seeking) ──────────
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

    # ── Step 3: Open connection to the source (stream=True = no full download) ─
    try:
        upstream = requests.get(
            direct_url,
            headers=headers,
            stream=True,
            timeout=20,
            allow_redirects=True,
        )
    except Exception as e:
        from django.http import HttpResponse
        return HttpResponse(f'Failed to connect to source: {e}', status=502)

    # ── Step 4: Build the streaming response ─────────────────────────────────
    content_type = upstream.headers.get('Content-Type', 'video/mp4')

    # Detect mkv and set correct MIME type
    if direct_url.lower().endswith('.mkv') or 'mkv' in content_type:
        content_type = 'video/x-matroska'

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=1024 * 512):  # 512 KB chunks
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    status_code = upstream.status_code  # 206 for range, 200 for full

    response = StreamingHttpResponse(
        generate(),
        status=status_code,
        content_type=content_type,
    )

    # Forward relevant headers from upstream so browser can seek properly
    for header in ('Content-Length', 'Content-Range', 'Accept-Ranges'):
        value = upstream.headers.get(header)
        if value:
            response[header] = value

    # If source didn't declare Accept-Ranges, add it ourselves optimistically
    if 'Accept-Ranges' not in upstream.headers:
        response['Accept-Ranges'] = 'bytes'

    # Allow embedding in the video player
    response['Access-Control-Allow-Origin'] = '*'

    return response

def _extract_download_url(html, host):
    """
    Extract the real download/stream URL from page HTML.
    Covers ww1.sabishares.com (?pt= inside jQuery html() call)
    and meetdownload.com (location.href in onclick JS).
    """
    # ── ww1.sabishares.com: ?pt= token inside jQuery .html("...") ────────────
    # e.g. $('.download-timer').html("<a ... href='https://...?pt=...'>...")
    m = re.search(
        r"\.html\(['\"].*?href=[\\'\"]+(https?://[^'\"\\]+\?pt=[^'\"\\]+)[\\'\"]",
        html, re.DOTALL
    )
    if m: return m.group(1)

    # Simpler fallback: any href with ?pt= anywhere in the page
    m = re.search(r"href=['\"]?(https?://[^'\">\s]+\?pt=[^'\">\s]+)['\"]?", html)
    if m: return m.group(1)

    # ── meetdownload.com: location.href = 'https://ma27b.kissorgrab.com/dl/...' ─
    m = re.search(
        r"location\.href\s*=\s*['\"]"
        r"(https?://[^'\"]{20,})['\"]",
        html
    )
    if m:
        url = m.group(1)
        # Make sure it's not an ad/tracker URL — must look like a file path
        if any(ext in url.lower() for ext in ['/dl/', '.mkv', '.mp4', '.avi', '.zip']):
            return url

    # ── Generic: window.location with file extension ──────────────────────────
    m = re.search(
        r"window\.location(?:\.href)?\s*=\s*['\"]"
        r"(https?://[^'\"]+\.(?:mp4|mkv|webm|avi|zip|rar)[^'\"]*)['\"]",
        html, re.IGNORECASE
    )
    if m: return m.group(1)

    # ── Generic: any direct file URL in JS strings ────────────────────────────
    m = re.search(
        r"[\x27\x22](https?://[^\x27\x22?\s]{10,}\.(?:mp4|mkv|webm|avi|zip|rar))[\x27\x22]",
        html, re.IGNORECASE
    )
    if m: return m.group(1)

    return None

# @method_decorator(cache_page(60 * 60 * 4), name='dispatch')  # cache 4h
class HomeView(ListView):
    model = Movie
    template_name = 'movies/home.html'
    context_object_name = 'movies'
    paginate_by = 12

    def get_queryset(self):
        # Recently Uploaded = standalone movies only (not series).
        # Exclude anything marked as a series, OR that has episode info (title_b).
        return (
            Movie.objects
                 .only('id', 'title', 'image_url', 'created_at', 'title_b', 'vi_year')
                 .filter(
                     Q(is_series=False),
                     Q(title_b__isnull=True) | Q(title_b=''),
                 )
                 .order_by('-created_at')
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # ── 0. Blockbusters — AUTO: top views >= 1 000, no manual flag needed ──
        block_qs = (
            Movie.objects
                 .only('id', 'title', 'image_url', 'created_at', 'views')
                 .filter(views__gte=1000)
                 .order_by('-views', '-created_at')
        )
        context['blockbusters'] = block_qs[:12]

        # ── 1. Trending Now (top 24 by all-time views > 0) ──
        context['trending'] = (
            Movie.objects
                 .only('id', 'title', 'image_url', 'views', 'created_at')
                 .filter(views__gt=0)
                 .order_by('-views', '-created_at')[:24]
        )

        # ── 2. Sidebar categories (cached) ──
        context['categories'] = get_sidebar_categories()

        # ── 3. All categories for the "Browse by Category" grid at the bottom ──
        # Deduplicate near-identical categories (e.g. "Hollywood", "Hollywood movie",
        # "Hollywood movies") by grouping on a normalised key and keeping only the
        # category with the highest movie count in each group.
        import re as _re
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

        # Group by normalised key; first entry (highest count) wins
        seen_keys = {}
        deduped = []
        for cat in raw_cats:
            key = _norm(cat.name)
            if key and key not in seen_keys:
                seen_keys[key] = True
                deduped.append(cat)

        # Re-sort alphabetically for display (strip leading emoji/symbols)
        deduped.sort(key=lambda c: _re.sub(r'[^\w\s]', '', c.name).strip().lower())
        context['all_categories'] = deduped

        # ── 4. Ongoing series ──
        # A series is ongoing when: is_series=True AND completed=False.
        # Also catch older entries that may not have is_series set but have
        # episode info (title_b) and are not yet marked completed.
        ongoing_qs = (
            Movie.objects
                 .only('id', 'title', 'title_b', 'image_url', 'title_b_updated_at', 'created_at')
                 .filter(
                     Q(is_series=True) | (Q(title_b__isnull=False) & ~Q(title_b='')),
                     completed=False,
                 )
                 .order_by('-title_b_updated_at', '-created_at')
        )
        context['ongoing_series'] = Paginator(ongoing_qs, 9).get_page(
            self.request.GET.get('ongoing_page', 1)
        )

        # ── 5. Latest episodes row (horizontal scroll, same queryset) ──
        # context['new_episodes'] = Paginator(ongoing_qs, 6).get_page(
        #     self.request.GET.get('new_page', 1)
        # )

        # ── 6. Completed series ──
        # A series is completed when: (is_series=True OR has episode info) AND completed=True.
        comp_ser = (
            Movie.objects
                 .only('id', 'title', 'title_b', 'image_url', 'title_b_updated_at', 'created_at')
                 .filter(
                     Q(is_series=True) | (Q(title_b__isnull=False) & ~Q(title_b='')),
                     completed=True,
                 )
                 .order_by('-title_b_updated_at', '-created_at')
        )
        context['completed_series'] = Paginator(comp_ser, 9).get_page(
            self.request.GET.get('completed_page', 1)
        )

        return context

@method_decorator(cache_page(60 * 60 * 4), name='dispatch')  # 4 hours instead of 24
class CategoryMoviesView(ListView):
    model = Movie
    template_name = 'movies/movie_list.html'
    context_object_name = 'movies'
    paginate_by = 12

    def get_queryset(self):
        self.category = get_object_or_404(Category, id=self.kwargs['cat_id'])
        # Show all movies in this category, newest first - optimized query
        return Movie.objects.select_related().only(
            'id', 'title', 'image_url', 'created_at', 'description', 'vi_year'
        ).filter(categories=self.category).order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['category'] = self.category
        
        # Use cached sidebar categories
        context['categories'] = get_sidebar_categories()
        return context


def old_movie_redirect(request, pk):
    """
    Permanent (301) redirect from the legacy /movie/<pk>/ URL to the new
    canonical /movie/<pk>/<slug>/ URL.

    This keeps all 20K existing links working while Google transfers ranking
    power to the new SEO-friendly addresses.
    """
    movie = get_object_or_404(Movie, pk=pk)
    return redirect(movie.get_absolute_url(), permanent=True)


class MovieDetailView(DetailView):
    model = Movie
    template_name = 'movies/movie_detail.html'

    def get_queryset(self):
        # Optimize the detail query with prefetch_related for likes/watchlists
        return Movie.objects.prefetch_related(
            'liked_by', 'watchlisted_by', 'categories', 'comments__user'
        )

    def get_object(self, queryset=None):
        # Look up by pk only — slug is checked separately for canonical redirect.
        # Using pk keeps the query fast even with 20K+ posts.
        if queryset is None:
            queryset = self.get_queryset()
        obj = get_object_or_404(queryset, pk=self.kwargs['pk'])

        # ✅ Increment views count
        Movie.objects.filter(pk=obj.pk).update(views=F('views') + 1)
        obj.refresh_from_db(fields=['views'])

        return obj

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()

        # ── Canonical slug enforcement ────────────────────────────────────────
        # If someone arrives with the wrong slug (e.g. an old cached link or a
        # manually-typed URL), redirect them permanently to the correct one so
        # there is only ever ONE canonical URL per movie.
        url_slug = kwargs.get('slug', '')
        if url_slug != self.object.slug:
            return redirect(self.object.get_absolute_url(), permanent=True)

        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        movie = context['object']  # already fetched — don't call get_object() again (causes double view-count)
        request = self.request
        user = request.user

        # Like/watchlist status
        liked_users = set(movie.liked_by.all())
        watchlisted_users = set(movie.watchlisted_by.all())
        
        context['is_liked'] = user.is_authenticated and user in liked_users
        context['is_watchlisted'] = user.is_authenticated and user in watchlisted_users
        
        # Top-level comments with replies prefetched
        context['comments'] = movie.comments.filter(
            parent__isnull=True
        ).select_related('user').prefetch_related(
            'replies__user'
        ).order_by('-created_at')
        
        context['comment_form'] = CommentForm()

        # Related movies — by shared category; fall back to recent movies if none match
        movie_categories = movie.categories.all()
        if movie_categories.exists():
            related_movies = Movie.objects.only(
                'id', 'title', 'image_url', 'created_at'
            ).filter(
                categories__in=movie_categories
            ).exclude(id=movie.id).distinct().order_by('?')[:12]
        else:
            # Movie has no categories — show 12 most recent as fallback
            related_movies = Movie.objects.only(
                'id', 'title', 'image_url', 'created_at'
            ).exclude(id=movie.id).order_by('-created_at')[:12]

        context['related_movies'] = related_movies

        # Cached sidebar
        context['categories'] = get_sidebar_categories()

        # Structured data
        context['full_image_url'] = request.build_absolute_uri(movie.image_url)
        context['full_video_url'] = request.build_absolute_uri(movie.video_url)
        context['logo_url'] = request.build_absolute_uri(static('img/logo.png'))

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
    if user in movie.liked_by.all():
        movie.liked_by.remove(user)
    else:
        movie.liked_by.add(user)
    return redirect(movie.get_absolute_url())

@login_required
def toggle_watchlist(request, pk):
    movie = get_object_or_404(Movie, pk=pk)
    user = request.user
    if user in movie.watchlisted_by.all():
        movie.watchlisted_by.remove(user)
    else:
        movie.watchlisted_by.add(user)
    return redirect(movie.get_absolute_url())

@method_decorator(cache_page(60 * 15), name='dispatch')  # 15 minutes for search
class SearchResultsView(ListView):
    model = Movie
    template_name = 'movies/search_results.html'
    context_object_name = 'movies'
    paginate_by = 12

    def get_queryset(self):
        query = self.request.GET.get('q', '').strip()
        if not query:
            return Movie.objects.none()

        # Create cache key for search results
        search_cache_key = f'search_{hash(query.lower())}'
        cached_results = cache.get(search_cache_key)
        
        if cached_results is not None:
            return cached_results

        # Optimize query with only necessary fields
        base_qs = Movie.objects.select_related().only(
            'id', 'title', 'description', 'image_url', 'created_at'
        )

        # 1) Exact‐phrase match: title__icontains OR description__icontains
        exact_q = Q(title__icontains=query) | Q(description__icontains=query)
        exact_matches = list(base_qs.filter(exact_q).distinct())

        if exact_matches:
            # Cache for 30 minutes
            cache.set(search_cache_key, exact_matches, 60 * 30)
            return exact_matches

        # 2) Keyword fallback
        keywords = query.split()
        fallback_q = Q()
        for kw in keywords:
            fallback_q |= Q(title__icontains=kw) | Q(description__icontains=kw)

        keyword_results = list(base_qs.filter(fallback_q).distinct())

        # Rank by keyword matches
        def count_matches(movie):
            text = f"{movie.title} {movie.description}".lower()
            return sum(kw.lower() in text for kw in keywords)

        sorted_results = sorted(keyword_results, key=count_matches, reverse=True)
        
        # Cache for 30 minutes
        cache.set(search_cache_key, sorted_results, 60 * 30)
        return sorted_results

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['query'] = self.request.GET.get('q', '')
        
        # Use cached sidebar categories
        context['categories'] = get_sidebar_categories()
        return context
    
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
import json

@csrf_exempt
def pwa_install_tracking(request):
    """Track PWA installations"""
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
    """Sync offline actions when user comes back online"""
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

# Add these view functions to your views.py

@require_POST
def add_comment(request, pk):
    """
    Add a comment to a movie (AJAX endpoint)
    Supports both authenticated and anonymous users
    """
    movie = get_object_or_404(Movie, pk=pk)
    
    # Check if AJAX request
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    content = request.POST.get('content', '').strip()
    
    if not content:
        if is_ajax:
            return JsonResponse({
                'success': False,
                'message': 'Comment cannot be empty'
            })
        messages.error(request, 'Comment cannot be empty')
        return redirect(movie.get_absolute_url())
    
    # Create comment
    comment = Comment()
    comment.movie = movie
    comment.content = content
    
    if request.user.is_authenticated:
        comment.user = request.user
    else:
        guest_name = request.POST.get('name', '').strip()
        if not guest_name:
            if is_ajax:
                return JsonResponse({
                    'success': False,
                    'message': 'Please provide your name'
                })
            messages.error(request, 'Please provide your name')
            return redirect(movie.get_absolute_url())
        comment.guest_name = guest_name
    
    comment.save()
    
    if is_ajax:
        # Render the comment HTML
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
    """
    Add a reply to a comment (AJAX endpoint)
    """
    movie = get_object_or_404(Movie, pk=movie_pk)
    parent_comment = get_object_or_404(Comment, pk=comment_pk)
    
    # Check if AJAX request
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    content = request.POST.get('content', '').strip()
    
    if not content:
        if is_ajax:
            return JsonResponse({
                'success': False,
                'message': 'Reply cannot be empty'
            })
        messages.error(request, 'Reply cannot be empty')
        return redirect(movie.get_absolute_url())
    
    # Create reply
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
                return JsonResponse({
                    'success': False,
                    'message': 'Please provide your name'
                })
            messages.error(request, 'Please provide your name')
            return redirect(movie.get_absolute_url())
        reply.guest_name = guest_name
    
    reply.save()
    
    if is_ajax:
        # Render the reply HTML
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
    """
    Delete a comment (only owner or staff)
    """
    comment = get_object_or_404(Comment, pk=pk)
    movie = comment.movie
    
    # Check permissions
    if request.user.is_authenticated and (request.user == comment.user or request.user.is_staff):
        comment.delete()
        
        # Check if AJAX request
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': True,
                'message': 'Comment deleted successfully'
            })
        
        messages.success(request, 'Comment deleted successfully')
    else:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': 'You do not have permission to delete this comment'
            })
        
        messages.error(request, 'You do not have permission to delete this comment')
    
    return redirect(movie.get_absolute_url() + '#comments-section')