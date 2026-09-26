# movies/views.py
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.views.generic import ListView, DetailView, CreateView, TemplateView
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import reverse_lazy
from django.contrib.auth import login
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator
from .models import Movie, Category, Comment, CommentReaction, Review, ExternalRating, Person, MovieCast, MovieCrew, UpcomingTitle, NotifyRequest, Profile
from .forms import MovieForm, CommentForm, ReviewForm, DownloadLinkFormSet
from django.db.models import Q, Prefetch, Count, Avg
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
from django.views.decorators.csrf import csrf_exempt
import requests
import re
from django.db import models as django_models

from django.http import StreamingHttpResponse
import requests

import re as _re
import django.db.models as django_models



import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException

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
        .only('id', 'title', 'slug', 'image_url', 'created_at', 'views', 'rating', 'trailer_url')
        .prefetch_related('external_ratings')
        .filter(views__gte=1000)
        .order_by('-views', '-created_at')[:12]
    )

    # Instant Watch — has a working stream, no gate/download friction to see it
    ctx['instant_watch'] = list(
        Movie.objects
        .only('id', 'title', 'slug', 'image_url', 'created_at', 'views', 'rating', 'trailer_url')
        .prefetch_related('external_ratings')
        .exclude(stream_url='').exclude(stream_url__isnull=True)
        .order_by('-views', '-created_at')[:15]
    )

    # Trending This Week — weekly_views resets every Monday (see
    # reset_weekly_views), so this rotates instead of freezing on whatever
    # crossed 1000 all-time views first like "Most Watched" does.
    ctx['trending_this_week'] = list(
        Movie.objects
        .only('id', 'title', 'slug', 'image_url', 'created_at', 'weekly_views', 'rating', 'trailer_url')
        .prefetch_related('external_ratings')
        .filter(weekly_views__gt=0)
        .order_by('-weekly_views', '-created_at')[:12]
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

    # "Most Popular" compact ranked lists — split movies vs TV, one shared
    # cache entry each, sitewide.
    ctx['trending_movies_list'] = get_trending_list(10, is_series=False)
    ctx['trending_series_list'] = get_trending_list(10, is_series=True)

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
        ).filter(movie_count__gt=0).exclude(name__icontains='18+').order_by('-movie_count')
    )
    seen_keys = {}
    deduped = []
    for cat in raw_cats:
        key = _norm(cat.name)
        if key and key not in seen_keys:
            seen_keys[key] = True
            deduped.append(cat)
    deduped.sort(key=lambda c: _re.sub(r'[^\w\s]', '', c.name).strip().lower())

    # Top 5 posters per category (its most-watched titles) — powers the
    # crossfading "Browse by Category" tile carousel. Cheap, only runs inside
    # this 5-min cache.
    for cat in deduped:
        cat.preview_images = list(
            Movie.objects.filter(categories=cat)
            .exclude(image_url='').exclude(image_url__isnull=True)
            .order_by('-views').values_list('image_url', flat=True)[:5]
        )

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
                    'id', 'title', 'slug', 'image_url', 'created_at', 'rating', 'title_b', 'trailer_url'
                ).prefetch_related('external_ratings').order_by('-created_at')[:12],
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
    cache.delete('movies_home_ctx_v2')
    cache.delete('movies_categories_v1')
    cache.delete('trending_list_v1_None')
    cache.delete('trending_list_v1_True')
    cache.delete('trending_list_v1_False')


TRENDING_LIST_CACHE_TTL = 60 * 15  # 15 minutes


def get_trending_list(count=10, is_series=None):
    """
    RT-style "Most Popular" compact list — ranked by page views, sitewide.
    One shared cache entry per variant (not per-page) since the list is
    identical everywhere it's shown (home, every movie detail page).
    `is_series=None` mixes both; True/False splits into Movies vs TV Shows.
    """
    cache_key = f'trending_list_v1_{is_series}'
    trending = cache.get(cache_key)
    if trending is None:
        qs = Movie.objects.only(
            'id', 'title', 'slug', 'image_url', 'rating', 'views', 'is_series'
        ).prefetch_related('external_ratings')
        if is_series is not None:
            qs = qs.filter(is_series=is_series)
        trending = list(qs.order_by('-views', '-created_at')[:count])
        cache.set(cache_key, trending, TRENDING_LIST_CACHE_TTL)
    return trending


def robots_txt(request):
    # Bandwidth-heavy SEO / AI crawlers that bring ~no real traffic — block them
    # entirely. Compliant bots stop immediately (combine with Cloudflare's bot
    # rules for the ones that ignore robots.txt). Googlebot/Bingbot/DuckDuckBot
    # and the FB/Twitter link-preview bots are intentionally left free.
    blocked_bots = [
        "AhrefsBot", "SemrushBot", "MJ12bot", "DotBot", "PetalBot", "Bytespider",
        "Amazonbot", "GPTBot", "CCBot", "ClaudeBot", "anthropic-ai",
        "Google-Extended", "meta-externalagent", "ImagesiftBot", "DataForSeoBot",
        "Barkrowler", "SeekportBot", "Diffbot", "magpie-crawler", "Timpibot",
    ]
    lines = []
    for _bot in blocked_bots:
        lines += [f"User-agent: {_bot}", "Disallow: /", ""]
    lines += [
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


# Authorizes Start.io (and the exchanges it resells to) to sell this app's ad
# inventory. Served at https://watch2d.org/app-ads.txt. Update this block
# whenever Start.io revises its entries (Portal → app-ads.txt).
APP_ADS_TXT = """\
start.io, 140648679, DIRECT
pubnative.net, 1007349, RESELLER, d641df8625486a7b
pubnative.net, 1008289, RESELLER, d641df8625486a7b
pubmatic.com, 161162, RESELLER, 5d62403b186f2ace
conversantmedia.com, 100339, RESELLER, 03113cd04947736d
opera.com, pub5925993551616, RESELLER, 55a0c5fd61378de3
rubiconproject.com, 24400, DIRECT, 0bfd66d529a55807
target.my.com, 13033031, RESELLER
acexchange.co.kr, 1746357004, RESELLER
outbrain.com, 0023749a2264ea0429a71b54ac9ca0de9a, RESELLER
appnexus.com, 7597, RESELLER, f5ab79cb980f11d1
zmaticoo.com, 114122, RESELLER
pubmatic.com, 160145, RESELLER, 5d62403b186f2ace
webeyemob.com, 80067, RESELLER
uis.mobfox.com, 2290, RESELLER, 5529a3d1f59865be
outbrain.com, 00dbc7a68d0cd51d55ac0aa9e1918c9a34, RESELLER
pubmatic.com, 163476, RESELLER, 5d62403b186f2ace
gitberry.com, 405100012, RESELLER
rubiconproject.com, 24400, RESELLER, 0bfd66d529a55807
rubiconproject.com, 24600, RESELLER, 0bfd66d529a55807
openx.com, 559912325, RESELLER, 6a698e2ec38604c6
lijit.com, 488437, RESELLER, fafdf38b16bf6b2b
rubiconproject.com, 17960, RESELLER, 0bfd66d529a55807
appnexus.com, 1019, RESELLER, f5ab79cb980f11d1
triplelift.com, 14127, RESELLER, 6c33edb13117fd86
sharethrough.com, 5EQ7ZF1q, RESELLER, d53b998a7bd4ecd2
smaato.com, 1100047713, RESELLER, 07bcf65f187117b4
loopme.com, 11318, RESELLER, 6c8d5f95897a5a3b
smartadserver.com, 4342, RESELLER, 060d053dcf45cbf3
advlion.com, 3144, RESELLER
trustedstack.com, TS677PGY3, RESELLER
themediagrid.com, FWN84J, DIRECT, 9fac4a4a87c2a44f
bidedge.io, 12427296, RESELLER
conversantmedia.com, 100792, RESELLER, 03113cd04947736d
copper6.com, 764121, RESELLER
nativo.com, 5958, RESELLER, 59521ca7cc5e9fee
pubmatic.com, 156500, RESELLER, 5d62403b186f2ace
openx.com, 540709535, RESELLER, 6a698e2ec38604c6
zetaglobal.net, 989, RESELLER
rubiconproject.com, 27052, RESELLER, 0bfd66d529a55807
playdigo.com, 2048, RESELLER, 92011346d63d3c30
rubiconproject.com, 26144, RESELLER, 0bfd66d529a55807
thebrave.io, 1234765, RESELLER, c25b2154543746ac
Media.net, 8CUIV8D19, RESELLER
rubiconproject.com, 19396, RESELLER, 0bfd66d529a55807
pubeasy.io, 110047, RESELLER
trustedstack.com, TS28K5YY0, RESELLER
video.unrulymedia.com, 799061815, RESELLER
lijit.com, 465542, RESELLER, fafdf38b16bf6b2b
kidoz.net, 15568, RESELLER, a109366414b7335e
opera.com, pub12998959884416, RESELLER, 55a0c5fd61378de3
pgamssp.com, 67f939e4ab77600bf50713d6, RESELLER
rubiconproject.com, 24852, RESELLER, 0bfd66d529a55807
adagio.io, 1529, RESELLER
rubiconproject.com, 19116, RESELLER, 0bfd66d529a55807
adelement.com, 48362, RESELLER
smaato.com, 1100059282, RESELLER, 07bcf65f187117b4
triplelift.com, 12158, RESELLER, 6c33edb13117fd86
pubmatic.com, 164125, RESELLER, 5d62403b186f2ace
taboola.com, 1618213, DIRECT, c228e6794e811952
pubnative.net, 1008770, RESELLER, d641df8625486a7b
rubiconproject.com, 17328, RESELLER, 0bfd66d529a55807
apester.com, 91071, DIRECT
triplelift.com, 11457, RESELLER, 6c33edb13117fd86
rubiconproject.com, 27784, RESELLER, 0bfd66d529a55807
axonix.com, 59204, RESELLER
improvedigital.com, 1532, RESELLER
themediagrid.com, JAZ4RI, RESELLER, 35d5010d7789b49d
criteo.com, B-072730, RESELLER, 9fac4a4a87c2a44f
app-stock.com, 509221, RESELLER, ed8c126ea5971415
smaato.com, 1100059563, RESELLER, 07bcf65f187117b4
rubiconproject.com, 20744, RESELLER, 0bfd66d529a55807
videoheroes.tv, 212747, RESELLER, 064bc410192443d8
adgrid.io, 30264, RESELLER
pubmatic.com, 165750, RESELLER, 5d62403b186f2ace
rubiconproject.com, 22544, RESELLER, 0bfd66d529a55807
vidazoo.com, 67aa86ac8effa21af881368d, RESELLER, b6ada874b4d7d0b2
undertone.com, 4261, RESELLER
adyoulike.com, 721f20f70910d379981dc19ec5da709f, RESELLER
video.unrulymedia.com, 2464975885, RESELLER
bidmachine.io, 1447, RESELLER
appnexus.com, 7664, RESELLER
pubmatic.com, 160925, RESELLER, 5d62403b186f2ace
rubiconproject.com, 20736, RESELLER, 0bfd66d529a55807
toponad.com, 168240066616ab, RESELLER, 1d49fe424a1a456d
rubiconproject.com, 28169, RESELLER, 0bfd66d529a55807
twist.win, TW2400538, RESELLER
Media.net, 8CU65V935, RESELLER
themediagrid.com, SJYVMZ, RESELLER, 35d5010d7789b49d
triplelift.com, 9342, RESELLER, 6c33edb13117fd86
pinklion.io, 190976892, DIRECT
bigo.sg, 887, RESELLER
rubiconproject.com, 22134, RESELLER, 0bfd66d529a55807
loopme.com, 11463, RESELLER, 6c8d5f95897a5a3b
showheroes.com, 6833, RESELLER
rubiconproject.com, 26000, RESELLER, 0bfd66d529a55807
mediayo.ai, 2255103, RESELLER
fourthdimentionconsulting.com, 19118819, RESELLER
audioboost.com, ADsBSrsbPdWXF20UZWhN, RESELLER
zetaglobal.net, 808, RESELLER
rubiconproject.com, 25872, RESELLER, 0bfd66d529a55807
screenil.com, 665898, RESELLER
triplelift.com, 8784, RESELLER, 6c33edb13117fd86
indexexchange.com, 215209, RESELLER, 50b1c356f2c5c8fc
growintech.co, 2723d092b63885e0d7c260cc007e8b9d81642, RESELLER, 9d8dfe5c6b00fb37
Media.net, 8CU9B72O6, RESELLER
triplelift.com, 12908, RESELLER, 6c33edb13117fd86
apexflowsdk.com, 1083, RESELLER
rubiconproject.com, 28075, RESELLER, 0bfd66d529a55807
video.unrulymedia.com, 817753694, RESELLER
adwmg.com, 101277, RESELLER, c9688a22012618e7
appnexus.com, 17973, RESELLER, f5ab79cb980f11d1
adform.com, 3386, RESELLER, 9f5210a2f0999e32
mobupps.com, c74d97b01eae257e44aa9d5bade97baf8099, RESELLER
improvedigital.com, 1785, RESELLER
anzu.io, 69a01331e82ac6eac1054bf7, RESELLER
truvid.com, 2643, RESELLER
rubiconproject.com, 17412, RESELLER, 0bfd66d529a55807
appnexus.com, 12700, RESELLER, f5ab79cb980f11d1
media.net, 8CUFTC5O2, RESELLER
appnexus.com, 16525, RESELLER, f5ab79cb980f11d1
blasto.ai, 585, RESELLER, 7e936b1feafdaa61
media.net, 8CUIQQN13, RESELLER
appnexus.com, 16641, RESELLER, f5ab79cb980f11d1
richaudience.com, h44H1yPBlk, RESELLER
rubiconproject.com, 13510, RESELLER
appnexus.com, 8233, RESELLER
adform.com, 1942, RESELLER
lijit.com, 583722, RESELLER, fafdf38b16bf6b2b
rubiconproject.com, 27963, RESELLER, 0bfd66d529a55807
smartadserver.com, 5791, RESELLER, 060d053dcf45cbf3
sharethrough.com, 5791, RESELLER, d53b998a7bd4ecd2
adorphic.com, 4051, RESELLER
triplelift.com, 13567, RESELLER, 6c33edb13117fd86
zetaglobal.net, 748, RESELLER
themediagrid.com, GODNC4, RESELLER, 9fac4a4a87c2a44f
"""


def app_ads_txt(request):
    return HttpResponse(APP_ADS_TXT, content_type="text/plain")


# Web ads.txt — authorizes the ad networks running on watch2d.org so advertisers
# treat the inventory as VERIFIED (higher CPM). Paste the exact lines from your
# Monetag dashboard (Sites → watch2d.org → ads.txt) between the markers below,
# plus any other network you run on the site. One entry per line.
ADS_TXT = """\
# ─── Monetag (paste your exact lines from the Monetag ads.txt panel) ───
# monetag.com, <your-publisher-id>, DIRECT, <cert>
"""


def ads_txt(request):
    return HttpResponse(ADS_TXT, content_type="text/plain")


# ── Adsterra in-app ad pages ──────────────────────────────────────────────────
# Adsterra web tags, served from a real page on watch2d.org so they fill. These
# are the EXACT, unedited tags from the watch2d.org Adsterra site — do NOT
# rebuild them; to change a format, paste the whole snippet Adsterra gives you.
_AD_TAGS = {
    'adsterra_native': (
        '<script async="async" data-cfasync="false" '
        'src="https://probationthimbledespite.com/040828910c3bdfc48913fd8d253a6597/invoke.js"></script>'
        '<div id="container-040828910c3bdfc48913fd8d253a6597"></div>'
    ),
    'adsterra_social': (
        '<script src="https://probationthimbledespite.com/86/57/9f/'
        '86579fa41364fe35a2c4337a24b48205.js"></script>'
    ),
}
# Adsterra Smartlink (direct-link offer, loaded full-screen by the Flutter app):
#   https://probationthimbledespite.com/maydkyrw2?key=46f141d0f51b741caabb347f4ac7e6a0


def ad_tag(request, fmt):
    snippet = _AD_TAGS.get(fmt)
    if not snippet:
        return HttpResponse(status=404)
    # Inject the Adsterra tag VERBATIM — no rebuilding, so it stays valid.
    html = (
        "<!DOCTYPE html><html><head>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}</style>"
        "</head><body>" + snippet + "</body></html>"
    )
    return HttpResponse(html)


def custom_404_view(request, exception):
    context = {
        'categories': get_sidebar_categories(),
    }
    return render(request, 'movies/404.html', context, status=404)


def ping_view(request):
    return JsonResponse({"status": "OK"})


# Telegram start_param constraint: only these characters, 64 bytes max.
_TG_PAYLOAD_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


def telegram_ad_gate(request):
    """
    Minimal Mini App page opened by the delivery bot's 'Continue to Download'
    button (movies.management.commands.run_telegram_bot). Plays the Monetag
    rewarded ad — the same zone the site's own Mini App uses — then hands the
    payload back to the bot via Telegram.WebApp.sendData(), which is the only
    place Monetag's webview-only SDK can run in the bot's flow at all.
    """
    payload = request.GET.get('p', '').strip()
    if not _TG_PAYLOAD_RE.match(payload):
        payload = ''
    return render(request, 'movies/telegram_ad_gate.html', {'payload': payload})


@csrf_exempt
@require_POST
def telegram_ad_gate_deliver(request):
    """
    Called directly by telegram_ad_gate.html once the Monetag ad resolves —
    NOT via the bot's /start flow. Telegram.WebApp.sendData() only works for
    Mini Apps opened via a Keyboard button, not the inline "Continue to
    Download" button we use, so the page talks to the site directly instead,
    authenticated with Telegram's own signed initData rather than a session.
    """
    import json as _json

    from movies.models import DownloadLink
    from movies.telegram_bot_api import (copy_message, send_message,
                                          social_footer, validate_init_data)
    from movies.management.commands._telegram_upload import resolve_direct_link

    try:
        body = _json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'bad_json'}, status=400)

    payload = (body.get('payload') or '').strip()
    chat_id = validate_init_data(body.get('init_data') or '')

    if not chat_id or not _TG_PAYLOAD_RE.match(payload) or not payload.startswith('dl'):
        return JsonResponse({'ok': False, 'error': 'invalid_request'}, status=400)

    try:
        dl = DownloadLink.objects.select_related('movie').get(pk=int(payload[2:]))
    except (DownloadLink.DoesNotExist, ValueError):
        send_message(chat_id, "⚠️ That link isn't available anymore.")
        return JsonResponse({'ok': True})

    file_storage = getattr(settings, 'TELETHON_PRIVATE_CHANNEL', None)
    if dl.telegram_message_id and file_storage:
        if copy_message(chat_id, file_storage, dl.telegram_message_id):
            send_message(chat_id, social_footer())
            return JsonResponse({'ok': True})
        # Message was deleted/inaccessible — fall through to the live link.

    send_message(chat_id, "⏳ Fetching your link…")
    session = requests.Session()
    direct = resolve_direct_link(dl.url, session)
    target = direct or dl.url
    send_message(
        chat_id,
        f"✅ Your download is ready — tap the button below 👇\n\n{social_footer()}",
        reply_markup={'inline_keyboard': [[
            {'text': '▶️ CLICK TO DOWNLOAD', 'url': target},
        ]]},
    )
    return JsonResponse({'ok': True})


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


# @require_GET
# def resolve_download_link(request):
#     landing_url = request.GET.get('url', '').strip()
#     debug = request.GET.get('debug') == '1' and request.user.is_staff

#     if not landing_url:
#         return JsonResponse({'error': 'No URL provided'}, status=400)

#     from urllib.parse import urlparse, urlunparse
#     parsed = urlparse(landing_url)
#     host = parsed.netloc.lower()
#     lower = landing_url.lower()

#     if 'sabishares.com' in host and 'preview' in parsed.query:
#         direct = urlunparse(parsed._replace(query='', fragment=''))
#         if debug:
#             return JsonResponse({'method': 'sabishares_preview', 'download_url': direct})
#         return JsonResponse({'download_url': direct})

#     direct_exts = ('.mp4', '.mkv', '.webm', '.avi', '.mov', '.zip', '.rar')
#     if '?pt=' in lower or any(lower.endswith(ext) for ext in direct_exts):
#         return JsonResponse({'download_url': landing_url})

#     if 'mylulutv.com' in host:
#         return JsonResponse({'download_url': landing_url})

#     if 'downloadwella.com' in host:
#         result, dbg = _resolve_downloadwella(landing_url, parsed, debug)
#         if result:
#             if debug:
#                 return JsonResponse({'method': 'downloadwella_post', 'download_url': result, 'debug': dbg})
#             return JsonResponse({'download_url': result})
#         if debug:
#             return JsonResponse({'method': 'downloadwella_failed', 'fallback': landing_url, 'debug': dbg})
#         return JsonResponse({'download_url': landing_url})

#     html, fetch_err = _fetch_html_safe(landing_url)
#     if not html:
#         if debug:
#             return JsonResponse({'method': 'fetch_failed', 'error': fetch_err, 'fallback': landing_url})
#         return JsonResponse({'download_url': landing_url})

#     download_url = _extract_download_url(html, host)
#     if download_url:
#         if debug:
#             return JsonResponse({'method': 'html_extract', 'download_url': download_url, 'html_length': len(html)})
#         return JsonResponse({'download_url': download_url})

#     if debug:
#         return JsonResponse({
#             'method': 'extract_failed',
#             'fallback': landing_url,
#             'html_length': len(html),
#             'cloudflare_block': 'cf-browser-verification' in html or 'Checking your browser' in html,
#             'has_pt_token': '?pt=' in html,
#             'has_kissorgrab': 'kissorgrab' in html,
#             'html_snippet': html[:3000],
#         })
#     return JsonResponse({'download_url': landing_url})

@require_GET
def resolve_download_link(request):
    landing_url = request.GET.get('url', '').strip()
    debug = request.GET.get('debug') == '1' and request.user.is_staff
 
    if not landing_url:
        return JsonResponse({'error': 'No URL provided'}, status=400)
 
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(landing_url)
    host   = parsed.netloc.lower()
    lower  = landing_url.lower()
 
    # ── sabishares: strip preview query ───────────────────────
    if 'sabishares.com' in host and 'preview' in parsed.query:
        direct = urlunparse(parsed._replace(query='', fragment=''))
        if debug:
            return JsonResponse({'method': 'sabishares_preview', 'download_url': direct})
        return JsonResponse({'download_url': direct})
 
    # ── already a direct link — return as-is ──────────────────
    #   Skip this shortcut for hosts whose file PAGES end in .mkv/.html;
    #   those are handled by their own scrapers below.
    direct_exts = ('.mp4', '.mkv', '.webm', '.avi', '.mov', '.zip', '.rar')
    # 'loadedfiles.' (no TLD) so both loadedfiles.org and the newer
    # loadedfiles.net are treated as gate pages, not direct files.
    gate_hosts  = ('loadedfiles.', 'downloadwella.com')
    if not any(g in host for g in gate_hosts) and (
            '?pt=' in lower or any(lower.endswith(ext) for ext in direct_exts)):
        return JsonResponse({'download_url': landing_url})
 
    # ── passthrough hosts ──────────────────────────────────────
    if 'mylulutv.com' in host:
        return JsonResponse({'download_url': landing_url})
 
    # ── downloadwella ──────────────────────────────────────────
    if 'downloadwella.com' in host:
        result, dbg = _resolve_downloadwella(landing_url, parsed, debug)
        if result:
            if debug:
                return JsonResponse({'method': 'downloadwella_post', 'download_url': result, 'debug': dbg})
            return JsonResponse({'download_url': result})
        if debug:
            return JsonResponse({'method': 'downloadwella_failed', 'fallback': landing_url, 'debug': dbg})
        return JsonResponse({'download_url': landing_url})
 
    # ── loadedfiles.org / loadedfiles.net ─────────────────────
    if 'loadedfiles.' in host:
        result, dbg = _resolve_loadedfiles(landing_url, parsed, debug)
        if result:
            if debug:
                return JsonResponse({'method': 'loadedfiles_resolved', 'download_url': result, 'debug': dbg})
            return JsonResponse({'download_url': result})
        # Couldn't resolve — fall back to the landing page itself
        if debug:
            return JsonResponse({'method': 'loadedfiles_failed', 'fallback': landing_url, 'debug': dbg})
        return JsonResponse({'download_url': landing_url})
 
    # ── generic HTML fetch + extract ──────────────────────────
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
            'method':             'extract_failed',
            'fallback':           landing_url,
            'html_length':        len(html),
            'cloudflare_block':   'cf-browser-verification' in html or 'Checking your browser' in html,
            'has_pt_token':       '?pt=' in html,
            'has_kissorgrab':     'kissorgrab' in html,
            'html_snippet':       html[:3000],
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

 
def _resolve_loadedfiles(landing_url, parsed, debug=False):
    """
    Resolve a loadedfiles.org file page to its real ?pt= download URL.
 
    Why two steps:
      loadedfiles.org checks BOTH Referer AND a session cookie.
      Step 1 — GET the homepage to receive a valid session cookie.
      Step 2 — GET the file page within that same session, sending
               the 9jarocks Referer.  The server now sees a real
               browser-like session and returns the page with the
               `var downloadUrl = '...?pt=...'` JS variable in it.
    """
    import requests as _requests
 
    dbg = {}
    HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
 
    try:
        session = _requests.Session()
        session.headers.update(HEADERS)
 
        # ── Step 1: warm up a session cookie from the homepage ────────────
        home_url = f"{parsed.scheme}://{parsed.netloc}/"
        try:
            warm = session.get(home_url, timeout=10, allow_redirects=True)
            dbg['warm_status'] = warm.status_code
            dbg['warm_cookies'] = list(session.cookies.keys())
        except Exception as e:
            dbg['warm_error'] = str(e)
            # Non-fatal — try the file page anyway
 
        # ── Step 2: follow the token chain to the final CDN file ──────────
        #   file page → ?pt=A → ?pt=B → 302 redirect OFF loadedfiles to the CDN.
        #   A redirect off loadedfiles.org IS the real direct download link, so
        #   the browser downloads it immediately (the 20s countdown is cosmetic).
        from urllib.parse import urljoin, urlparse as _urlparse
        import json as _json

        next_patterns = (
            r"var\s+downloadUrl\s*=\s*['\"](https?://[^'\"]+)['\"]",
            r"window\.location(?:\.href)?\s*=\s*['\"](https?://[^'\"]+\?pt=[^'\"]+)['\"]",
            r"['\"](https?://loadedfiles\.[a-z]{2,}/[^'\"]+\?pt=[^'\"]+)['\"]",
            # 2026 redesign: an Alpine.js widget (`x-data="dlTimer({ link: '...' })"`)
            # replaced the old countdown script. The link is JSON/unicode-escaped
            # inside the HTML attribute (e.g. : for ':'), not a plain URL.
            r"dlTimer\(\{[^}]*?link:\s*'([^']+)'",
        )
        referer = 'https://www.my9jarocks.bz/'
        current = landing_url
        last_pt = None
        saw_redirect_hop = False

        for hop in range(3):
            resp = session.get(current, timeout=15, allow_redirects=False,
                               headers={'Referer': referer})
            code = resp.status_code
            dbg['hop_%d' % hop] = code

            # A redirect off loadedfiles.org is the real CDN file link.
            if code in (301, 302, 303, 307, 308):
                loc = resp.headers.get('Location', '')
                if not loc:
                    break
                target = urljoin(current, loc)
                if 'loadedfiles.' not in _urlparse(target).netloc.lower():
                    dbg['pattern'] = 'cdn_redirect'
                    return target, dbg          # ← direct CDN download URL
                saw_redirect_hop = True
                referer, current = current, target
                continue

            if code != 200:
                dbg['error'] = 'HTTP %d' % code
                break

            html = resp.text
            nxt = None
            for pat in next_patterns:
                m = re.search(pat, html, re.IGNORECASE)
                if m:
                    nxt = m.group(1).strip()
                    break
            if not nxt or nxt == current:
                break
            # The dlTimer pattern's capture is JSON/unicode-escaped
            # (\uXXXX, \/) — decode it the same way a JS engine would.
            # No-op for the other patterns, which never contain a backslash.
            if '\\' in nxt:
                try:
                    nxt = _json.loads('"' + nxt + '"')
                except ValueError:
                    pass
            if '?pt=' in nxt:
                last_pt = nxt
            referer, current = current, nxt

        # Fallback: deepest ?pt= link (old behaviour) — only trust it if we
        # actually saw at least one real redirect hop (proof the token chain
        # is converging toward a CDN file). Some loadedfiles.net requests now
        # just regenerate a brand-new token on every fetch forever without
        # ever redirecting — returning one of those as a "resolved" link
        # would just hand the user another dead countdown page instead of
        # falling back to the real landing page where their own browser can
        # complete the JS challenge properly.
        if last_pt and saw_redirect_hop:
            dbg['pattern'] = 'pt_fallback'
            return last_pt, dbg
        dbg.setdefault('error', 'no_link_found')
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
            .only('id', 'title', 'slug', 'image_url', 'created_at', 'title_b', 'vi_year', 'rating', 'trailer_url')
            .prefetch_related('external_ratings')
            .filter(
                Q(is_series=False),
                Q(title_b__isnull=True) | Q(title_b=''),
            )
            .order_by('-created_at')
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # ── Cached heavy queries ──────────────────────────────────────────────
        cached = cache.get('movies_home_ctx_v2')
        if cached is None:
            cached = _build_movies_home_context()
            cache.set('movies_home_ctx_v2', cached, MOVIES_HOME_CACHE_TTL)
        context.update(cached)

        # ── Series sections — cached separately (not user-specific) ──────────
        ongoing_cached = cache.get('home_ongoing_series_v2')
        if ongoing_cached is None:
            ongoing_cached = list(
                Movie.objects
                .only('id', 'title', 'slug', 'title_b', 'image_url',
                      'title_b_updated_at', 'created_at', 'rating', 'trailer_url')
                .prefetch_related('external_ratings')
                .filter(
                    Q(is_series=True) | (Q(title_b__isnull=False) & ~Q(title_b='')),
                    completed=False,
                )
                .order_by('-title_b_updated_at', '-created_at')[:27]  # 3 pages × 9
            )
            cache.set('home_ongoing_series_v2', ongoing_cached, MOVIES_HOME_CACHE_TTL)

        comp_cached = cache.get('home_completed_series_v2')
        if comp_cached is None:
            comp_cached = list(
                Movie.objects
                .only('id', 'title', 'slug', 'title_b', 'image_url',
                      'title_b_updated_at', 'created_at', 'rating', 'trailer_url')
                .prefetch_related('external_ratings')
                .filter(
                    Q(is_series=True) | (Q(title_b__isnull=False) & ~Q(title_b='')),
                    completed=True,
                )
                .order_by('-title_b_updated_at', '-created_at')[:27]
            )
            cache.set('home_completed_series_v2', comp_cached, MOVIES_HOME_CACHE_TTL)

        context['ongoing_series'] = Paginator(ongoing_cached, 9).get_page(
            self.request.GET.get('ongoing_page', 1)
        )
        context['completed_series'] = Paginator(comp_cached, 9).get_page(
            self.request.GET.get('completed_page', 1)
        )

        # ── Coming Soon teaser (cached) — first few un-released TMDB titles ───
        upcoming_cached = cache.get('home_upcoming_v2')
        if upcoming_cached is None:
            from django.utils import timezone
            today = timezone.now().date().isoformat()
            upcoming_cached = list(
                UpcomingTitle.objects
                .filter(release_date__gte=today)
                .order_by('release_date')[:12]
            )
            cache.set('home_upcoming_v2', upcoming_cached, MOVIES_HOME_CACHE_TTL)
        context['upcoming'] = upcoming_cached

        if self.request.user.is_authenticated:
            context['watchlisted_ids'] = set(
                self.request.user.watchlist_movies.values_list('id', flat=True))
            context['notify_requested_ids'] = set(
                NotifyRequest.objects.filter(user=self.request.user)
                .values_list('tmdb_id', flat=True))
        else:
            context['watchlisted_ids'] = set()
            context['notify_requested_ids'] = set()

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
        # get() already resolved self.category — reuse it instead of a 2nd query.
        category = getattr(self, 'category', None)
        if category is None:
            category = get_object_or_404(Category, id=self.kwargs['cat_id'])
        self.category = category
        cache_key = f'cat_movies_{category.pk}_v2'
        qs = cache.get(cache_key)
        if qs is None:
            qs = list(
                Movie.objects
                .only('id', 'title', 'slug', 'image_url', 'created_at', 'description', 'vi_year', 'rating', 'trailer_url')
                .prefetch_related('external_ratings')
                .filter(categories=self.category)
                .order_by('-created_at')
            )
            cache.set(cache_key, qs, CATEGORY_PAGE_CACHE_TTL)
        self.query = self.request.GET.get('q', '').strip()
        if self.query:
            q_lower = self.query.lower()
            qs = [m for m in qs if q_lower in m.title.lower()]
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['category'] = self.category
        context['categories'] = get_sidebar_categories()
        context['query'] = self.query
        context['other_categories'] = [
            c for c in context['categories'] if c.id != self.category.id
        ]
        if self.request.user.is_authenticated:
            context['watchlisted_ids'] = set(
                self.request.user.watchlist_movies.values_list('id', flat=True))
        else:
            context['watchlisted_ids'] = set()
        return context


# ══════════════════════════════════════════════════════════════════════════════
# A–Z BROWSE + GENRE HUB  —  long-tail SEO. An alphabetical index and one page
# per letter make every title reachable through crawlable hub pages (mirrors the
# structure competitor download sites use to rank on long-tail queries).
# ══════════════════════════════════════════════════════════════════════════════
AZ_LETTERS = list('ABCDEFGHIJKLMNOPQRSTUVWXYZ') + ['0-9']


AZ_INDEX_CACHE_TTL = 60 * 60 * 6  # 6 hours — counts change slowly, page is low-traffic


class AZIndexView(TemplateView):
    """/a-z/ hub — links out to every letter page."""
    template_name = 'movies/az_index.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        letter_counts = cache.get('az_index_counts_v1')
        if letter_counts is None:
            letter_counts = {}
            for letter in AZ_LETTERS:
                if letter == '0-9':
                    letter_counts[letter] = Movie.objects.exclude(
                        title__iregex=r'^[A-Za-z]').count()
                else:
                    letter_counts[letter] = Movie.objects.filter(
                        title__istartswith=letter).count()
            cache.set('az_index_counts_v1', letter_counts, AZ_INDEX_CACHE_TTL)

        ctx['letters'] = [
            {'letter': L, 'count': letter_counts.get(L, 0)} for L in AZ_LETTERS
        ]
        ctx['total_titles'] = sum(letter_counts.values())
        ctx['trending_movies_list'] = get_trending_list(6, is_series=False)
        ctx['trending_series_list'] = get_trending_list(6, is_series=True)
        ctx['browse_categories'] = get_sidebar_categories()
        return ctx   # nav `categories` comes from the context processor (full list)


class AZLetterView(ListView):
    """/a-z/<letter>/ — every title starting with that letter, paginated."""
    template_name = 'movies/az_letter.html'
    context_object_name = 'movies'
    paginate_by = 48

    def get(self, request, *args, **kwargs):
        self.letter = kwargs['letter'].upper()
        if self.letter not in AZ_LETTERS:
            return redirect('movies:az_index', permanent=True)
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        base = Movie.objects.only(
            'id', 'title', 'slug', 'image_url', 'rating', 'trailer_url'
        ).prefetch_related('external_ratings')
        if self.letter == '0-9':
            qs = base.exclude(title__iregex=r'^[A-Za-z]')
        else:
            qs = base.filter(title__istartswith=self.letter)
        self.query = self.request.GET.get('q', '').strip()
        if self.query:
            qs = qs.filter(title__icontains=self.query)
        return qs.order_by('title')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['letter'] = self.letter
        ctx['letters'] = AZ_LETTERS
        ctx['query'] = self.query
        if self.request.user.is_authenticated:
            ctx['watchlisted_ids'] = set(
                self.request.user.watchlist_movies.values_list('id', flat=True))
        else:
            ctx['watchlisted_ids'] = set()
        return ctx


class GenresIndexView(TemplateView):
    """/genres/ — a crawlable hub linking to EVERY category (tag) page."""
    template_name = 'movies/genres_index.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # ALL categories — not the 3-item sidebar set. This is the full genre hub.
        # Adult/18+ hidden from public browse (ad-network + SEO safety).
        # `latest_movies` (top 5 by views) feeds category_tiles.html's poster
        # crossfade — one extra query total via Prefetch, not one per category.
        ctx['all_categories'] = (
            Category.objects.exclude(name__icontains='18+')
            .prefetch_related(
                Prefetch(
                    'movies',
                    queryset=Movie.objects.only('id', 'image_url').order_by('-views')[:5],
                    to_attr='latest_movies'
                )
            )
            .order_by('name')
        )
        return ctx


def old_movie_redirect(request, pk):
    movie = get_object_or_404(Movie, pk=pk)
    return redirect(movie.get_absolute_url(), permanent=True)


def old_category_redirect(request, cat_id):
    category = get_object_or_404(Category, pk=cat_id)
    return redirect(category.get_absolute_url(), permanent=True)


def _build_movie_seo_paragraph(movie, seo_type, completion_label, is_series):
    """
    Per-movie description paragraph shown on the download page.

    Rotates between 4 differently-structured templates (keyed off movie.pk,
    so a given title always gets the same one) instead of a single fill-in-
    the-blank sentence repeated across ~25k pages. Google's "scaled content
    abuse" detection targets exactly that pattern — identical sentence
    skeletons with only nouns swapped are read as auto-generated regardless
    of how unique the underlying facts are.
    """
    from django.template.defaultfilters import truncatewords

    title    = movie.title
    # Scraped titles very often already bake the year in ("Buddy (2026)") —
    # ~68% of titles with vi_year set already contain it, so appending it
    # again produced "Buddy (2026) (2026)" on roughly 13,000 pages.
    year     = (f" ({movie.vi_year})"
                if movie.vi_year and movie.vi_year not in title else "")
    genre    = movie.vi_genre or ""
    country  = movie.vi_country or ""
    language = movie.vi_language or ""
    subtitle = movie.vi_subtitle or ""
    filesize = movie.vi_filesize or ""
    runtime  = movie.vi_runtime or ""
    episodes = (movie.vi_episodes or "") if is_series else ""
    cast     = truncatewords(movie.vi_cast, 8) if movie.vi_cast else ""
    type_lc  = seo_type.lower()

    variant = movie.pk % 4

    if variant == 0:
        parts = [
            f"{title}{year} is a{f' {genre}' if genre else ''} {type_lc}"
            f"{f' from {country}' if country else ''}{f', in {language}' if language else ''}"
            f"{f' with {subtitle} subtitles' if subtitle else ''}.",
            f"Download {title} free in HD — available in 480p, 720p and 1080p MP4 & MKV"
            f"{f' (file size {filesize})' if filesize else ''}{f', runtime {runtime}' if runtime else ''}"
            f"{f', {episodes} episodes' if episodes else ''}.",
        ]
        if cast:
            parts.append(f"Starring {cast}.")
        parts.append(f"Tap a download link below to get {title} on Watch2D.")

    elif variant == 1:
        parts = [
            f"Looking to download {title}{year}? This{f' {country}' if country else ''} {type_lc}"
            f"{f' ({genre})' if genre else ''} is ready in HD on Watch2D.",
            f"Choose 480p, 720p or 1080p in MP4 or MKV"
            f"{f' (file size {filesize})' if filesize else ''}{f' — runtime {runtime}' if runtime else ''}.",
        ]
        audio_bits = []
        if language:
            audio_bits.append(f"{language} audio")
        if subtitle:
            audio_bits.append(f"{subtitle} subtitles")
        if audio_bits:
            parts.append(f"Comes with {' and '.join(audio_bits)}.")
        if cast:
            parts.append(f"Starring {cast}.")
        if episodes:
            parts.append(f"{episodes} episodes ready to download.")
        parts.append("Scroll down for the direct links.")

    elif variant == 2:
        parts = [
            f"{title}{year} is available to download on Watch2D in 480p, 720p and 1080p (MP4/MKV)"
            f"{f', file size {filesize}' if filesize else ''}.",
        ]
        descriptor = f"{genre + ' ' if genre else ''}{type_lc}"
        tail_bits = []
        if country:
            tail_bits.append(f"from {country}")
        if language:
            tail_bits.append(f"in {language}")
        if subtitle:
            tail_bits.append(f"with {subtitle} subs")
        if runtime:
            tail_bits.append(f"runtime {runtime}")
        parts.append(f"It's a {descriptor}{(' ' + ', '.join(tail_bits)) if tail_bits else ''}.")
        if cast:
            parts.append(f"Cast includes {cast}.")
        if episodes:
            parts.append(f"{episodes} episodes included.")

    else:
        parts = [
            f"{title}{year}: {genre + ' ' if genre else ''}{type_lc}{f' from {country}' if country else ''}.",
            f"Free HD download — 480p, 720p, 1080p, MP4/MKV{f' ({filesize})' if filesize else ''}.",
        ]
        if cast:
            parts.append(f"Featuring {cast}.")
        if runtime:
            parts.append(f"Runtime: {runtime}.")
        if episodes:
            parts.append(f"{episodes} episodes.")
        parts.append(f"Get {title} on Watch2D below.")

    return " ".join(parts)


class MovieDetailView(DetailView):
    model = Movie
    template_name = 'movies/movie_detail.html'

    def get_queryset(self):
        return Movie.objects.prefetch_related(
            'liked_by', 'watchlisted_by', 'watched_by', 'verified_watched_by',
            'categories', 'comments__user'
        )

    def get_object(self, queryset=None):
        if queryset is None:
            queryset = self.get_queryset()
        obj = queryset.filter(pk=self.kwargs['pk']).first()
        if obj is None:
            # The ID may come from a divergent DB (e.g. a Telegram post generated
            # by a scraper writing to a different DB). Recover the movie by SLUG so
            # the post redirects to the real page instead of 404ing. Try the exact
            # slug, then the slug with any trailing "-<n>" dedup suffix stripped.
            slug = (self.kwargs.get('slug') or '').strip('/')
            obj = Movie.objects.filter(slug=slug).first()
            if obj is None and slug:
                base = _re.sub(r'-\d+$', '', slug)
                if base and base != slug:
                    obj = (Movie.objects.filter(slug=base).first()
                           or Movie.objects.filter(slug__startswith=base + '-').first())
            if obj is None:
                raise Http404('No movie matches the given query.')
            # get() will redirect to obj.get_absolute_url() since the slug differs.
        Movie.objects.filter(pk=obj.pk).update(
            views=F('views') + 1, weekly_views=F('weekly_views') + 1)
        obj.views = (obj.views or 0) + 1   # reflect the bump without an extra round-trip
        obj.weekly_views = (obj.weekly_views or 0) + 1
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
        # Don't duplicate — the scraped title often already includes the status
        # (e.g. "Run Away S01 (Complete)"), which produced "(Complete) (Complete)".
        if completion_label and completion_label.strip('()').lower() in movie.title.lower():
            completion_label = ''

        context['seo_type'] = seo_type
        context['is_series'] = is_series
        context['completion_label'] = completion_label
        context['seo_paragraph'] = _build_movie_seo_paragraph(
            movie, seo_type, completion_label, is_series)
        # Breadcrumb nav + BreadcrumbList schema — first of the already-
        # prefetched categories, so no extra query.
        context['primary_category'] = movie_categories[0] if movie_categories else None

        # ── Like / watchlist / watched — use prefetched sets, no extra queries ─
        verified_watched_ids = {u.pk for u in movie.verified_watched_by.all()}
        context['verified_watched_ids'] = verified_watched_ids
        if user.is_authenticated:
            liked_ids      = {u.pk for u in movie.liked_by.all()}
            watchlisted_ids = {u.pk for u in movie.watchlisted_by.all()}
            watched_ids    = {u.pk for u in movie.watched_by.all()}
            context['is_liked']       = user.pk in liked_ids
            context['is_watchlisted'] = user.pk in watchlisted_ids
            context['is_watched']     = user.pk in watched_ids
        else:
            context['is_liked']       = False
            context['is_watchlisted'] = False
            context['is_watched']     = False

        # ── Comments (prefetched in get_queryset) ─────────────────────────────
        context['comments'] = movie.comments.filter(
            parent__isnull=True
        ).select_related('user').prefetch_related(
            'replies__user', 'reactions', 'replies__reactions'
        ).order_by('-created_at')
        # movie.comments.all() is already prefetched in get_queryset() — counting
        # the in-memory list (not .count()) avoids a second query.
        context['total_comment_count'] = len(movie.comments.all())

        context['comment_form'] = CommentForm()

        # ── Reviews & ratings ──────────────────────────────────────────────────
        context['external_ratings'] = {
            r.source: r for r in movie.external_ratings.all()
        }
        agg_key = f'movie_review_agg_{movie.id}_v1'
        agg = cache.get(agg_key)
        if agg is None:
            agg = Review.objects.filter(movie=movie).aggregate(
                avg=Avg('rating'), count=Count('id'))
            cache.set(agg_key, agg, 60 * 15)
        context['community_score'] = round(agg['avg'] / 2, 1) if agg['avg'] else None
        context['community_review_count'] = agg['count']

        critic_scores = [r.score for r in context['external_ratings'].values()]
        context['critic_score'] = round(sum(critic_scores) / len(critic_scores), 1) if critic_scores else None

        # ── Trust badges — cheap, purely presentational, computed from data
        # already gathered above. Require >=2 independent sources / reviews so
        # a single generous score can't "certify" a title on its own. ─────────
        context['is_certified'] = (
            context['critic_score'] is not None and context['critic_score'] >= 7.0
            and len(critic_scores) >= 2
        )
        context['is_fan_favorite'] = (
            context['community_score'] is not None and context['community_score'] >= 4.0
            and agg['count'] >= 10
        )

        if user.is_authenticated:
            context['user_review'] = Review.objects.filter(movie=movie, user=user).first()
        else:
            context['user_review'] = None
        context['review_form'] = ReviewForm(instance=context['user_review'])
        context['reviews'] = (
            Review.objects.filter(movie=movie)
            .exclude(user=user if user.is_authenticated else None)
            .select_related('user')
            .order_by('-created_at')[:20]
        )

        # ── Related movies — by category, deterministic order (NO order_by('?'))
        # order_by('?') = ORDER BY RANDOM() = full table scan every request.
        # Use pk descending (fast index scan) filtered by same category instead.
        # Cached per-movie (30 min) — this m2m join+distinct is the page's heaviest
        # query and its result changes rarely, so skip the round-trip on repeats.
        rel_key = f'movie_related_{movie.id}_v2'
        related_movies = cache.get(rel_key)
        if related_movies is None:
            if movie_categories:
                related_movies = list(
                    Movie.objects
                    .only('id', 'title', 'slug', 'image_url', 'created_at', 'rating', 'trailer_url')
                    .prefetch_related('external_ratings')
                    .filter(categories__in=movie_categories)
                    .exclude(id=movie.id)
                    .distinct()
                    .order_by('-created_at')[:12]
                )
            else:
                related_movies = list(
                    Movie.objects
                    .only('id', 'title', 'slug', 'image_url', 'created_at', 'rating', 'trailer_url')
                    .prefetch_related('external_ratings')
                    .exclude(id=movie.id)
                    .order_by('-created_at')[:12]
                )
            cache.set(rel_key, related_movies, 60 * 30)

        context['related_movies'] = related_movies
        context['trending_movies_list'] = get_trending_list(6, is_series=False)
        context['trending_series_list'] = get_trending_list(6, is_series=True)
        if user.is_authenticated:
            context['watchlisted_ids'] = set(
                user.watchlist_movies.values_list('id', flat=True))
        else:
            context['watchlisted_ids'] = set()

        # ── Cast (TMDB-enriched). Top-billed first; capped for the row. ───────
        # Cached per-movie (6h) — cast essentially never changes after enrichment.
        cast_key = f'movie_cast_{movie.id}_v1'
        cast = cache.get(cast_key)
        if cast is None:
            cast = list(
                MovieCast.objects
                .filter(movie=movie)
                .select_related('person')
                .order_by('order')[:18]
            )
            cache.set(cast_key, cast, 60 * 60 * 6)
        context['cast'] = cast

        # ── Crew (Director / Writers / Producers tabs) ─────────────────────────
        crew_key = f'movie_crew_{movie.id}_v1'
        crew = cache.get(crew_key)
        if crew is None:
            crew = list(
                MovieCrew.objects
                .filter(movie=movie)
                .select_related('person')
                .order_by('order')
            )
            cache.set(crew_key, crew, 60 * 60 * 6)
        context['directors'] = [c for c in crew if c.department == 'directing']
        context['writers']   = [c for c in crew if c.department == 'writing']
        context['producers'] = [c for c in crew if c.department == 'production']
        context['director_names'] = ', '.join(c.person.name for c in context['directors'])

        # ── Rating distribution histogram (1..10 half-star buckets) ───────────
        hist_key = f'movie_rating_hist_{movie.id}_v1'
        histogram = cache.get(hist_key)
        if histogram is None:
            counts_qs = (
                Review.objects.filter(movie=movie)
                .values('rating')
                .annotate(n=Count('id'))
            )
            counts = {row['rating']: row['n'] for row in counts_qs}
            max_count = max(counts.values()) if counts else 0
            histogram = [
                {
                    'rating': r,
                    'count': counts.get(r, 0),
                    'pct': round((counts.get(r, 0) / max_count) * 100) if max_count else 0,
                }
                for r in range(1, 11)
            ]
            cache.set(hist_key, histogram, 60 * 15)
        context['rating_histogram'] = histogram

        context['categories']     = get_sidebar_categories()
        context['full_image_url'] = request.build_absolute_uri(movie.image_url)
        context['full_video_url'] = request.build_absolute_uri(movie.video_url)
        context['logo_url']       = request.build_absolute_uri(static('img/logo.png'))

        return context


@login_required
def toggle_like(request, pk):
    movie = get_object_or_404(Movie, pk=pk)
    user = request.user
    if movie.liked_by.filter(pk=user.pk).exists():
        movie.liked_by.remove(user)
        is_liked = False
    else:
        movie.liked_by.add(user)
        is_liked = True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'is_liked': is_liked})
    return redirect(movie.get_absolute_url())


@login_required
def toggle_watchlist(request, pk):
    movie = get_object_or_404(Movie, pk=pk)
    user = request.user
    if movie.watchlisted_by.filter(pk=user.pk).exists():
        movie.watchlisted_by.remove(user)
        is_watchlisted = False
    else:
        movie.watchlisted_by.add(user)
        is_watchlisted = True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'is_watchlisted': is_watchlisted})
    return redirect(movie.get_absolute_url())


@login_required
def toggle_watched(request, pk):
    """Self-reported diary log — 'I've seen this', independent of rating."""
    movie = get_object_or_404(Movie, pk=pk)
    user = request.user
    if movie.watched_by.filter(pk=user.pk).exists():
        movie.watched_by.remove(user)
        is_watched = False
    else:
        movie.watched_by.add(user)
        is_watched = True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'is_watched': is_watched})
    return redirect(movie.get_absolute_url())


@login_required
def toggle_notify(request, pk):
    """'Notify Me' on an upcoming (not-yet-released) title. Stored as a
    NotifyRequest keyed by tmdb_id (see model docstring for why it isn't an
    M2M on UpcomingTitle directly)."""
    upcoming = get_object_or_404(UpcomingTitle, pk=pk)
    user = request.user
    existing = NotifyRequest.objects.filter(user=user, tmdb_id=upcoming.tmdb_id).first()
    if existing:
        existing.delete()
        is_notifying = False
    else:
        NotifyRequest.objects.create(
            user=user, tmdb_id=upcoming.tmdb_id,
            title=upcoming.title, media_type=upcoming.media_type,
        )
        is_notifying = True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'is_notifying': is_notifying})
    return redirect('movies:coming_soon')


def movie_quick_actions(request, pk):
    """Small HTML fragment (poster, Watch/Download, Watchlist/Like/Watched
    toggles) for the site-wide 'quick actions' popup — so a Watchlist/Like/
    Watched tap works straight from any grid/carousel, no need to open the
    full movie page first. Buttons reuse the same .js-watchlist-btn/.js-like-
    btn/.js-watched-btn delegated handlers already wired in base.html."""
    movie = get_object_or_404(Movie, pk=pk)
    user = request.user
    if user.is_authenticated:
        is_liked = movie.liked_by.filter(pk=user.pk).exists()
        is_watchlisted = movie.watchlisted_by.filter(pk=user.pk).exists()
        is_watched = movie.watched_by.filter(pk=user.pk).exists()
    else:
        is_liked = is_watchlisted = is_watched = False
    html = render_to_string('movies/components/quick_actions_content.html', {
        'movie': movie, 'user': user,
        'is_liked': is_liked, 'is_watchlisted': is_watchlisted, 'is_watched': is_watched,
    }, request=request)
    return HttpResponse(html)


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

        search_cache_key = f'search_v2_{hash(query.lower())}'
        cached_results = cache.get(search_cache_key)
        if cached_results is not None:
            return cached_results

        base_qs = Movie.objects.only(
            'id', 'title', 'slug', 'description', 'image_url', 'created_at', 'rating', 'trailer_url'
        ).prefetch_related('external_ratings')

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
        if self.request.user.is_authenticated:
            context['watchlisted_ids'] = set(
                self.request.user.watchlist_movies.values_list('id', flat=True))
        else:
            context['watchlisted_ids'] = set()
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


@login_required
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
    comment.user = request.user
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


@login_required
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
    reply.user = request.user
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


@login_required
@require_POST
def react_to_comment(request, pk):
    """Toggle an emoji reaction on a comment/reply. Same emoji again removes
    it; a different emoji swaps it (one reaction per user per message)."""
    comment = get_object_or_404(Comment, pk=pk)
    emoji = request.POST.get('emoji', '').strip()
    valid_emojis = {choice[0] for choice in CommentReaction.REACTION_CHOICES}
    if emoji not in valid_emojis:
        return JsonResponse({'success': False, 'message': 'Invalid reaction'})

    existing = CommentReaction.objects.filter(comment=comment, user=request.user).first()
    if existing and existing.emoji == emoji:
        existing.delete()
        user_reaction = None
    elif existing:
        existing.emoji = emoji
        existing.save(update_fields=['emoji'])
        user_reaction = emoji
    else:
        CommentReaction.objects.create(comment=comment, user=request.user, emoji=emoji)
        user_reaction = emoji

    counts = dict(
        CommentReaction.objects.filter(comment=comment)
        .values_list('emoji').annotate(n=Count('id')).order_by()
    )
    return JsonResponse({'success': True, 'counts': counts, 'user_reaction': user_reaction})


@login_required
@require_POST
def add_review(request, pk):
    movie = get_object_or_404(Movie, pk=pk)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    try:
        rating = int(request.POST.get('rating', ''))
        assert 1 <= rating <= 10
    except (TypeError, ValueError, AssertionError):
        if is_ajax:
            return JsonResponse({'success': False, 'message': 'Please pick a star rating'})
        messages.error(request, 'Please pick a star rating')
        return redirect(movie.get_absolute_url())

    content = request.POST.get('content', '').strip()
    review, _created = Review.objects.update_or_create(
        movie=movie, user=request.user,
        defaults={'rating': rating, 'content': content},
    )
    movie.watched_by.add(request.user)  # rating it implies you've seen it
    cache.delete(f'movie_review_agg_{movie.id}_v1')

    if is_ajax:
        html = render_to_string('movies/components/review_item.html', {
            'review': review, 'movie': movie, 'user': request.user,
            'verified_watched_ids': {u.pk for u in movie.verified_watched_by.all()},
        })
        return JsonResponse({
            'success': True,
            'message': 'Review posted!',
            'html': html,
            'review_id': review.id,
        })

    messages.success(request, 'Review posted!')
    return redirect(movie.get_absolute_url() + '#reviews-section')


@login_required
@require_POST
def delete_review(request, pk):
    review = get_object_or_404(Review, pk=pk)
    movie = review.movie
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if request.user == review.user or request.user.is_staff:
        review.delete()
        cache.delete(f'movie_review_agg_{movie.id}_v1')
        if is_ajax:
            return JsonResponse({'success': True, 'message': 'Review deleted'})
        messages.success(request, 'Review deleted')
    else:
        if is_ajax:
            return JsonResponse({'success': False, 'message': 'You do not have permission to delete this review'})
        messages.error(request, 'You do not have permission to delete this review')

    return redirect(movie.get_absolute_url() + '#reviews-section')


@require_POST
def report_broken_link(request, pk):
    """
    Receives a broken-link report from a user on the movie detail page.
    Sends a notification email to the admin via Brevo (formerly Sendinblue).
 
    POST body (form-encoded or JSON):
        episode_note  – optional free-text from user, e.g. "Episode 5 link"
        link_label    – optional label of the specific download link clicked
    """
    movie = get_object_or_404(Movie, pk=pk)
 
    # ── Collect context ──────────────────────────────────────────────────────
    episode_note = (
        request.POST.get('episode_note', '').strip()
        or request.GET.get('episode_note', '').strip()
    )
    link_label = (
        request.POST.get('link_label', '').strip()
        or request.GET.get('link_label', '').strip()
    )
 
    movie_url      = request.build_absolute_uri(movie.get_absolute_url())
    admin_edit_url = request.build_absolute_uri(
        f"/watch2d/watch2d_admin/admin/movies/movie/{movie.pk}/change/"
    )
 
    # Build the email body
    specific_link_line = ""
    if link_label:
        specific_link_line = f"<li><strong>Link clicked:</strong> {link_label}</li>"
 
    episode_line = ""
    if episode_note:
        episode_line = f"<li><strong>User note:</strong> {episode_note}</li>"
 
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <h2 style="color: #e53e3e; border-bottom: 2px solid #e53e3e; padding-bottom: 10px;">
        🔗 Broken Download Link Reported
      </h2>
      <p style="color: #4a5568;">A user has reported a broken or missing download link.</p>
      <table style="width:100%; border-collapse:collapse; margin-top:16px;">
        <tr style="background:#f7fafc;">
          <td style="padding:10px; border:1px solid #e2e8f0; font-weight:bold; width:35%;">Movie</td>
          <td style="padding:10px; border:1px solid #e2e8f0;">{movie.title}</td>
        </tr>
        <tr>
          <td style="padding:10px; border:1px solid #e2e8f0; font-weight:bold;">Movie ID</td>
          <td style="padding:10px; border:1px solid #e2e8f0;">#{movie.pk}</td>
        </tr>
        <tr style="background:#f7fafc;">
          <td style="padding:10px; border:1px solid #e2e8f0; font-weight:bold;">Page URL</td>
          <td style="padding:10px; border:1px solid #e2e8f0;">
            <a href="{movie_url}" style="color:#3182ce;">{movie_url}</a>
          </td>
        </tr>
        {"<tr><td style='padding:10px; border:1px solid #e2e8f0; font-weight:bold;'>Link Label</td><td style='padding:10px; border:1px solid #e2e8f0;'>" + link_label + "</td></tr>" if link_label else ""}
        {"<tr style='background:#f7fafc;'><td style='padding:10px; border:1px solid #e2e8f0; font-weight:bold;'>User Note</td><td style='padding:10px; border:1px solid #e2e8f0;'>" + episode_note + "</td></tr>" if episode_note else ""}
        <tr {"style='background:#f7fafc;'" if not episode_note else ""}>
          <td style="padding:10px; border:1px solid #e2e8f0; font-weight:bold;">Reported by</td>
          <td style="padding:10px; border:1px solid #e2e8f0;">
            {request.user.username if request.user.is_authenticated else "Guest"}
          </td>
        </tr>
      </table>
      <div style="margin-top:28px; text-align:center;">
        <a href="{admin_edit_url}"
           style="display:inline-block; padding:12px 28px; background:#3182ce; color:#fff;
                  font-weight:bold; font-size:15px; text-decoration:none; border-radius:8px;
                  margin-right:12px;">
          ✏️ Edit Movie in Admin
        </a>
        <a href="{movie_url}"
           style="display:inline-block; padding:12px 28px; background:#718096; color:#fff;
                  font-weight:bold; font-size:15px; text-decoration:none; border-radius:8px;">
          🎬 View Movie Page
        </a>
      </div>
      <p style="margin-top:24px; color:#a0aec0; font-size:12px;">
        — Watch2D automated alert
      </p>
    </div>
    """
 
    # ── Send via Brevo ────────────────────────────────────────────────────────
    import logging
    logger = logging.getLogger(__name__)

    brevo_api_key = getattr(settings, 'BREVO_API_KEY', '')
    admin_email   = getattr(settings, 'BREVO_ADMIN_EMAIL', '')
    sender_email  = getattr(settings, 'BREVO_SENDER_EMAIL', '')
    sender_name   = getattr(settings, 'BREVO_SENDER_NAME', 'Watch2D Alerts')

    if not brevo_api_key or not admin_email:
        logger.error(
            "report_broken_link: cannot send email — "
            f"BREVO_API_KEY={'SET' if brevo_api_key else 'MISSING'}, "
            f"BREVO_ADMIN_EMAIL={'SET' if admin_email else 'MISSING'}"
        )
        # Still acknowledge receipt so user isn't confused, but log clearly
        return JsonResponse({'status': 'ok', 'message': 'Report received. Thank you!'})

    try:
        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key['api-key'] = brevo_api_key

        api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
            sib_api_v3_sdk.ApiClient(configuration)
        )

        send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
            to=[{"email": admin_email}],
            sender={"name": sender_name, "email": sender_email or admin_email},
            subject=f"🔗 Broken Link: {movie.title}",
            html_content=html_content,
        )

        api_response = api_instance.send_transac_email(send_smtp_email)
        logger.info(f"report_broken_link: email sent OK for movie #{movie.pk} — messageId={getattr(api_response, 'message_id', 'n/a')}")

    except ApiException as e:
        logger.error(
            f"report_broken_link: Brevo ApiException for movie #{movie.pk} — "
            f"status={e.status}, reason={e.reason}, body={e.body}"
        )
        return JsonResponse({'status': 'error', 'message': 'Failed to send report.'}, status=500)

    except Exception as e:
        logger.error(f"report_broken_link: unexpected error for movie #{movie.pk} — {type(e).__name__}: {e}")
        return JsonResponse({'status': 'error', 'message': 'Failed to send report.'}, status=500)

    return JsonResponse({'status': 'ok', 'message': 'Report received. Thank you!'})




# ═══════════════════════════════════════════════════════════════════════════
# DOWNLOAD GATE VIEW  —  paste at the bottom of  movies/views.py
# ═══════════════════════════════════════════════════════════════════════════
#
# Also add  DownloadGateView  to the import in movies/urls.py (see urls.py).
#
# No new imports needed — everything referenced below is already present
# at the top of views.py.
# ═══════════════════════════════════════════════════════════════════════════

from urllib.parse import unquote as _url_unquote


class DownloadGateView(DetailView):
    """
    Intermediate "gate" page shown between movie_detail and the real download.

    URL:  /movie/<pk>/download/?link=<DownloadLink.pk>
      or  /movie/<pk>/download/?url=<percent-encoded-url>

    This view renders INSTANTLY — it does NOT pre-fetch the download URL.
    The browser-side JS on the gate page calls /resolve-download/ via fetch()
    in parallel with the countdown timer, exactly as handleDownload() used to
    do on movie_detail.  This keeps page load fast even when resolution takes
    several seconds (e.g. downloadwella POST scrape).

    What the view provides to the template:
        movie           – Movie instance (with categories + download_links)
        link_obj        – DownloadLink instance, or None for url= param
        link_label      – Human-readable label, e.g. "Episode 5 (720p)"
        landing_url     – The raw URL JS will resolve (nkiri/downloadwella page)
        countdown       – Seconds for the countdown timer (default 5)
        seo_type        – e.g. "Korean Drama", "Hollywood Movie"
        related_movies  – Up to 8 related movies for the suggestions strip
        categories      – Sidebar categories (required by base.html)
        disable_global_popunder – True → base.html skips its click-popunder
    """

    model = Movie
    template_name = 'movies/download_gate.html'
    COUNTDOWN_SECONDS = 5

    # ── Queryset ──────────────────────────────────────────────────────────────
    def get_queryset(self):
        return Movie.objects.prefetch_related('categories', 'download_links')

    def get_object(self, queryset=None):
        if queryset is None:
            queryset = self.get_queryset()
        return get_object_or_404(queryset, pk=self.kwargs['pk'])

    # ── Request handling ──────────────────────────────────────────────────────
    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        # Real proof they watched it HERE (not just self-reported) — the trust
        # signal RT/Letterboxd can't offer since they don't host the content.
        if request.user.is_authenticated:
            self.object.watched_by.add(request.user)
            self.object.verified_watched_by.add(request.user)
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    # ── Context ───────────────────────────────────────────────────────────────
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        movie   = context['object']
        request = self.request

        # ── 1. Figure out which link was requested ────────────────────────────
        link_pk  = request.GET.get('link', '').strip()
        raw_url  = request.GET.get('url',  '').strip()

        link_obj    = None
        link_label  = 'Download'
        landing_url = ''

        if link_pk:
            # ?link=<DownloadLink.pk> — the normal case from episode buttons
            try:
                link_obj    = movie.download_links.get(pk=int(link_pk))
                landing_url = link_obj.url
                link_label  = link_obj.label or 'Download'
            except (DownloadLink.DoesNotExist, ValueError):
                pass  # fall through to other options

        if not landing_url and raw_url:
            # ?url=<encoded> — from the legacy single download_url field
            landing_url = _url_unquote(raw_url)
            link_label  = 'Download'

        if not landing_url and movie.download_url:
            # Last resort: use the movie's own download_url
            landing_url = movie.download_url
            link_label  = 'Download'

        # ── 2. SEO type (same logic as MovieDetailView) ───────────────────────
        movie_categories = list(movie.categories.all())   # uses prefetch cache
        category_names   = [c.name.lower() for c in movie_categories]
        country          = (movie.vi_country or '').lower()

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
        elif '18plus' in category_names or '18+ movie' in category_names:
            seo_type = 'Adult Movie'
        else:
            seo_type = 'Movie'

        # ── 3. Related movies ─────────────────────────────────────────────────
        related_fields = ('id', 'title', 'slug', 'image_url', 'rating', 'trailer_url')
        if movie_categories:
            related_movies = list(
                Movie.objects
                .only(*related_fields)
                .prefetch_related('external_ratings')
                .filter(categories__in=movie_categories)
                .exclude(pk=movie.pk)
                .distinct()
                .order_by('-created_at')[:8]
            )
        else:
            related_movies = list(
                Movie.objects
                .only(*related_fields)
                .prefetch_related('external_ratings')
                .exclude(pk=movie.pk)
                .order_by('-created_at')[:8]
            )

        # ── 3.5. Episode navigation (series with per-episode links) ───────────
        #   Build a distinct, ordered episode list (one best-priority link each)
        #   from the already-prefetched links, so users can go to the next/prev
        #   episode or jump straight to any episode from the gate page.
        episodes = []
        current_ep = prev_ep = next_ep = None
        if link_obj is not None and link_obj.episode_number:
            reps = {}
            for dl in movie.download_links.all():   # prefetched — no extra query
                n = dl.episode_number
                if not n:
                    continue
                cur = reps.get(n)
                if cur is None or (dl.priority, dl.pk) < (cur.priority, cur.pk):
                    reps[n] = dl
            ordered = sorted(reps.values(), key=lambda d: d.episode_number)
            episodes = [{'n': d.episode_number, 'link_pk': d.pk} for d in ordered]
            current_ep = link_obj.episode_number
            nums = [e['n'] for e in episodes]
            if current_ep in nums and len(episodes) > 1:
                i = nums.index(current_ep)
                if i > 0:
                    prev_ep = episodes[i - 1]
                if i < len(episodes) - 1:
                    next_ep = episodes[i + 1]

        # ── 4. Pack context ───────────────────────────────────────────────────
        context.update({
            'movie':          movie,
            'link_obj':       link_obj,
            'link_label':     link_label,
            'landing_url':    landing_url,
            'countdown':      self.COUNTDOWN_SECONDS,
            'seo_type':       seo_type,
            'related_movies': related_movies,
            'episodes':       episodes,
            'current_ep':     current_ep,
            'prev_ep':        prev_ep,
            'next_ep':        next_ep,
            'categories':     get_sidebar_categories(),
            # Tells base.html to skip the global click-popunder so the gate's
            # own ad script is the sole popunder on this page.
            'disable_global_popunder': True,
            'watchlisted_ids': (
                set(request.user.watchlist_movies.values_list('id', flat=True))
                if request.user.is_authenticated else set()
            ),
        })
        return context


class StreamGateView(DetailView):
    """
    Dedicated streaming page for a movie that has a `stream_url`
    (set by the moviebox / streamimdb scrapers — separate from downloads).

    URL:  /movie/<pk>/stream/

    Renders stream_gate.html, which embeds the streaming player in an iframe.
    The actual stream is fetched client-side by the embedded player in the
    viewer's browser; we only hand it the embed URL.
    """

    model = Movie
    template_name = 'movies/stream_gate.html'

    def get_queryset(self):
        return Movie.objects.prefetch_related('categories')

    def get_object(self, queryset=None):
        if queryset is None:
            queryset = self.get_queryset()
        return get_object_or_404(queryset, pk=self.kwargs['pk'])

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        # No stream → bounce back to the detail page.
        if not self.object.stream_url:
            return redirect(self.object.get_absolute_url())
        if request.user.is_authenticated:
            self.object.watched_by.add(request.user)
            self.object.verified_watched_by.add(request.user)
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        movie   = context['object']

        # ── SEO type (same logic as the download gate) ────────────────────────
        movie_categories = list(movie.categories.all())
        category_names   = [c.name.lower() for c in movie_categories]
        country          = (movie.vi_country or '').lower()

        if 'chinese drama' in category_names or 'chinese' in country:
            seo_type = 'Chinese Drama'
        elif 'korean drama' in category_names or 'k drama' in category_names or 'korean' in country:
            seo_type = 'Korean Drama'
        elif 'thai drama' in category_names or 'thai' in country:
            seo_type = 'Thai Drama'
        elif 'anime' in category_names:
            seo_type = 'Anime Series'
        elif 'tv series' in category_names or 'series' in category_names:
            seo_type = 'TV Series'
        elif 'bollywood' in category_names or 'bollywood movies' in category_names:
            seo_type = 'Bollywood Movie'
        elif 'nollywood movie' in category_names or 'nollywood movies' in category_names or 'nollywood' in category_names:
            seo_type = 'Nollywood Movie'
        elif 'hollywood movie' in category_names or 'hollywood movies' in category_names or 'hollywood' in category_names:
            seo_type = 'Hollywood Movie'
        else:
            seo_type = 'Movie'

        # ── Related movies ────────────────────────────────────────────────────
        related_fields = ('id', 'title', 'slug', 'image_url', 'rating', 'trailer_url')
        if movie_categories:
            related_movies = list(
                Movie.objects
                .only(*related_fields)
                .prefetch_related('external_ratings')
                .filter(categories__in=movie_categories)
                .exclude(pk=movie.pk)
                .distinct()
                .order_by('-created_at')[:8]
            )
        else:
            related_movies = list(
                Movie.objects
                .only(*related_fields)
                .prefetch_related('external_ratings')
                .exclude(pk=movie.pk)
                .order_by('-created_at')[:8]
            )

        # Full server chain built from tmdb_id (streamimdb, vidlink, vidsrc,
        # 2embed) — deduped and with the stored stream_url leading, so viewers
        # get real numbered servers to switch between, not a single blind
        # fallback.
        from movies.stream_providers import build_stream_chain
        chain = build_stream_chain(
            movie.tmdb_id, is_series=movie.is_series,
            season=movie.season_number or 1)
        sources = []
        seen = set()
        for url in [movie.stream_url] + chain:
            url = (url or '').strip()
            if url and url not in seen:
                seen.add(url)
                sources.append(url)

        context.update({
            'movie':           movie,
            'stream_url':      movie.stream_url,
            'stream_sources':  sources,
            'tmdb_id':         movie.tmdb_id or '',
            'is_series':       movie.is_series,
            'tmdb_seasons':    movie.tmdb_seasons or '',
            'seo_type':        seo_type,
            'related_movies':  related_movies,
            'categories':      get_sidebar_categories(),
            'disable_global_popunder': True,
            'watchlisted_ids': (
                set(self.request.user.watchlist_movies.values_list('id', flat=True))
                if self.request.user.is_authenticated else set()
            ),
        })
        return context


# ════════════════════════════════════════════════════════════════════════════
#  ACTOR / CAST PAGE  —  /actor/<pk>/<slug>/
#  Every cast member becomes an SEO-friendly page listing the titles they're in.
# ════════════════════════════════════════════════════════════════════════════
class ActorView(DetailView):
    model = Person
    template_name = 'movies/actor.html'
    context_object_name = 'person'

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        # Canonicalise the slug (301 to the correct one) for SEO.
        if kwargs.get('slug', '') != self.object.slug:
            return redirect(self.object.get_absolute_url(), permanent=True)
        return self.render_to_response(self.get_context_data(object=self.object))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        person = context['person']
        credits = (
            MovieCast.objects
            .filter(person=person)
            .select_related('movie')
            .prefetch_related('movie__external_ratings')
            .order_by('order', '-movie__created_at')
        )
        # De-dup movies (a person can have one credit per movie via unique_together,
        # but guard anyway) and keep their character label.
        seen, titles = set(), []
        for c in credits:
            if c.movie_id in seen:
                continue
            seen.add(c.movie_id)
            titles.append({'movie': c.movie, 'character': c.character})
        context['titles'] = titles
        context['title_count'] = len(titles)
        if self.request.user.is_authenticated:
            context['watchlisted_ids'] = set(
                self.request.user.watchlist_movies.values_list('id', flat=True))
        else:
            context['watchlisted_ids'] = set()
        return context


PERSON_FILMOGRAPHY_PREVIEW_LIMIT = 6


def person_filmography(request, pk):
    """Small HTML fragment — a person's other titles for the site-wide
    'filmography' popup, so seeing what someone's been in is one click from
    the movie page instead of a full navigation most people never discover."""
    person = get_object_or_404(Person, pk=pk)
    credits = (
        MovieCast.objects
        .filter(person=person)
        .select_related('movie')
        .order_by('order', '-movie__created_at')
    )
    seen, titles = set(), []
    for c in credits:
        if c.movie_id in seen:
            continue
        seen.add(c.movie_id)
        titles.append({'movie': c.movie, 'character': c.character})
    total = len(titles)
    html = render_to_string('movies/components/filmography_content.html', {
        'person': person,
        'titles': titles[:PERSON_FILMOGRAPHY_PREVIEW_LIMIT],
        'total': total,
        'has_more': total > PERSON_FILMOGRAPHY_PREVIEW_LIMIT,
    }, request=request)
    return HttpResponse(html)


# ════════════════════════════════════════════════════════════════════════════
#  COMING SOON  —  /coming-soon/
#  Not-yet-released TMDB titles; auto-removed once they enter the catalogue.
# ════════════════════════════════════════════════════════════════════════════
class ComingSoonView(ListView):
    model = UpcomingTitle
    template_name = 'movies/coming_soon.html'
    context_object_name = 'upcoming'
    paginate_by = 24

    def get_queryset(self):
        from django.utils import timezone
        today = timezone.now().date().isoformat()
        return (
            UpcomingTitle.objects
            .filter(release_date__gte=today)
            .order_by('release_date')
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['categories'] = get_sidebar_categories()
        if self.request.user.is_authenticated:
            context['notify_requested_ids'] = set(
                NotifyRequest.objects.filter(user=self.request.user)
                .values_list('tmdb_id', flat=True))
        else:
            context['notify_requested_ids'] = set()
        return context


# ══════════════════════════════════════════════════════════════════════════════
#  STREAMING ONLY  —  /streaming-only/
#  Movies/series that have a working stream but no download link yet (either
#  never had one, or it's still being resolved after a fresh scrape).
# ══════════════════════════════════════════════════════════════════════════════
class StreamOnlyView(ListView):
    model = Movie
    template_name = 'movies/movie_list_by_cat.html'
    context_object_name = 'movies'
    paginate_by = 24

    def get_queryset(self):
        from django.db.models import Count, Q
        qs = (
            Movie.objects
            .prefetch_related('external_ratings')
            .exclude(Q(stream_url='') | Q(stream_url__isnull=True))
            # download_url is NULL (not '') for most rows that never had the
            # legacy single-URL field set — filtering only on '' silently
            # excluded almost every genuinely streaming-only title.
            .filter(Q(download_url='') | Q(download_url__isnull=True))
            .annotate(num_links=Count('download_links'))
            .filter(num_links=0)
        )
        self.query = self.request.GET.get('q', '').strip()
        if self.query:
            qs = qs.filter(title__icontains=self.query)
        return qs.order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['categories'] = get_sidebar_categories()
        context['category'] = None
        context['stream_only'] = True
        context['query'] = self.query
        if self.request.user.is_authenticated:
            context['watchlisted_ids'] = set(
                self.request.user.watchlist_movies.values_list('id', flat=True))
        else:
            context['watchlisted_ids'] = set()
        return context


# ══════════════════════════════════════════════════════════════════════════════
#  TOP MOVIES / TOP TV SHOWS  —  /top-movies/, /top-tv-shows/
#  Full ranked "Most Popular" list — the "View All" target for the compact
#  trending_list.html widget shown on home/movie-detail/A-Z.
# ══════════════════════════════════════════════════════════════════════════════
class TopListView(ListView):
    model = Movie
    template_name = 'movies/top_list.html'
    context_object_name = 'movies'
    paginate_by = 50
    is_series = False  # overridden by subclasses
    page_heading = 'Top Movies'

    def get_queryset(self):
        qs = (
            Movie.objects
            .only('id', 'title', 'slug', 'image_url', 'rating', 'views', 'is_series')
            .prefetch_related('external_ratings')
            .filter(is_series=self.is_series)
        )
        self.query = self.request.GET.get('q', '').strip()
        if self.query:
            qs = qs.filter(title__icontains=self.query)
        return qs.order_by('-views', '-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['categories'] = get_sidebar_categories()
        context['page_heading'] = self.page_heading
        context['query'] = self.query
        return context


class TopMoviesView(TopListView):
    is_series = False
    page_heading = 'Top Movies'


class TopSeriesView(TopListView):
    is_series = True
    page_heading = 'Top TV Shows'


# ══════════════════════════════════════════════════════════════════════════════
#  WATCHLIST / LIKED  —  /watchlist/, /liked/
#  A signed-in user's own saved titles.
# ══════════════════════════════════════════════════════════════════════════════
class WatchlistView(LoginRequiredMixin, ListView):
    model = Movie
    template_name = 'movies/watchlist.html'
    context_object_name = 'movies'
    paginate_by = 24

    def get_queryset(self):
        return self.request.user.watchlist_movies.prefetch_related('external_ratings').order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['categories'] = get_sidebar_categories()
        context['page_title'] = 'My Watchlist'
        context['watchlisted_ids'] = set(
            self.request.user.watchlist_movies.values_list('id', flat=True))
        return context


class LikedView(LoginRequiredMixin, ListView):
    model = Movie
    template_name = 'movies/watchlist.html'
    context_object_name = 'movies'
    paginate_by = 24

    def get_queryset(self):
        return self.request.user.liked_movies.prefetch_related('external_ratings').order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['categories'] = get_sidebar_categories()
        context['watchlisted_ids'] = set(
            self.request.user.watchlist_movies.values_list('id', flat=True))
        context['page_title'] = 'My Liked Movies'
        return context


class WatchedView(LoginRequiredMixin, ListView):
    """Self-reported diary log — everything the user has marked as watched."""
    model = Movie
    template_name = 'movies/watchlist.html'
    context_object_name = 'movies'
    paginate_by = 24

    def get_queryset(self):
        return self.request.user.watched_movies.prefetch_related('external_ratings').order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['categories'] = get_sidebar_categories()
        context['watchlisted_ids'] = set(
            self.request.user.watchlist_movies.values_list('id', flat=True))
        context['page_title'] = 'Watched'
        return context


# ══════════════════════════════════════════════════════════════════════════════
#  PROFILE  —  /u/<display_name>/
#  Keyed by the anonymous nickname, never the account username — nobody's
#  real identity (username, email, or Google photo) is ever shown publicly.
# ══════════════════════════════════════════════════════════════════════════════
class ProfileView(DetailView):
    model = User
    template_name = 'movies/profile.html'
    context_object_name = 'profile_user'
    slug_field = 'profile__display_name'
    slug_url_kwarg = 'display_name'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile_user = context['profile_user']
        context['avatar_url'] = profile_user.profile.avatar_url

        context['reviews'] = (
            Review.objects.filter(user=profile_user)
            .select_related('movie')
            .order_by('-created_at')[:30]
        )
        context['review_count'] = Review.objects.filter(user=profile_user).count()
        context['watchlist_count'] = profile_user.watchlist_movies.count()
        context['liked_count'] = profile_user.liked_movies.count()
        context['watched_count'] = profile_user.watched_movies.count()
        context['categories'] = get_sidebar_categories()

        movie_fields = ('id', 'title', 'slug', 'image_url', 'rating', 'trailer_url')
        context['watchlist_preview'] = (
            profile_user.watchlist_movies.only(*movie_fields)
            .prefetch_related('external_ratings')[:12]
        )
        context['liked_preview'] = (
            profile_user.liked_movies.only(*movie_fields)
            .prefetch_related('external_ratings')[:12]
        )
        if self.request.user.is_authenticated:
            context['watchlisted_ids'] = set(
                self.request.user.watchlist_movies.values_list('id', flat=True))
        else:
            context['watchlisted_ids'] = set()
        return context


@login_required
@require_POST
def update_display_name(request):
    """Change your public nickname (never the underlying account username)."""
    import re as _re_mod
    new_name = request.POST.get('display_name', '').strip()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if not (2 <= len(new_name) <= 30) or not _re_mod.match(r'^[A-Za-z0-9_ ]+$', new_name):
        msg = 'Nickname must be 2-30 characters (letters, numbers, spaces, underscores only)'
        if is_ajax:
            return JsonResponse({'success': False, 'message': msg})
        messages.error(request, msg)
        return redirect('movies:profile', display_name=request.user.profile.display_name)

    if Profile.objects.filter(display_name__iexact=new_name).exclude(user=request.user).exists():
        msg = 'That nickname is already taken'
        if is_ajax:
            return JsonResponse({'success': False, 'message': msg})
        messages.error(request, msg)
        return redirect('movies:profile', display_name=request.user.profile.display_name)

    request.user.profile.display_name = new_name
    request.user.profile.save(update_fields=['display_name'])
    if is_ajax:
        return JsonResponse({'success': True, 'display_name': new_name})
    messages.success(request, 'Nickname updated!')
    return redirect('movies:profile', display_name=new_name)