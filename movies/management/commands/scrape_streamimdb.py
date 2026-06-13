"""
Management command: scrape_streamimdb
=====================================
Crawl streamimdb.ru listing pages (/movies, /tv-shows), visit each title page,
and upsert it into the Movie DB as a STREAMING entry.

How this site differs from the download scrapers (9jarocks / thenkiri)
─────────────────────────────────────────────────────────────────────
streamimdb.ru is a streaming/embed site — there are NO download links. Every
title page exposes a TMDB id and an embeddable player:

  • Movie page  /movie/<hash>-<slug>   →  window.__cbCwMeta = {"type":"movie","id":"<tmdb>",...}
  • TV page     /tv/<hash>-<slug>       →  window.__cbTvMeta = {"type":"tv","id":"<tmdb>",...}
  • Durable embed (what we STORE)       →  https://streamimdb.ru/embed/<movie|tv>/<tmdb>

What we store in Movie.video_url
────────────────────────────────
The DURABLE EMBED URL (e.g. https://streamimdb.ru/embed/movie/1273221), NOT a
raw stream URL. Reasons (confirmed by probing the site):
  • The real HLS master.m3u8 is fetched from streamdata.vaplayer.ru/api.php and
    ROTATES on every request (the path carries a short-lived token) — a stored
    m3u8 would go stale.
  • That m3u8 returns HTTP 403 without a `Referer: nextgencloudfabric.com`
    header, which a <video>/hls.js player on your own domain cannot send.
  • The embed URL iframes cleanly through the existing player fallback in
    movie_detail.html and handles HLS + referer + episode selection internally.

We STILL resolve the real stream during scraping — but only as a LIVENESS CHECK
(skip titles that have no working source). Use --allow-unverified to store them
anyway.

Stream resolution chain (for the liveness check)
────────────────────────────────────────────────
  streamimdb.ru page  →  tmdb id
                      →  GET https://streamdata.vaplayer.ru/api.php?tmdb=<id>&type=movie
                         (or &type=tv&season=1&episode=1), Referer/Origin = the
                         player host → JSON { data: { stream_urls: [...] } }

Usage
─────
python manage.py scrape_streamimdb
python manage.py scrape_streamimdb --media movie
python manage.py scrape_streamimdb --media tv
python manage.py scrape_streamimdb --startpage 1 --endpage 5
python manage.py scrape_streamimdb --media movie --max-pages 3 --no-social
python manage.py scrape_streamimdb --category hollywood        # force a DB category
python manage.py scrape_streamimdb --allow-unverified          # keep titles with no live stream
"""

import json
import re
import time

import urllib3
from django.core.management.base import BaseCommand
from django.db import IntegrityError
from django.utils import timezone

import cloudscraper
from bs4 import BeautifulSoup

from movies.models import Movie

# Re-use the generic DB + social helpers from the 9jarocks scraper so we don't
# duplicate ~200 lines of Telegram/Facebook posting and DB matching logic.
from .scrape_9jarocks import (
    find_existing_movie,
    assign_db_categories,
    _post_to_all_platforms,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ══════════════════════════════════════════════════════════════
# SITE CONSTANTS
# ══════════════════════════════════════════════════════════════

SITE_URL   = 'https://streamimdb.ru'
STREAM_API = 'https://streamdata.vaplayer.ru/api.php'

# The real player iframe is served from this host; the stream API + the m3u8
# CDN both gate on this Referer/Origin. We only need it for the liveness check.
PLAYER_HOST    = 'https://nextgencloudfabric.com'
PLAYER_HEADERS = {
    'Referer': PLAYER_HOST + '/',
    'Origin':  PLAYER_HOST,
    'Accept':  '*/*',
}

# Listing item links look like  /movie/84s7q-scary-movie  or  /tv/19cem-jungle-cubs
_ITEM_HREF_RE = re.compile(r'^/(movie|tv)/[a-z0-9]+-', re.IGNORECASE)

# window.__cbCwMeta (movie) or window.__cbTvMeta (tv)
_META_RE = re.compile(r'__cb(?:Cw|Tv)Meta\s*=\s*(\{.*?\})\s*;', re.DOTALL)


# ── Category inference ────────────────────────────────────────
# streamimdb only exposes TMDB genres + country of origin, while your sidebar
# categories are origin-based. Map country → DB category names; everything else
# falls back to Hollywood (movies) / Series (tv). Override with --category.
_COUNTRY_TO_DB = {
    'south korea': ['Korean drama'],
    'korea':       ['Korean drama'],
    'china':       ['Chinese drama'],
    'taiwan':      ['Chinese drama'],
    'hong kong':   ['Chinese drama'],
    'thailand':    ['Thai drama'],
    'india':       ['Bollywood movies'],
    'nigeria':     ['Nollywood movies'],
}

# Friendly --category aliases → DB category name list (forces the assignment).
_CATEGORY_ALIASES = {
    'hollywood': ['Hollywood movies'],
    'kdrama':    ['Korean drama'],
    'korean':    ['Korean drama'],
    'chinese':   ['Chinese drama'],
    'thai':      ['Thai drama'],
    'bollywood': ['Bollywood movies'],
    'nollywood': ['Nollywood movies'],
    'anime':     ['Anime'],
    'animation': ['Animation'],
    'series':    ['Series'],
}


# ══════════════════════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════════════════════

def _make_scraper():
    """Return a cloudscraper session with browser-like headers."""
    scraper = cloudscraper.create_scraper()
    scraper.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': SITE_URL + '/',
    })
    return scraper


# ══════════════════════════════════════════════════════════════
# PARSERS
# ══════════════════════════════════════════════════════════════

def get_item_urls_from_listing(html: str, media_filter: str | None = None) -> list[str]:
    """
    Extract all movie/tv title URLs from a listing page.
    media_filter: 'movie', 'tv', or None (both).
    """
    soup = BeautifulSoup(html, 'html.parser')
    urls, seen = [], set()
    for a in soup.find_all('a', href=True):
        m = _ITEM_HREF_RE.match(a['href'])
        if not m:
            continue
        if media_filter and m.group(1).lower() != media_filter:
            continue
        full = SITE_URL + a['href']
        if full not in seen:
            seen.add(full)
            urls.append(full)
    return urls


def _extract_meta(html: str) -> dict | None:
    """Parse the window.__cbCwMeta / __cbTvMeta JSON object."""
    m = _META_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _extract_ld(soup: BeautifulSoup) -> dict:
    """Return the first Movie / TVSeries JSON-LD block, or {}."""
    for tag in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(tag.string or '')
        except Exception:
            continue
        if isinstance(data, dict) and data.get('@type') in ('Movie', 'TVSeries'):
            return data
    return {}


def _iso_to_runtime(iso: str) -> str:
    """'PT1H35M' / 'PT95M' → '1h 35m' / '95m'."""
    if not iso:
        return ''
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?', iso)
    if not m:
        return ''
    h  = int(m.group(1) or 0)
    mn = int(m.group(2) or 0)
    if h and mn:
        return f'{h}h {mn}m'
    if h:
        return f'{h}h'
    if mn:
        return f'{mn}m'
    return ''


def parse_page(html: str, url: str) -> dict | None:
    """
    Parse a streamimdb.ru movie or tv page.

    Returns a dict with: tmdb_id, media_type, title_raw, description, image_url,
    is_series, and the vi_* metadata fields — or None if no TMDB id is found
    (i.e. it isn't a playable title page).
    """
    meta = _extract_meta(html)
    soup = BeautifulSoup(html, 'html.parser')
    ld   = _extract_ld(soup)

    media_type = (meta or {}).get('type')
    tmdb_id    = str((meta or {}).get('id') or '').strip()
    title_raw  = ((meta or {}).get('title') or '').strip()
    poster     = ((meta or {}).get('poster') or '').strip()

    if not media_type:
        media_type = 'tv' if '/tv/' in url else 'movie'

    # Fallback: data-src="/embed/movie/123"  /  "/embed/tv/123"
    if not tmdb_id:
        ds = re.search(r'/embed/(movie|tv)/(\d+)', html)
        if ds:
            media_type = ds.group(1)
            tmdb_id    = ds.group(2)

    if not tmdb_id:
        return None

    if not title_raw:
        title_raw = (ld.get('name') or '').strip()
    if not title_raw:
        return None

    # Year from datePublished (e.g. "2026")
    year = ''
    ym = re.search(r'(\d{4})', str(ld.get('datePublished') or ''))
    if ym:
        year = ym.group(1)

    # Description: prefer the fuller #cbPlot overview, fall back to JSON-LD.
    description = ''
    plot = soup.find(id='cbPlot')
    if plot:
        description = plot.get_text(strip=True)
    if not description:
        description = (ld.get('description') or '').strip()

    image_url = poster or (ld.get('image') or '')

    runtime = _iso_to_runtime(str(ld.get('duration') or ''))

    genres = ld.get('genre') or []
    if isinstance(genres, str):
        genres = [genres]
    genre = ', '.join(g for g in genres if g)

    actors = ld.get('actor') or []
    cast = ', '.join(a.get('name', '') for a in actors if isinstance(a, dict) and a.get('name'))

    country = ''
    co = ld.get('countryOfOrigin')
    if isinstance(co, dict):
        country = co.get('name', '')
    elif isinstance(co, list) and co and isinstance(co[0], dict):
        country = co[0].get('name', '')

    is_series = (media_type == 'tv') or (ld.get('@type') == 'TVSeries')

    return {
        'tmdb_id':     tmdb_id,
        'media_type':  'tv' if is_series else 'movie',
        'title_raw':   title_raw,
        'description': description,
        'image_url':   image_url,
        'is_series':   is_series,
        'vi_year':     year,
        'vi_genre':    genre,
        'vi_cast':     cast,
        'vi_country':  country,
        'vi_runtime':  runtime,
        'vi_language': '',
        'vi_subtitle': '',
        'vi_episodes': '',
        'vi_status':   '',
        'vi_filesize': '',
    }


def build_embed_url(media_type: str, tmdb_id: str) -> str:
    """Durable, iframe-able player URL — what we store in Movie.video_url."""
    mt = 'tv' if media_type == 'tv' else 'movie'
    return f'{SITE_URL}/embed/{mt}/{tmdb_id}'


def resolve_stream(scraper, tmdb_id: str, media_type: str,
                   season: int = 1, episode: int = 1) -> dict | None:
    """
    Liveness check: hit the real stream API and confirm at least one stream URL
    exists. Returns a small info dict (stream_count, file_name, imdb_id,
    backdrop) or None if no live source. We do NOT store the returned m3u8 — it
    rotates per request and is referer-locked.
    """
    is_tv = (media_type == 'tv')
    params = f'?tmdb={tmdb_id}&type={"tv" if is_tv else "movie"}'
    if is_tv:
        params += f'&season={season}&episode={episode}'
    try:
        r = scraper.get(STREAM_API + params, headers=PLAYER_HEADERS, timeout=25)
        if r.status_code != 200:
            return None
        j = r.json()
    except Exception:
        return None

    if str(j.get('status_code')) != '200':
        return None
    data    = j.get('data') or {}
    streams = data.get('stream_urls') or []
    if not streams:
        return None
    return {
        'stream_count': len(streams),
        'file_name':    data.get('file_name', ''),
        'imdb_id':      data.get('imdb_id', ''),
        'backdrop':     data.get('backdrop', ''),
    }


def infer_db_cats(parsed: dict) -> list[str]:
    """Best-effort DB category list from country/genre/media type."""
    country = (parsed.get('vi_country') or '').lower()
    genre   = (parsed.get('vi_genre') or '').lower()
    is_tv   = parsed.get('is_series')

    cats: list[str] = []
    if 'japan' in country:
        cats = ['Anime'] if 'anim' in genre else ['Series']
    else:
        for key, vals in _COUNTRY_TO_DB.items():
            if key in country:
                cats = list(vals)
                break
    if not cats:
        if 'animation' in genre:
            cats = ['Animation']
        else:
            cats = ['Hollywood movies']

    if is_tv and 'Series' not in cats:
        cats.append('Series')
    return cats


def clean_title(raw: str) -> str:
    """Plain show/movie name: collapse whitespace, drop any [..] tags."""
    t = re.sub(r'\s+', ' ', raw or '').strip()
    t = re.sub(r'\s*\[[^\]]*\]\s*', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


# Reduce any stored title to its bare SHOW name, so streamimdb's single
# whole-show entry can find every per-season record you already have:
#   "From Season 1 (Complete)"           -> "from"
#   "From (Episode 3 Added) | TV Series" -> "from"
#   "From (2022)"                        -> "from"
_SHOW_KEY_SUBS = [
    re.compile(r'\((?:19|20)\d\d\)'),                        # (year)
    re.compile(r'\(?\s*(?:complete|completed)\s*\)?', re.I),
    re.compile(r'\(?\s*episode[^)]*\)?', re.I),              # (Episode 3 Added)
    re.compile(r'[\-–|:]?\s*\bTV\s*Series\b.*$', re.I),
    re.compile(r'\bS(?:eason)?\s*0*\d{1,2}\b.*$', re.I),     # Season N / S01 onward
]


def show_key(title: str) -> str:
    t = title or ''
    for rx in _SHOW_KEY_SUBS:
        t = rx.sub(' ', t)
    t = re.sub(r'[^a-z0-9]+', ' ', t.lower())
    return re.sub(r'\s+', ' ', t).strip()


# ══════════════════════════════════════════════════════════════
# UPSERT  (shared by the crawler and the --url/--file variant)
# ══════════════════════════════════════════════════════════════

def save_item(parsed: dict, embed_url: str, db_cats: list[str],
              no_social: bool = False, update_only: bool = False) -> tuple[Movie | None, str]:
    """
    Add streaming to your catalog, honoring how you already store titles:

    • SERIES — your shows are stored PER-SEASON ("From Season 1", "From Season 2"),
      while streamimdb has ONE whole-show player. So we attach the stream to
      EVERY existing season-record of that show. Only if you have none do we
      create a single whole-show record.
    • MOVIE — matched as "Name (Year)" (falling back to a bare-name match with a
      compatible year); enriched, or created as "Name (Year)".

    Never touches download links/url, categories, title, or is_series — so it
    coexists with your 9jarocks / nkiri download scrapers.

    Returns (movie | None, status):
      'enriched' | 'created' | 'unchanged' | 'skipped-no-match'.
    """
    is_series = bool(parsed.get('is_series'))
    name      = clean_title(parsed['title_raw'])
    year      = parsed.get('vi_year', '')

    vi_fields = dict(
        vi_year     = year[:10],
        vi_country  = parsed.get('vi_country', '')[:120],
        vi_language = parsed.get('vi_language', '')[:120],
        vi_subtitle = parsed.get('vi_subtitle', '')[:60],
        vi_genre    = parsed.get('vi_genre', '')[:200],
        vi_cast     = parsed.get('vi_cast', ''),
        vi_episodes = parsed.get('vi_episodes', '')[:20],
        vi_status   = parsed.get('vi_status', '')[:60],
        vi_runtime  = parsed.get('vi_runtime', '')[:30],
        vi_filesize = parsed.get('vi_filesize', '')[:30],
    )

    def _enrich(mv):
        changed = False
        if mv.stream_url != embed_url:
            mv.stream_url = embed_url[:600]; changed = True
        if not mv.image_url and parsed['image_url']:
            mv.image_url = parsed['image_url'][:500]; changed = True
        if not mv.description and parsed['description']:
            mv.description = parsed['description']; changed = True
        for f, v in vi_fields.items():
            if v and not getattr(mv, f, ''):
                setattr(mv, f, v); changed = True
        if changed:
            mv.save()
        return changed

    def _create(title):
        mv = None
        for cand in [title[:200], f"{title} [{parsed['tmdb_id']}]"]:
            try:
                mv = Movie.objects.create(
                    title       = cand[:200],
                    description = parsed['description'],
                    video_url   = '',                       # not a trailer
                    stream_url  = embed_url[:600],
                    image_url   = (parsed['image_url'] or '')[:500],
                    is_series   = is_series,
                    scraped     = True,
                    **vi_fields,
                )
                break
            except IntegrityError:
                continue
        if not mv:
            raise IntegrityError(f"Could not create a unique title for '{title}'")
        assign_db_categories(mv, scraped_cats=[], forced_db_cats=db_cats)
        print(f"      ✅ Created (stream-only): {mv.title}")
        if not no_social:
            _post_to_all_platforms(mv, is_new=True)
        return mv

    # ── SERIES: enrich EVERY existing season-record of the show ────────────
    if is_series:
        key     = show_key(name)
        # Narrow with a prefix query, then exact show-key match (handles
        # "Season 1 (Complete)", "(Episode N Added)", "| TV Series", etc.)
        matches = [mv for mv in Movie.objects.filter(is_series=True,
                                                     title__istartswith=name[:30])
                   if show_key(mv.title) == key]
        if matches:
            n = 0
            for mv in matches:
                if _enrich(mv):
                    n += 1
                    print(f"      🔗 stream added → {mv.title}")
            return matches[0], ('enriched' if n else 'unchanged')
        if update_only:
            return None, 'skipped-no-match'
        return _create(name), 'created'

    # ── MOVIE: a single "Name (Year)" record ──────────────────────────────
    title = f"{name} ({year})" if year else name
    movie = find_existing_movie(title)
    if not movie:
        cand = find_existing_movie(name)         # DB may store it without a year
        if cand and (not year or not cand.vi_year or cand.vi_year == year):
            movie = cand
    if movie:
        return movie, ('enriched' if _enrich(movie) else 'unchanged')
    if update_only:
        return None, 'skipped-no-match'
    return _create(title), 'created'


def resolve_category_arg(cat_arg: str) -> list[str] | None:
    """Turn a --category alias / raw DB name into a DB category name list."""
    if not cat_arg:
        return None
    key = cat_arg.strip().lower()
    if key in _CATEGORY_ALIASES:
        return list(_CATEGORY_ALIASES[key])
    # Treat anything else as a literal DB category name.
    return [cat_arg.strip()]


# ══════════════════════════════════════════════════════════════
# MANAGEMENT COMMAND
# ══════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = (
        'Crawl streamimdb.ru listing pages and upsert each title into the DB as '
        'a streaming entry (durable embed URL in video_url; real stream verified '
        'as a liveness check).'
    )

    def add_arguments(self, parser):
        parser.add_argument('--media', choices=['movie', 'tv', 'both'], default='both',
                            help='Which sections to crawl (default: both).')
        parser.add_argument('--startpage', type=int, default=1,
                            help='Listing page to start from (default: 1).')
        parser.add_argument('--endpage', type=int, default=None,
                            help='Stop after this listing page (inclusive).')
        parser.add_argument('--max-pages', type=int, default=None,
                            help='Maximum listing pages to crawl per section.')
        parser.add_argument('--category', type=str, default=None,
                            help='Force a DB category for every title (alias or raw name). '
                                 'Aliases: hollywood, kdrama, chinese, thai, bollywood, '
                                 'nollywood, anime, animation, series.')
        parser.add_argument('--no-social', action='store_true', default=False,
                            help='Save to DB only — skip all social posts.')
        parser.add_argument('--update-only', action='store_true', default=False,
                            help='Only ADD streaming to movies that already exist; skip titles '
                                 'with no match instead of creating stream-only entries.')
        parser.add_argument('--allow-unverified', action='store_true', default=False,
                            help='Store titles even when the stream liveness check fails.')
        parser.add_argument('--delay', type=float, default=0.4,
                            help='Seconds between individual title requests (default: 0.4).')

    def handle(self, *args, **options):
        from django.db import connection

        media       = options['media']
        start_page  = options['startpage']
        end_page    = options['endpage']
        max_pages   = options['max_pages']
        no_social   = options['no_social']
        update_only = options['update_only']
        allow_unver = options['allow_unverified']
        delay       = options['delay']

        forced_cats = None
        if options['category']:
            forced_cats = resolve_category_arg(options['category'])

        sections = []
        if media in ('movie', 'both'):
            sections.append(('movie', '/movies'))
        if media in ('tv', 'both'):
            sections.append(('tv', '/tv-shows'))

        print('=' * 60)
        print('🚀  streamimdb.ru scraper starting')
        print(f'    Sections : {", ".join(s[1] for s in sections)}')
        print(f'    Pages    : {start_page} → {end_page or "∞"}'
              + (f'  (max {max_pages})' if max_pages else ''))
        print(f'    Category : {", ".join(forced_cats) if forced_cats else "(auto-infer)"}')
        print(f'    Social   : {"DISABLED" if no_social else "ON (Telegram + Facebook)"}')
        print(f'    Verify   : {"liveness optional" if allow_unver else "skip dead streams"}')
        print('=' * 60)

        scraper = _make_scraper()
        total_seen = total_created = total_enriched = total_skipped = 0

        for media_type, base_path in sections:
            print(f'\n\n{"═" * 60}')
            print(f'📂  Section: {base_path}')
            print(f'{"═" * 60}')

            page          = start_page
            pages_crawled = 0
            seen_urls: set[str] = set()

            while True:
                if end_page and page > end_page:
                    print(f'\n✅ Reached end page {end_page}.')
                    break
                if max_pages and pages_crawled >= max_pages:
                    print(f'\n✅ Crawled {max_pages} pages for this section.')
                    break

                listing_url = f'{SITE_URL}{base_path}?page={page}'
                print(f'\n{"─" * 60}')
                print(f'🌐 Listing page {page}: {listing_url}')

                try:
                    resp = scraper.get(listing_url, timeout=25)
                    if resp.status_code == 404:
                        print('   ✅ No more pages (404).')
                        break
                    resp.raise_for_status()
                except Exception as e:
                    print(f'   ❌ Failed to fetch listing page: {e}')
                    break

                pages_crawled += 1
                item_urls = get_item_urls_from_listing(resp.text, media_filter=media_type)
                fresh = [u for u in item_urls if u not in seen_urls]
                print(f'   📋 {len(item_urls)} items ({len(fresh)} new this section)')

                if not fresh:
                    print('   ⚠️ No new items — end of section.')
                    break

                for item_url in fresh:
                    seen_urls.add(item_url)
                    print(f'\n   🎬 {item_url}')
                    if delay > 0:
                        time.sleep(delay)

                    try:
                        ok = self._process_item(scraper, item_url, media_type,
                                                forced_cats, no_social, allow_unver,
                                                update_only)
                    except Exception as e:
                        print(f'      💥 Error: {e}')
                        import traceback; traceback.print_exc()
                        connection.close()
                        ok = 'error'

                    total_seen += 1
                    if ok == 'created':
                        total_created += 1
                    elif ok == 'enriched':
                        total_enriched += 1
                    elif ok in ('skipped', 'error', 'skipped-no-match'):
                        total_skipped += 1

                page += 1

        print(f'\n\n{"=" * 60}')
        print('🎉  Scraping complete!')
        print(f'    Titles processed   : {total_seen}')
        print(f'    Enriched existing  : {total_enriched}')
        print(f'    Created stream-only: {total_created}')
        print(f'    Skipped/no-match   : {total_skipped}')
        print('=' * 60)

    # ──────────────────────────────────────────────────────────

    def _process_item(self, scraper, url, media_type,
                      forced_cats, no_social, allow_unver, update_only=False) -> str:
        resp = scraper.get(url, timeout=25)
        if resp.status_code != 200:
            print(f'      ⚠️ HTTP {resp.status_code} — skipping')
            return 'skipped'

        parsed = parse_page(resp.text, url)
        if not parsed:
            print('      ⚠️ Could not parse page — skipping')
            return 'skipped'

        print(f'      📝 {parsed["title_raw"]}  '
              f'({parsed.get("vi_year") or "?"}, {parsed["media_type"]})')

        info = resolve_stream(scraper, parsed['tmdb_id'], parsed['media_type'])
        if info:
            print(f'      🎞  Stream OK — {info["stream_count"]} source(s) | {info["file_name"][:60]}')
        else:
            if not allow_unver:
                print('      ⛔ No live stream — skipping (use --allow-unverified to keep)')
                return 'skipped'
            print('      ⚠️ No live stream — storing anyway (--allow-unverified)')

        embed_url = build_embed_url(parsed['media_type'], parsed['tmdb_id'])
        db_cats   = forced_cats if forced_cats is not None else infer_db_cats(parsed)

        _movie, status = save_item(parsed, embed_url, db_cats,
                                   no_social=no_social, update_only=update_only)
        print(f'      📋 {status} | embed: {embed_url}')
        return status
