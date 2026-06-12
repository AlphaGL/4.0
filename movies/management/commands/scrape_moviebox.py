"""
Management command: scrape_moviebox
===================================
Crawl moviebox.ph and upsert titles into the Movie DB, using moviebox's private
signed "wefeed" API (host: h5-api.aoneroom.com).

This site is NOT a simple HTML scrape — it's a Nuxt SPA backed by a signed BFF
API. The signing + id scheme were reverse-engineered from the JS bundle:

1. Request signing (anonymous):
     X-Client-Token : "<unix_ts>,<md5(reverse(str(unix_ts)))>"
     X-Client-Info  : {"timezone":"<IANA tz>"}
     X-Request-Lang : "en"
   The very first call also returns a guest JWT via the `x-user` response header
   / `token` cookie, which we send as `Authorization: Bearer <token>` afterwards.
   (Recovered from UA()/Fe() in the bundle: token = `${ts},${MD5(reverse(ts))}`.)

2. Slug ↔ subjectId:
     moviedetail URLs look like /moviedetail/<title-slug>-<code> (e.g.
     mortal-kombat-0ISdrp8hJl3). <code> is base62 (alphabet 0-9a-zA-Z) of the
     numeric subjectId, with the string REVERSED (little-endian).
       decode("0ISdrp8hJl3") -> 2812062564983656232

3. Metadata:
     GET /wefeed-h5api-bff/detail?subjectId=<num>  → title, releaseDate, duration,
     genre, countryName, description, cover, cast (stars), imdbRating, and a
     resource catalog (seasons / maxEp). subjectType: 1 = movie, 2 = series.

What we store in Movie.video_url
────────────────────────────────
The embeddable PLAYER URL:  https://netfilm.world/movies/<subjectId>
(netfilm.world is returned by the API's media-player/get-domain and is the web
player route /movies/:id). Reasons:
  • The /subject/play and /subject/download API endpoints return no stream/mp4
    URLs to server-side clients (hasResource:false for every title/param/region
    we tested, including from a Nigerian IP) — moviebox gates real media to its
    in-browser player. So there is no durable stream URL to store.
  • netfilm.world / moviebox.ph send NO X-Frame-Options / CSP, so the player
    page iframes cleanly through movie_detail.html's generic-iframe fallback and
    plays client-side in the visitor's browser (where the stream IS served).

  ⚠️  NOTE: server-side we can verify metadata + that the player page is
  embeddable, but we cannot browser-verify playback here. download_url is left
  empty (no extractable direct download).

Usage
─────
python manage.py scrape_moviebox
python manage.py scrape_moviebox --media movie
python manage.py scrape_moviebox --media tv
python manage.py scrape_moviebox --section /web/movie --section /web/tv-series
python manage.py scrape_moviebox --no-social
python manage.py scrape_moviebox --category hollywood
"""

import re
import time
import json
import hashlib

import urllib3
from django.core.management.base import BaseCommand
from django.db import IntegrityError

import cloudscraper
from bs4 import BeautifulSoup

from movies.models import Movie

# Re-use the generic DB + social helpers from the 9jarocks scraper.
from .scrape_9jarocks import (
    find_existing_movie,
    assign_db_categories,
    # _post_to_all_platforms,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ══════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════

API_BASE    = 'https://h5-api.aoneroom.com'
BFF         = '/wefeed-h5api-bff'
WEB_BASE    = 'https://moviebox.ph'
PLAYER_BASE = 'https://netfilm.world'   # fallback; refreshed from get-domain

# base62 alphabet (digits, lowercase, uppercase) — confirmed against
# 8WtKSYJVEb1 -> 997144265920760504
_B62 = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'

# /moviedetail/<slug>-<code>  and  /tvdetail/<slug>-<code>
_DETAIL_SLUG_RE = re.compile(r'/(?:movie|tv)detail/([A-Za-z0-9\-]+-[A-Za-z0-9]{6,14})')
# trailing base62 code on a slug
_CODE_RE = re.compile(r'([A-Za-z0-9]{6,14})$')

_COUNTRY_TO_DB = {
    'south korea': ['Korean drama'],
    'korea':       ['Korean drama'],
    'china':       ['Chinese drama'],
    'taiwan':      ['Chinese drama'],
    'hong kong':   ['Chinese drama'],
    'thailand':    ['Thai drama'],
    'india':       ['Bollywood movies'],
    'nigeria':     ['Nollywood movies'],
    'south africa':['Series'],
}

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
# ID ENCODING
# ══════════════════════════════════════════════════════════════

def decode_slug_code(code: str) -> int | None:
    """base62(reversed) code → numeric subjectId."""
    n = 0
    for ch in code[::-1]:
        i = _B62.find(ch)
        if i < 0:
            return None
        n = n * 62 + i
    return n


def encode_subject_id(n: int) -> str:
    """numeric subjectId → base62(reversed) code (inverse of decode)."""
    if n == 0:
        return '0'
    out = ''
    while n:
        out += _B62[n % 62]
        n //= 62
    return out


def subject_id_from(url_or_slug: str) -> int | None:
    """
    Accept a full moviedetail/tvdetail URL, a bare slug, a trailing code, or a
    raw numeric subjectId, and return the numeric subjectId.
    """
    s = (url_or_slug or '').strip()
    if s.isdigit():
        return int(s)
    m = _DETAIL_SLUG_RE.search(s)
    slug = m.group(1) if m else s.rsplit('/', 1)[-1]
    code = slug.rsplit('-', 1)[-1]
    cm = _CODE_RE.search(code)
    if not cm:
        return None
    return decode_slug_code(cm.group(1))


# ══════════════════════════════════════════════════════════════
# SIGNED API CLIENT
# ══════════════════════════════════════════════════════════════

class MovieboxClient:
    """Signed client for the moviebox/aoneroom wefeed API."""

    def __init__(self, timezone: str = 'Africa/Lagos'):
        self.tz = timezone
        self.s = cloudscraper.create_scraper()
        self.s.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
        })
        self._offset = 0          # server_ts - local_ts
        self.player_base = PLAYER_BASE
        self._bootstrap()

    @staticmethod
    def _md5(x: str) -> str:
        return hashlib.md5(x.encode()).hexdigest()

    def _client_token(self) -> str:
        ts = int(time.time()) + self._offset
        return f'{ts},{self._md5(str(ts)[::-1])}'

    def _headers(self) -> dict:
        h = {
            'Accept': 'application/json',
            'X-Client-Info': json.dumps({'timezone': self.tz}),
            'X-Request-Lang': 'en',
            'X-Client-Token': self._client_token(),
            'Referer': WEB_BASE + '/',
            'Origin': WEB_BASE,
        }
        tok = self.s.cookies.get('token')
        if tok:
            h['Authorization'] = f'Bearer {tok}'
        return h

    def _bootstrap(self):
        """First call: sync clock to server + obtain the guest JWT cookie."""
        try:
            r = self.s.get(API_BASE + BFF + '/tab/get-bottom-tab-list',
                           headers={'Accept': 'application/json',
                                    'X-Client-Info': json.dumps({'timezone': self.tz}),
                                    'X-Request-Lang': 'en',
                                    'Referer': WEB_BASE + '/', 'Origin': WEB_BASE},
                           timeout=20)
            from email.utils import parsedate_to_datetime
            d = r.headers.get('Date')
            if d:
                self._offset = int(parsedate_to_datetime(d).timestamp()) - int(time.time())
        except Exception as e:
            print(f'   ⚠️  bootstrap failed (continuing): {e}')
        # Refresh the player domain (best-effort)
        try:
            gd = self.get('/media-player/get-domain')
            dom = (gd or {}).get('data')
            if isinstance(dom, str) and dom.startswith('http'):
                self.player_base = dom.rstrip('/')
        except Exception:
            pass

    def get(self, path: str, params: dict | None = None) -> dict | None:
        url = API_BASE + BFF + path
        r = self.s.get(url, headers=self._headers(), params=params, timeout=25)
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except Exception:
            return None

    def detail(self, subject_id: int) -> dict | None:
        j = self.get('/detail', {'subjectId': str(subject_id)})
        if not j or j.get('code') not in (0, '0'):
            return None
        return j.get('data') or None

    def player_url(self, detail_path: str, subject_id: int, is_series: bool) -> str:
        """
        Build the embeddable web-player URL that actually streams (confirmed via
        a real browser capture). Shape:
          <base>/spa/videoPlayPage/movies/<detailPath>
                 ?id=<subjectId>&type=/<movie|tv>/detail&detailSe=&detailEp=&lang=en
        """
        if not detail_path:
            detail_path = f'movie-{encode_subject_id(subject_id)}'
        typ = '/tv/detail' if is_series else '/movie/detail'
        return (f'{self.player_base}/spa/videoPlayPage/movies/{detail_path}'
                f'?id={subject_id}&type={typ}&detailSe=&detailEp=&lang=en')

    def listing_slugs(self, section_path: str) -> list[str]:
        """Scrape a moviebox.ph SSR section page for detail slugs."""
        try:
            html = self.s.get(WEB_BASE + section_path, timeout=25).text
        except Exception as e:
            print(f'   ❌ Failed to fetch {section_path}: {e}')
            return []
        return list(dict.fromkeys(_DETAIL_SLUG_RE.findall(html)))


# ══════════════════════════════════════════════════════════════
# PARSING
# ══════════════════════════════════════════════════════════════

def _duration_to_runtime(seconds) -> str:
    try:
        sec = int(seconds)
    except (TypeError, ValueError):
        return ''
    if sec <= 0:
        return ''
    h, m = divmod(sec // 60, 60)
    if h and m:
        return f'{h}h {m}m'
    if h:
        return f'{h}h'
    return f'{m}m'


def parse_detail(data: dict) -> dict | None:
    """Turn a /detail `data` object into our normalized scrape dict."""
    subj = data.get('subject') or {}
    sid  = subj.get('subjectId')
    title = (subj.get('title') or '').strip()
    if not sid or not title:
        return None

    is_series = subj.get('subjectType') == 2
    detail_path = (subj.get('detailPath') or '').strip()

    year = ''
    ym = re.search(r'(\d{4})', str(subj.get('releaseDate') or ''))
    if ym:
        year = ym.group(1)

    genre = (subj.get('genre') or '').replace(',', ', ')
    cover = (subj.get('cover') or {}).get('url', '')

    stars = data.get('stars') or []
    cast_names = []
    for st in stars:
        name = st.get('name', '').strip() if isinstance(st, dict) else ''
        if name and name not in cast_names:
            cast_names.append(name)
    cast = ', '.join(cast_names)

    seasons = (data.get('resource') or {}).get('seasons') or []
    episodes = ''
    status = ''
    season_num = 1
    if is_series and seasons:
        max_ep = max((ss.get('maxEp') or 0) for ss in seasons)
        if max_ep:
            episodes = str(max_ep)
        ses = sorted(ss.get('se') for ss in seasons if (ss.get('se') or 0) > 0)
        if ses:
            season_num = ses[0]                       # first/lowest season number
            if len(ses) > 1:
                status = f'S{ses[0]}-S{ses[-1]}'

    return {
        'subject_id':  int(sid),
        'detail_path': detail_path,
        'is_series':   is_series,
        'season_num':  season_num,
        'title_raw':   title,
        'description': (subj.get('description') or '').strip(),
        'image_url':   cover,
        'vi_year':     year,
        'vi_genre':    genre,
        'vi_country':  subj.get('countryName') or '',
        'vi_cast':     cast,
        'vi_runtime':  _duration_to_runtime(subj.get('duration')),
        'vi_episodes': episodes,
        'vi_status':   status,
        'vi_language': '',
        'vi_subtitle': '',
        'vi_filesize': '',
    }


def infer_db_cats(parsed: dict) -> list[str]:
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
        cats = ['Animation'] if 'animation' in genre else ['Hollywood movies']
    if is_tv and 'Series' not in cats:
        cats.append('Series')
    return cats


# Dubbed / foreign-language variants moviebox ships, e.g.
# "Spider-Man Noir [Version française]", "... [Versão portuguesa]", "[Dublado]".
_FOREIGN_VARIANT_RE = re.compile(
    r'version\s+fran|fran[çc]ais|versione\s+ital|versi[oó]n\s+espa|vers[ãa]o|'
    r'dublad[oa]|legendad[oa]|doblad[oa]|sub\s*indo|espa[ñn]ol|deutsch|'
    r't[üu]rk[çc]e|عرب|हिन्दी|ا?لعربية',
    re.IGNORECASE,
)


def is_foreign_variant(raw: str) -> bool:
    """True for a non-English dub/version title (we skip these for an EN catalog)."""
    return bool(_FOREIGN_VARIANT_RE.search(raw or ''))


def clean_name(raw: str) -> str:
    """
    Strip moviebox's bracketed version/quality tags and trailing season ranges,
    leaving just the plain show/movie name.
      "Spider-Man Noir [Version française]" -> "Spider-Man Noir"
      "Naruto: Shippuden S1-S2"             -> "Naruto: Shippuden"
    """
    t = re.sub(r'\s+', ' ', raw or '').strip()
    t = re.sub(r'\s*\[[^\]]*\]\s*', ' ', t)                    # drop any [..] tag
    t = re.sub(r'\s*\([^)]*\b(?:dub|sub|version)\b[^)]*\)\s*', ' ', t, flags=re.I)
    t = re.sub(r'\s*\bS\d{1,2}\s*-\s*S\d{1,2}\b\s*$', '', t)   # trailing season range
    return re.sub(r'\s+', ' ', t).strip()


def build_title(parsed: dict) -> str:
    """
    Build the stored title in the SAME convention as the 9jarocks/nkiri scrapers
    (confirmed against the live DB):
      • Movie  -> "Name (Year)"      e.g. "Mortal Kombat (2021)"
      • Series -> "Name Season N"    e.g. "Teach You A Lesson Season 1"
    """
    name = clean_name(parsed['title_raw'])
    if parsed.get('is_series'):
        return f"{name} Season {parsed.get('season_num') or 1}"
    year = parsed.get('vi_year', '')
    return f"{name} ({year})" if year else name


# Back-compat alias (other modules import clean_title).
def clean_title(raw: str) -> str:
    return clean_name(raw)


def match_existing(parsed: dict):
    """
    Find an existing DB movie using the same title convention, trying a couple of
    forms so streaming enriches the right download record:
      • the convention title  ("Name (Year)" / "Name Season N")
      • the bare name         (in case the stored title omits year/season)
    find_existing_movie already covers season-spelling + "(Complete)" variants.
    """
    candidates = [build_title(parsed), clean_name(parsed['title_raw'])]
    seen = set()
    for cand in candidates:
        if cand and cand not in seen:
            seen.add(cand)
            movie = find_existing_movie(cand)
            if movie:
                return movie
    return None


def resolve_category_arg(cat_arg: str) -> list[str] | None:
    if not cat_arg:
        return None
    key = cat_arg.strip().lower()
    if key in _CATEGORY_ALIASES:
        return list(_CATEGORY_ALIASES[key])
    return [cat_arg.strip()]


# ══════════════════════════════════════════════════════════════
# UPSERT  (shared by the crawler and the --url/--file variant)
# ══════════════════════════════════════════════════════════════

def save_item(parsed: dict, stream_url: str, db_cats: list[str],
              no_social: bool = False, update_only: bool = False) -> tuple[Movie | None, str]:
    """
    Add streaming to a movie.

    • EXISTING movie (matched by title): ENRICH it — set stream_url and backfill
      only empty metadata fields. Never touches its download links, download_url,
      categories, title, or is_series. This is the "scrape downloads first, then
      add streaming to the same movie" flow.
    • NO match: create a new stream-only Movie (unless update_only=True, in which
      case skip — we only enrich existing download titles).

    Returns (movie | None, status) where status is one of:
      'enriched' | 'created' | 'unchanged' | 'skipped-no-match'.
    """
    title = build_title(parsed)

    vi_fields = dict(
        vi_year     = parsed.get('vi_year', '')[:10],
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

    movie = match_existing(parsed)

    # ── ENRICH an existing (download) movie ────────────────────────────────
    if movie:
        updated = False
        if movie.stream_url != stream_url:
            movie.stream_url = stream_url[:600]
            updated = True
        # Backfill ONLY empty metadata — never overwrite the download scraper's
        # data, categories, title, is_series, or download links.
        if not movie.image_url and parsed['image_url']:
            movie.image_url = parsed['image_url'][:500]
            updated = True
        if not movie.description and parsed['description']:
            movie.description = parsed['description']
            updated = True
        for field, value in vi_fields.items():
            if value and not getattr(movie, field, ''):
                setattr(movie, field, value)
                updated = True
        if updated:
            movie.save()
        return movie, ('enriched' if updated else 'unchanged')

    # ── No match: create a stream-only title (unless update_only) ──────────
    if update_only:
        return None, 'skipped-no-match'

    for candidate in [
        title[:200],
        f"{title} [{parsed['subject_id']}]",   # last-resort uniqueness
    ]:
        if not candidate:
            continue
        try:
            movie = Movie.objects.create(
                title       = candidate[:200],
                description = parsed['description'],
                video_url   = '',                       # not a trailer
                stream_url  = stream_url[:600],
                image_url   = (parsed['image_url'] or '')[:500],
                is_series   = parsed['is_series'],
                scraped     = True,
                **vi_fields,
            )
            break
        except IntegrityError:
            continue
    if not movie:
        raise IntegrityError(f"Could not create a unique title for '{title}'")

    # Only stream-only NEW movies get categories assigned (inferred).
    assign_db_categories(movie, scraped_cats=[], forced_db_cats=db_cats)
    print(f"      ✅ Created (stream-only): {movie.title}")
    if not no_social:
        _post_to_all_platforms(movie, is_new=True)
    return movie, 'created'


# ══════════════════════════════════════════════════════════════
# MANAGEMENT COMMAND
# ══════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = (
        'Scrape moviebox.ph via its signed wefeed API and upsert titles into the '
        'DB (rich metadata; embeddable netfilm.world player URL in video_url).'
    )

    def add_arguments(self, parser):
        parser.add_argument('--media', choices=['movie', 'tv', 'both'], default='both',
                            help='Filter saved titles by type (default: both).')
        parser.add_argument('--section', action='append', dest='sections', default=[],
                            help='moviebox.ph SSR section path(s) to harvest slugs from. '
                                 'Repeatable. Default: / , /web/movie , /web/tv-series.')
        parser.add_argument('--category', type=str, default=None,
                            help='Force a DB category (alias or raw name).')
        parser.add_argument('--no-social', action='store_true', default=False,
                            help='Save to DB only — skip all social posts.')
        parser.add_argument('--update-only', action='store_true', default=False,
                            help='Only ADD streaming to movies that already exist (from your '
                                 'download scrapers). Skip titles with no match instead of '
                                 'creating stream-only entries.')
        parser.add_argument('--include-dubs', action='store_true', default=False,
                            help='Include non-English dub/version titles (e.g. "[Version '
                                 'française]"). Default: skip them.')
        parser.add_argument('--limit', type=int, default=None,
                            help='Stop after processing this many titles.')
        parser.add_argument('--delay', type=float, default=0.3,
                            help='Seconds between detail requests (default: 0.3).')

    def handle(self, *args, **options):
        from django.db import connection

        media       = options['media']
        no_social   = options['no_social']
        update_only = options['update_only']
        include_dubs = options['include_dubs']
        limit       = options['limit']
        delay       = options['delay']
        forced_cats = resolve_category_arg(options['category']) if options['category'] else None
        sections    = options['sections'] or ['/', '/web/movie', '/web/tv-series']

        print('=' * 60)
        print('🚀  moviebox.ph scraper starting (signed wefeed API)')
        print(f'    Sections : {", ".join(sections)}')
        print(f'    Media    : {media}')
        print(f'    Mode     : {"ENRICH existing only" if update_only else "enrich + create stream-only"}')
        print(f'    Category : {", ".join(forced_cats) if forced_cats else "(auto-infer)"}')
        print(f'    Social   : {"DISABLED" if no_social else "ON (Telegram + Facebook)"}')
        print('=' * 60)

        client = MovieboxClient()
        print(f'    Player   : {client.player_base}/movies/<id>')

        # 1) Harvest unique subjectIds from the SSR section pages
        subject_ids: list[int] = []
        seen: set[int] = set()
        for sec in sections:
            slugs = client.listing_slugs(sec)
            sids  = []
            for slug in slugs:
                sid = subject_id_from(slug)
                if sid and sid not in seen:
                    seen.add(sid)
                    subject_ids.append(sid)
                    sids.append(sid)
            print(f'   📄 {sec}: {len(slugs)} slugs → {len(sids)} new subjectIds')

        print(f'\n   🎯 {len(subject_ids)} unique titles to process')

        total_created = total_enriched = total_unchanged = total_skipped = 0
        processed = 0

        for sid in subject_ids:
            if limit and processed >= limit:
                print(f'\n✅ Reached --limit {limit}.')
                break
            processed += 1
            if delay > 0:
                time.sleep(delay)

            try:
                data = client.detail(sid)
                if not data:
                    print(f'   ⚠️ {sid}: no detail — skipping')
                    total_skipped += 1
                    continue
                parsed = parse_detail(data)
                if not parsed:
                    print(f'   ⚠️ {sid}: unparseable — skipping')
                    total_skipped += 1
                    continue

                if media == 'movie' and parsed['is_series']:
                    continue
                if media == 'tv' and not parsed['is_series']:
                    continue

                if not include_dubs and is_foreign_variant(parsed['title_raw']):
                    print(f"   ⏭  {parsed['title_raw'][:45]} — foreign-language dub, skipping")
                    total_skipped += 1
                    continue

                kind = 'TV' if parsed['is_series'] else 'MOVIE'
                print(f"\n   🎬 [{kind}] {parsed['title_raw'][:45]}  "
                      f"({parsed.get('vi_year') or '?'}, {parsed.get('vi_country') or '?'})")

                stream_url = client.player_url(parsed['detail_path'], sid, parsed['is_series'])
                db_cats    = forced_cats if forced_cats is not None else infer_db_cats(parsed)
                _movie, status = save_item(parsed, stream_url, db_cats,
                                           no_social=no_social, update_only=update_only)
                print(f'      📋 {status} | {stream_url}')

                if status == 'created':
                    total_created += 1
                elif status == 'enriched':
                    total_enriched += 1
                elif status == 'unchanged':
                    total_unchanged += 1
                else:  # skipped-no-match
                    total_skipped += 1
            except Exception as e:
                print(f'   💥 {sid}: error: {e}')
                import traceback; traceback.print_exc()
                connection.close()
                total_skipped += 1

        print(f'\n\n{"=" * 60}')
        print('🎉  moviebox scrape complete!')
        print(f'    Processed         : {processed}')
        print(f'    Enriched existing : {total_enriched}')
        print(f'    Created stream-only: {total_created}')
        print(f'    Unchanged         : {total_unchanged}')
        print(f'    Skipped/no-match  : {total_skipped}')
        print('=' * 60)
