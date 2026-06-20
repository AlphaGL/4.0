"""
scrape_seriezloaded_wp.py
=========================
Django management command that scrapes www.seriezloaded.com.ng (WordPress /
MH Magazine Lite theme + Jannah-style content) by crawling category listing
pages, then visits each post to extract title, image, description, video info,
and download links — and publishes DIRECTLY to your own WordPress site.
Zero DB interaction, zero social posting.

Source site : https://www.seriezloaded.com.ng
Target site : Your WordPress site  (WP_SITE_URL / WP_APP_PASSWORD in Django settings)

Usage:
    python manage.py scrape_seriezloaded_wp
    python manage.py scrape_seriezloaded_wp --category hollywood
    python manage.py scrape_seriezloaded_wp --category kdrama --startpage 3
    python manage.py scrape_seriezloaded_wp --startpage 1 --endpage 5
    python manage.py scrape_seriezloaded_wp --category all --max-pages 10

    # Scrape individual post URLs from a text file (one URL per line):
    python manage.py scrape_seriezloaded_wp --urls-file links.txt
    python manage.py scrape_seriezloaded_wp --urls-file links.txt --delay 1.0

WORDPRESS ONE-TIME SETUP (required for smart dedup / update):
    Add this to your theme's functions.php so the script can store and query
    the original SeriezLoaded source URL on each post:

        add_action('init', function() {
            register_post_meta('post', '_seriezloaded_source_url', [
                'show_in_rest' => true,
                'single'       => true,
                'type'         => 'string',
                'auth_callback' => '__return_true',
            ]);
        });

    How it works:
        - MOVIES  : if the source URL already exists on your WP site → SKIP
        - SERIES  : if source URL already exists → UPDATE (new episode count,
                    fresh download links, slug/URL never changes)
        - NEW     : if not found → CREATE a new published post

Available --category aliases:
    hollywood, nollywood, nollywood_series, hollywood_series,
    kdrama, chinese_drama, thai_drama, anime, all  (default: all)

Place this file at:
    <your_app>/management/commands/scrape_seriezloaded_wp.py
"""

from django.core.management.base import BaseCommand
import requests
from bs4 import BeautifulSoup
import re
import cloudscraper
from urllib.parse import urlparse, unquote
import urllib3
import time
import base64

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ══════════════════════════════════════════════════════════════
# SITE / CATEGORY CONSTANTS  —  seriezloaded.com.ng
# ══════════════════════════════════════════════════════════════

SITE_URL = 'https://www.seriezloaded.com.ng'

# SeriezLoaded uses WordPress category URLs:
#   Movies : /movies/<sub-category>/
#   Series : /series/<sub-category>/
# The REST API (document index 1) confirms the available categories.
CATEGORY_DEFINITIONS = [
    {
        'key':       'hollywood',
        'slug':      'movies/holly-wood-movies',
        'label':     'Hollywood Movie',
        'wp_cat':    'Hollywood movie',
        'is_series': False,
    },
    {
        'key':       'nollywood',
        'slug':      'movies/nollywood-movies',
        'label':     'Nollywood Movie',
        'wp_cat':    'Nollywood movie',
        'is_series': False,
    },
    {
        'key':       'india_movies',
        'slug':      'movies/india-movies',
        'label':     'Indian Movie',
        'wp_cat':    'Indian movie',
        'is_series': False,
    },
    {
        'key':       'chinese_movies',
        'slug':      'movies/chinese-movies',
        'label':     'Chinese Movie',
        'wp_cat':    'Chinese movie',
        'is_series': False,
    },
    {
        'key':       'hollywood_series',
        'slug':      'series/hollywood-tv-series',
        'label':     'Hollywood TV Series',
        'wp_cat':    'Hollywood Series',
        'is_series': True,
    },
    {
        'key':       'nollywood_series',
        'slug':      'series/nollywood-series',
        'label':     'Nollywood TV Series',
        'wp_cat':    'Nollywood tv series',
        'is_series': True,
    },
    {
        'key':       'kdrama',
        'slug':      'series/korean-series',
        'label':     'Korean Drama',
        'wp_cat':    'Korean Drama',
        'is_series': True,
    },
    {
        'key':       'chinese_drama',
        'slug':      'series/chinese-drama',
        'label':     'Chinese Drama',
        'wp_cat':    'Chinese drama',
        'is_series': True,
    },
    {
        'key':       'thai_drama',
        'slug':      'series/thai-series',
        'label':     'Thai Drama',
        'wp_cat':    'Thai drama',
        'is_series': True,
    },
    {
        'key':       'sa_series',
        'slug':      'series/sa-series',
        'label':     'South African Series',
        'wp_cat':    'South African Series',
        'is_series': True,
    },
    {
        'key':       'anime',
        'slug':      'series/anime-series',
        'label':     'Anime',
        'wp_cat':    'Anime',
        'is_series': True,
    },
    {
        'key':       'trending',
        'slug':      'trending',
        'label':     'Trending',
        'wp_cat':    'Entertainment',
        'is_series': False,   # mixed; we detect per-post
    },
]

CATEGORY_ALIASES = {
    'hollywood':        ['hollywood'],
    'nollywood':        ['nollywood'],
    'nollywood_series': ['nollywood_series'],
    'hollywood_series': ['hollywood_series'],
    'series':           ['hollywood_series', 'nollywood_series'],
    'kdrama':           ['kdrama'],
    'korean':           ['kdrama'],
    'chinese':          ['chinese_drama'],
    'cdrama':           ['chinese_drama'],
    'chinese_drama':    ['chinese_drama'],
    'thai':             ['thai_drama'],
    'thai_drama':       ['thai_drama'],
    'india':            ['india_movies'],
    'indian':           ['india_movies'],
    'bollywood':        ['india_movies'],
    'anime':            ['anime'],
    'sa':               ['sa_series'],
    'trending':         ['trending'],
    'all':              [d['key'] for d in CATEGORY_DEFINITIONS],
}

_SLUG_TO_DEF = {d['slug']: d for d in CATEGORY_DEFINITIONS}
_KEY_TO_DEF  = {d['key']:  d for d in CATEGORY_DEFINITIONS}


# ── Known download domains (same philosophy as 9jarocks scraper) ──────────────
KNOWN_DOWNLOAD_DOMAINS = [
    'mega.nz', 'drive.google.com', 'mediafire.com', 'pixeldrain.com',
    'terabox.com', 'gofile.io', 'mixdrop.co', 'streamtape.com',
    'doodstream.com', 'filemoon.sx', 'loadedfiles.org', 'webloaded.com.ng',
    'wideshares.org', 'downloadwella.com', 'netnaija.com', 'fzmovies.net',
    'o2tvseries.com', 'yts.mx', 'yts.am', 'sabishares.com',
    'meetdownload.com', 'sojuoppa.com', 'dramabus.tv', 'archive.org',
    'onedrive.live.com', 'dl.', 'nkirifiles.com',
    # SeriezLoaded uses its own redirect wrapper — these are the real hosts
    # after unwrapping slnig.link / seriezloaded download endpoints:
    'slnig.link', 'seriezloaded.com.ng/sl-download',
]

FILE_EXTENSIONS = ['.mp4', '.mkv', '.avi', '.zip', '.rar', '.srt']

DOWNLOAD_KEYWORDS = [
    'download', '480p', '540p', '720p', '1080p', '4k',
    'hd', 'episode', 'fast server', 'slow server', 'mirror', 'batch',
    'download here', 'download video server',
]

AD_DOMAINS = [
    'googletagmanager.com', 'cloudflareinsights.com',
    'musetteanstoss.com',   # pop-under ad network seen on SeriezLoaded
    'ftd.agency',
]

# In-memory WP category cache  name → ID
_wp_category_cache: dict = {}


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

_RESOLVE_CACHE: dict = {}   # wrapper URL → resolved direct URL (avoid re-fetching same link)


def _resolve_sl_download_link(wrapper_url: str, scraper) -> str:
    """
    SeriezLoaded never shows the real file URL on the post page itself — every
    download button points at an internal wrapper:

        https://www.seriezloaded.com.ng/sl-download/?link=MzQyNzc4LDA=

    That wrapper page is plain HTML with a JS countdown that, after a few
    seconds, does:

        window.location.href = "https://area.waffi.cloud/d/8/<file>.mkv?preview"

    That target is itself NOT the file — it's a WaffiCloud (OneManager) HTML
    preview/landing page. Stripping the trailing "?preview" query string from
    that URL gives the actual direct file URL that starts downloading
    immediately in a browser.

    This function:
      1. Fetches the wrapper page HTML.
      2. Regex-extracts the `window.location.href = "..."` JS-redirect target.
      3. Strips any query string (?preview etc.) from that target.
      4. Returns the resulting direct download URL — or the original
         wrapper_url unchanged if anything fails (so the scraper still has
         *something* to publish rather than nothing).
    """
    if not wrapper_url:
        return wrapper_url

    if wrapper_url in _RESOLVE_CACHE:
        return _RESOLVE_CACHE[wrapper_url]

    try:
        resp = scraper.get(wrapper_url, timeout=20)
        if resp.status_code != 200:
            print(f"      ⚠️ Resolve failed (HTTP {resp.status_code}) — keeping wrapper URL")
            _RESOLVE_CACHE[wrapper_url] = wrapper_url
            return wrapper_url
        html = resp.text
    except Exception as e:
        print(f"      ⚠️ Resolve fetch error: {e} — keeping wrapper URL")
        _RESOLVE_CACHE[wrapper_url] = wrapper_url
        return wrapper_url

    # Extract the JS redirect target:  window.location.href = "...";
    m = re.search(
        r'window\.location\.href\s*=\s*["\']([^"\']+)["\']',
        html
    )
    if not m:
        print("      ⚠️ No JS redirect found on wrapper page — keeping wrapper URL")
        _RESOLVE_CACHE[wrapper_url] = wrapper_url
        return wrapper_url

    target = m.group(1).strip()
    # The JS string may contain HTML-encoded ampersands etc.
    target = target.replace('&amp;', '&')

    # Strip the query string (?preview and anything else) to get the
    # actual direct-download file URL.
    parsed = urlparse(target)
    direct_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    print(f"      ✅ Resolved → {direct_url}")
    _RESOLVE_CACHE[wrapper_url] = direct_url
    return direct_url


def _make_scraper():
    """Return a cloudscraper session with browser-like headers."""
    scraper = cloudscraper.create_scraper()
    scraper.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         SITE_URL,
    })
    return scraper


def _normalise_sl_url(href: str) -> str:
    """Ensure the URL always has the www. prefix (canonical form for SeriezLoaded)."""
    href = re.sub(r'^(https?://)seriezloaded\.com\.ng', r'\g<1>www.seriezloaded.com.ng',
                  href, count=1)
    return href


def _is_post_url(href: str) -> bool:
    """
    Valid SeriezLoaded post URLs look like:
      https://www.seriezloaded.com.ng/<post-slug>/
    They must NOT be category/tag/page/admin/feed URLs.
    """
    href = _normalise_sl_url(href)
    if not href.startswith(SITE_URL):
        return False
    path = href[len(SITE_URL):]
    if not path or path in ('/', ''):
        return False

    skip = (
        '/movies/', '/series/', '/tag/', '/category/', '/page/',
        '/wp-', '/feed', '/author/', '/search/', '/sitemap',
        '/contact', '/privacy', '/dmca', '/advertise', '/promote',
        '/trending/', '/trending',
        '?', '#', '.xml', '.php', '/music/', '/album/',
        '/sl-download',   # download redirect endpoint (not a post)
    )
    if any(s in path for s in skip):
        return False

    # Must be a single path segment like /post-slug/ or /post-slug
    segments = [s for s in path.strip('/').split('/') if s]
    if len(segments) != 1:
        return False

    return True


# ══════════════════════════════════════════════════════════════
# LISTING PAGE / PAGINATION HELPERS
# ══════════════════════════════════════════════════════════════

def get_post_urls_from_listing_page(html: str, base_url: str) -> list:
    """Extract individual post URLs from a SeriezLoaded category listing page."""
    soup  = BeautifulSoup(html, 'html.parser')
    links = set()

    # Primary: <article> tags — MH Magazine Lite theme wraps each post card in <article>
    for article in soup.find_all('article'):
        for a in article.find_all('a', href=True):
            href = _normalise_sl_url(a['href'].strip().rstrip('/'))
            if _is_post_url(href):
                links.add(href)

    # Fallback: all anchors with post-style URLs
    if not links:
        for a in soup.find_all('a', href=True):
            href = _normalise_sl_url(a['href'].strip())
            if _is_post_url(href):
                links.add(href)

    return list(links)


def has_next_page(html: str) -> bool:
    """SeriezLoaded uses standard WP pagination (page-numbers next link)."""
    soup = BeautifulSoup(html, 'html.parser')
    for a in soup.find_all('a', href=True):
        text = a.get_text(strip=True).lower()
        cls  = ' '.join(a.get('class', []))
        if (
            text in ('next', '»', '›', 'next page') or
            'next' in cls or
            'nextpostslink' in cls or
            'page-numbers next' in cls or
            'next page-numbers' in cls
        ):
            return True
    return False


# ══════════════════════════════════════════════════════════════
# POST PAGE PARSER  —  seriezloaded.com.ng
# ══════════════════════════════════════════════════════════════

def parse_post_page(html: str, url: str) -> dict | None:
    """
    Parse a single SeriezLoaded.com.ng post page.

    SeriezLoaded post structure (from the Michael (2026) post):
      - og:title / og:description / og:image  meta tags
      - <h1 class="entry-title new-entry-tt">
      - <div class="entry-content mh-clearfix">
        - Notice! paragraph (skip)
        - Poster <img>
        - Synopsis <p>
        - <u><strong>VIDEO INFORMATION</strong></u>
        - <blockquote> with 🎬 Title, 📅 Year, 🎭 Genre, ⏳ Duration,
                          📺 Type, 🏳️ Country, ⭐ Stars, 🗣 Language,
                          📄 Subtitle, 📁 Source, 🌟 IMDB
        - <strong><u>📹 Trailer</u></strong> + <iframe>
        - <u><strong>DOWNLOAD LINKS</strong></u>
        - <a class="btn-ghost" href="...">DOWNLOAD VIDEO SERVER 1</a>

    Returns dict or None if unparseable.
    """
    soup = BeautifulSoup(html, 'html.parser')

    body_text = soup.get_text(' ', strip=True)
    if len(body_text) < 200:
        return None

    # ── Title ────────────────────────────────────────────────────
    title_raw = ''
    og_title  = soup.find('meta', property='og:title')
    if og_title:
        title_raw = og_title.get('content', '').strip()
        # Strip site suffix: "Download X Free With English Subtitles - SeriezLoaded NG"
        title_raw = re.sub(
            r'\s*[-–|]\s*(SeriezLoaded.*|Download.*Subtitles.*)$',
            '', title_raw, flags=re.IGNORECASE
        ).strip()
        title_raw = re.sub(r'^Download\s+', '', title_raw, flags=re.IGNORECASE).strip()
        title_raw = re.sub(r'\s+Free With English Subtitles.*$', '', title_raw,
                           flags=re.IGNORECASE).strip()

    if not title_raw or len(title_raw) < 4:
        h1 = (
            soup.find('h1', class_='entry-title') or
            soup.find('h1', class_=re.compile(r'entry-title', re.I)) or
            soup.find('h1')
        )
        if h1:
            title_raw = h1.get_text(strip=True)
            # Strip flag emojis and category labels that SeriezLoaded prepends
            title_raw = re.sub(r'^[\U0001F1E0-\U0001F1FF\U0001F004-\U0001FFFF\s]+', '',
                                title_raw).strip()

    if not title_raw or len(title_raw) < 4:
        title_tag = soup.find('title')
        if title_tag:
            title_raw = re.sub(
                r'\s*[-–|]\s*(SeriezLoaded.*|Free With English.*)$',
                '', title_tag.get_text(strip=True), flags=re.IGNORECASE
            ).strip()

    if not title_raw or len(title_raw) < 4:
        return None

    # ── Categories — extracted from breadcrumb and og:article:section ───
    categories = []
    _seen_cats = set()

    _SKIP_CAT_NAMES = {'uncategorized', 'video', 'all', 'home', 'latest', 'recent', 'trending'}

    def _add_cat(name: str):
        name = name.strip()
        key  = name.lower()
        if not name or key in _SKIP_CAT_NAMES or key in _seen_cats:
            return
        _seen_cats.add(key)
        categories.append(name)

    # og:article:section meta tags
    for meta_tag in soup.find_all('meta', property='article:section'):
        val = meta_tag.get('content', '').strip()
        # SeriezLoaded stores flag emojis in article:section — skip those
        if val and not re.match(r'^[\U0001F1E0-\U0001F9FF]+$', val):
            _add_cat(val)

    # Fallback: breadcrumb links (SeriezLoaded breadcrumb: Home » Movies » 🇨🇦 » Title)
    if not categories:
        breadcrumb = soup.find('p', id='breadcrumbs') or soup.find(class_='custom-breadcrumb')
        if breadcrumb:
            for a in breadcrumb.find_all('a', href=True):
                href_lower = a['href'].lower()
                text = a.get_text(strip=True)
                if '/movies/' in href_lower or '/series/' in href_lower:
                    # Skip flag-only links, keep category name links
                    if text and not re.match(r'^[\U0001F1E0-\U0001F9FF\U0001F004-\U0001FFFF]+$', text):
                        _add_cat(text)

    # ── Content div ──────────────────────────────────────────────
    content_div = (
        soup.find('div', class_='entry-content') or
        soup.find('div', class_='entry') or
        soup.find('div', class_='post-content') or
        soup.find('article') or
        soup.find('body')
    )

    # ── Image ────────────────────────────────────────────────────
    image_url = ''
    og_img = soup.find('meta', property='og:image')
    if og_img:
        image_url = og_img.get('content', '').strip()
    if not image_url and content_div:
        for img in content_div.find_all('img'):
            src = (img.get('src') or img.get('data-src') or
                   img.get('data-lazy-src') or '').strip()
            if not src or src.endswith('.gif'):
                continue
            try:
                w = img.get('width', '0')
                if int(str(w).replace('px', '')) < 80:
                    continue
            except (ValueError, TypeError):
                pass
            alt = (img.get('alt') or '').lower()
            if 'screenshot' in alt or 'thumb' in alt or 'ad' in alt:
                continue
            image_url = src
            break

    # ── Video / Trailer ──────────────────────────────────────────
    video_url = ''
    yt_domains = ['youtube.com/embed', 'youtu.be', 'youtube-nocookie.com']

    for _iframe in soup.find_all('iframe'):
        _src = (_iframe.get('src') or _iframe.get('data-src') or '').strip()
        if _src and any(d in _src for d in yt_domains):
            video_url = _src
            break

    if not video_url:
        _yt_re = re.search(
            r'(?:src|data-src)=["\']([^"\']*(?:youtube\.com/embed|youtu\.be)[^"\']*)["\']',
            html
        )
        if _yt_re:
            video_url = _yt_re.group(1).strip()

    # ── Description / Synopsis ───────────────────────────────────
    description = ''

    og_desc = soup.find('meta', property='og:description')
    if og_desc:
        description = og_desc.get('content', '').strip()
        # SeriezLoaded og:description sometimes starts with "VIDEO INFORMATION" — cut it
        _vi_cut = re.split(r'VIDEO\s+INFORMATION', description, maxsplit=1, flags=re.IGNORECASE)
        description = _vi_cut[0].strip()

    if not description and content_div:
        for p in content_div.find_all('p'):
            text = p.get_text(strip=True)
            if not text or len(text) < 40:
                continue
            # Skip the "Notice!" warning line
            if text.lower().startswith('notice!'):
                continue
            if re.search(r'https?://', text):
                continue
            if re.match(
                r'^(mp4|mkv|download|filename|filesize|duration|imdb|title|year|type|'
                r'country|language|director|genre|stars|subtitle|video\s+information|'
                r'trailer|download\s+links|screenshot|notice)',
                text, re.IGNORECASE
            ):
                continue
            description = text[:800]
            break

    if description and len(description) > 800:
        description = description[:800].rsplit(' ', 1)[0] + '...'

    # ── Metadata from VIDEO INFORMATION blockquote ───────────────
    # SeriezLoaded stores video info inside a <blockquote> using emoji bullets:
    #   🎬 Title: Michael
    #   📅 Year: 2026
    #   🎭 Genre: Biography, Drama, History, Music
    #   ⏳ Duration: 2h 7m
    #   📺 Type: Movie
    #   🏳️ Country: United States, Canada
    #   ⭐ Stars: Jaafar Jackson, Nia Long, ...
    #   🗣 Language: English
    #   📄 Subtitle Language: English
    #   📁 Source: Michael.2026.1080p...
    #   🌟 IMDB: https://www.imdb.com/title/tt11378946
    meta = {}
    if content_div:
        bq = content_div.find('blockquote')
        if bq:
            bq_text = bq.get_text('\n')
            for line in bq_text.splitlines():
                line = line.strip()
                # Strip leading emoji characters
                line = re.sub(r'^[\U0001F000-\U0001FFFF\u2600-\u27FF\uFE0F\s]+', '', line).strip()
                if ':' in line:
                    key, _, val = line.partition(':')
                    k = key.strip().lower().rstrip(':').strip()
                    v = val.strip()
                    if k == 'imdb' and not v:
                        imdb_a = bq.find('a', href=re.compile(r'imdb\.com', re.I))
                        if imdb_a:
                            v = imdb_a['href'].strip()
                    if k and v and len(k) < 50:
                        meta[k] = v

        # Fallback: table rows
        if not meta:
            table = content_div.find('table')
            if table:
                for row in table.find_all('tr'):
                    cells = row.find_all(['td', 'th'])
                    if len(cells) >= 2:
                        k = cells[0].get_text(strip=True).lower().rstrip(':')
                        v = cells[1].get_text(strip=True)
                        if k and v:
                            meta[k] = v

    # Capture IMDB link separately in case blockquote parse missed it
    if 'imdb' not in meta and content_div:
        bq = content_div.find('blockquote')
        if bq:
            imdb_a = bq.find('a', href=re.compile(r'imdb\.com', re.I))
            if imdb_a:
                meta['imdb'] = imdb_a['href'].strip()

    # ── Download links ───────────────────────────────────────────
    # SeriezLoaded uses:
    #   <a class="btn-ghost" href="/sl-download?link=...">DOWNLOAD VIDEO SERVER 1</a>
    #   or direct external links via btn-ghost / button / download classes

    _SKIP_BTN = {
        "can't download?", "cant download?", "how to download",
        "how to download?", "report broken link", "report link",
        "request movie", "subscribe", "follow us", "join us",
        "leave a comment", "share", "recommended", "notify me",
        "click here", "click to see what's airing today & this week→",
        "learn how to download", "report here!",
    }
    _SKIP_HREF_FRAGS = [
        'how-to-download', '/faq', '/help', 'report-broken',
        'request-movie', 'cant-download', 'episodes-calendar',
        'dramarain-ad', '#respond', 'mailto:', 'javascript',
        '/contact', '/privacy', '/dmca', '/advertise',
        'slnig.link/telegram', 'slnig.link/dramarain',
    ]

    download_links = []
    seen_urls      = set()

    if content_div:
        for a in content_div.find_all('a', href=True):
            href     = a.get('href', '').strip()
            btn_text = a.get_text(strip=True) or 'Download'
            href_lower = href.lower()
            btn_lower  = btn_text.lower().strip().rstrip('?')

            if not href or href.startswith('#') or 'javascript' in href_lower:
                continue
            if btn_lower in _SKIP_BTN:
                continue
            if any(frag in href_lower for frag in _SKIP_HREF_FRAGS):
                continue
            if any(ad in href_lower for ad in AD_DOMAINS):
                continue
            # Skip social / tracking links
            if any(skip in href_lower for skip in [
                'facebook.com', 'twitter.com', 't.me/', 'youtube.com/watch?',
                'imdb.com', 'wp-admin', 'tiktok.com', 'x.com/', 'telegram.org',
                'googletagmanager', 'cloudflare', 'instagram.com',
            ]):
                continue

            a_classes = ' '.join(a.get('class', []))

            # SeriezLoaded download buttons:
            #   class="btn-ghost"    → primary download button
            #   class="button"       → also used for downloads
            #   class="download"     → legacy
            is_sl_dl_btn = any(c in a_classes for c in (
                'btn-ghost', 'button', 'download', 'buttondl', 'se-button',
            ))

            # /sl-download?link=... is SeriezLoaded's internal download redirect
            is_sl_redirect = '/sl-download' in href_lower

            is_dl = is_sl_dl_btn or is_sl_redirect or (
                any(d in href_lower for d in KNOWN_DOWNLOAD_DOMAINS)
                or any(href_lower.endswith(ext) for ext in FILE_EXTENSIONS)
                or any(kw in btn_lower for kw in DOWNLOAD_KEYWORDS)
                or any(kw in href_lower for kw in ['/dl/', '/get/', '/file/', 'mirror'])
            )

            if is_dl and href not in seen_urls:
                seen_urls.add(href)

                # Episode label detection from button text
                # SeriezLoaded pattern: "DOWNLOAD EPISODE 1", "DOWNLOAD VIDEO SERVER 1"
                ep_label = ''
                ep_match = re.search(r'episode\s*(\d+)', btn_text, re.IGNORECASE)
                if ep_match:
                    ep_label = f'E{int(ep_match.group(1)):02d}'

                # Fallback: look in URL for SxxExx
                if not ep_label:
                    _se = re.search(r'[Ss](\d+)[Ee](\d+)', href)
                    if _se:
                        sn, en = int(_se.group(1)), int(_se.group(2))
                        ep_label = f'S{sn:02d}E{en:02d}' if sn > 1 else f'E{en:02d}'

                label = btn_text.strip() or 'DOWNLOAD'
                download_links.append({
                    'url':      href,
                    'label':    label,
                    'ep_label': ep_label,
                })
                print(f"   🔗 [{ep_label or 'FILE'}] {label} → {href}")

    # ── Series / complete detection ──────────────────────────────
    is_series = bool(re.search(
        r'\bS\d{1,2}\b|\bSeason\s?\d{1,2}\b|\bEpisode\b|\bEp\.?\s?\d+\b|Series\b',
        title_raw, re.IGNORECASE
    ))
    is_complete = bool(re.search(r'\bcomplete(d)?\b', title_raw, re.IGNORECASE))

    # ── Extract vi_ metadata fields ──────────────────────────────
    def _mv(keys):
        for k in keys:
            v = meta.get(k, '').strip()
            if v:
                return v
        return ''

    vi = {
        'vi_year':     _mv(['year', 'release year', 'release date']),
        'vi_country':  _mv(['country', 'country of origin']),
        'vi_language': _mv(['language', 'audio']),
        'vi_subtitle': _mv(['subtitle language', 'subtitle', 'subtitles', 'sub']),
        'vi_genre':    _mv(['genre', 'genres', 'category']),
        'vi_cast':     _mv(['stars', 'cast', 'actors', 'starring']),
        'vi_director': _mv(['director', 'directed by']),
        'vi_episodes': _mv(['episodes', 'episode', 'total episodes']),
        'vi_status':   _mv(['status', 'series status']),
        'vi_runtime':  _mv(['duration', 'running time', 'runtime', 'run time']),
        'vi_filesize': _mv(['filesize', 'file size', 'size']),
        'vi_filename': _mv(['source', 'filename', 'file name']),
        'vi_type':     _mv(['type']),
        'vi_imdb':     _mv(['imdb']),
    }

    if not vi['vi_year']:
        m_yr = re.search(r'\((\d{4})\)', title_raw)
        if m_yr:
            vi['vi_year'] = m_yr.group(1)

    return {
        'title_raw':      title_raw,
        'description':    description,
        'image_url':      image_url,
        'video_url':      video_url,
        'download_links': download_links,
        'categories':     categories,
        'is_series':      is_series,
        'is_complete':    is_complete,
        'source_url':     url,      # stored in WP meta for dedup
        'meta':           meta,
        **vi,
    }


# ══════════════════════════════════════════════════════════════
# TITLE CLEANING
# ══════════════════════════════════════════════════════════════

def clean_title_parts(raw: str):
    """
    Returns (title, title_b, is_series).

    Series  : title = "Blood Sisters Season 2",  title_b = "Episode 1 – 4 (Complete)"
    Movie   : title = "Michael (2026)",           title_b = ""
    """
    title       = re.sub(r'\s+', ' ', raw).strip()
    title_lower = title.lower()
    is_complete = bool(re.search(r'\bcomplete(d)?\b', title_lower))

    # Strip trailing "| Category" suffix
    pipe_match = re.search(r'\s*\|\s*[^|]+$', title)
    if pipe_match:
        title = title[:pipe_match.start()].strip()

    # Detect series (SXX / Season X)
    series_pat = re.compile(r'(?i)(.*?\b(S\d{1,2}|Season\s?\d{1,2}))[\s\-–:]*\s*(.*)')
    match       = series_pat.match(title)
    if match:
        base    = match.group(1).strip()
        rest    = match.group(3).strip()
        ep_block = re.match(
            r'[\(\[](Episode\s*[\d\s\-–—]+(?:Added)?|Complete[d]?)[\)\]]',
            rest, re.IGNORECASE
        )
        if ep_block:
            title_b = ep_block.group(1).strip()
        else:
            title_b = re.sub(r'^\(|\)$', '', rest).strip()
            title_b = re.sub(r'\s*\([^)]{2,30}\)\s*$', '', title_b).strip()
        if is_complete and 'complete' not in base.lower() and 'complete' not in title_b.lower():
            base += ' (Completed)' if 'completed' in title_lower else ' (Complete)'
        return base, title_b, True

    # Episode info in parens: "Show (Episode 3 Added)"
    ep_in_paren = re.search(r'^(.*?)\s*\((Episode\s*\d+.*?)\)\s*$', title, re.IGNORECASE)
    if ep_in_paren:
        return ep_in_paren.group(1).strip(), ep_in_paren.group(2).strip(), True

    # Movie with year
    movie_match = re.search(r'^(.*?\(\d{4}\))', title)
    if movie_match:
        return movie_match.group(1).strip(), '', False

    return title, '', False


def _resolve_all_download_links(parsed: dict, scraper) -> None:
    """
    In-place: replace every wrapper URL in parsed['download_links'] with its
    resolved direct-download URL (see _resolve_sl_download_link docstring).
    Skipped links (resolution failure) keep their original wrapper URL so
    nothing is lost — worst case the visitor sees the wrapper page instead
    of an instant download.
    """
    for dl in parsed.get('download_links', []):
        wrapper = dl.get('url', '')
        if not wrapper:
            continue
        dl['url'] = _resolve_sl_download_link(wrapper, scraper)


# ══════════════════════════════════════════════════════════════
# WORDPRESS API HELPERS
# ══════════════════════════════════════════════════════════════

def _get_wp_auth_header() -> dict:
    from django.conf import settings
    username = getattr(settings, 'WP_USERNAME', '')
    password = getattr(settings, 'WP_APP_PASSWORD', '')
    token    = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {
        'Authorization': f'Basic {token}',
        'Content-Type':  'application/json',
    }


def _get_wp_base_url() -> str:
    from django.conf import settings
    return getattr(settings, 'WP_SITE_URL', '').rstrip('/')


# ── SeriezLoaded source category → your WP target category ───────────────────
# Adjust the VALUES to match the exact category names on YOUR target WP site.
_SL_CAT_MAP: dict[str, str] = {
    # ── Hollywood ─────────────────────────────────────────────────────────────
    "hollywood movie":        "Hollywood movie",
    "holly-wood-movies":      "Hollywood movie",
    "hollywood movies":       "Hollywood movie",
    # ── Hollywood Series ──────────────────────────────────────────────────────
    "hollywood tv series":    "Hollywood Series",
    "hollywood series":       "Hollywood Series",
    "hollywood-tv-series":    "Hollywood Series",
    # ── Nollywood ─────────────────────────────────────────────────────────────
    "nollywood movie":        "Nollywood movie",
    "nollywood movies":       "Nollywood movie",
    "nollywood":              "Nollywood movie",
    # ── Nollywood Series ──────────────────────────────────────────────────────
    "nollywood tv series":    "Nollywood tv series",
    "nollywood series":       "Nollywood tv series",
    # ── Korean ────────────────────────────────────────────────────────────────
    "korean drama":           "Korean Drama",
    "korean series":          "Korean Drama",
    "kdrama":                 "Korean Drama",
    "k-drama":                "Korean Drama",
    # ── Chinese ───────────────────────────────────────────────────────────────
    "chinese drama":          "Chinese drama",
    "chinese series":         "Chinese drama",
    "cdrama":                 "Chinese drama",
    "chinese movie":          "Chinese movie",
    "chinese movies":         "Chinese movie",
    # ── Thai ──────────────────────────────────────────────────────────────────
    "thai drama":             "Thai drama",
    "thai series":            "Thai drama",
    # ── South African ─────────────────────────────────────────────────────────
    "south african series":   "South African Series",
    "sa series":              "South African Series",
    # ── Indian ────────────────────────────────────────────────────────────────
    "indian movie":           "Indian movie",
    "india movies":           "Indian movie",
    "bollywood":              "Indian movie",
    # ── Anime ─────────────────────────────────────────────────────────────────
    "anime":                  "Anime",
    "anime series":           "Anime",
    # ── Genres used as fallback categories ────────────────────────────────────
    "action":                 "Action",
    "animation":              "Animation",
    "biography":              "Biography",
    "comedy":                 "Comedy",
    "crime":                  "Crime",
    "documentary":            "Documentary",
    "drama":                  "TV Series",
    "entertainment":          "Entertainment",
    "family":                 "Family",
    "fantasy":                "Fantasy",
    "history":                "History",
    "horror":                 "Horror",
    "mystery":                "Mystery",
    "reality-tv":             "Reality-tv",
    "romance":                "Romance",
    "sci-fi":                 "Sci-fi",
    "thriller":               "Thriller",
    "war":                    "War",
    "western":                "Western",
    # ── Fallback safety nets ──────────────────────────────────────────────────
    "movie":                  "Movie",
    "movies":                 "Movie",
    "tv series":              "TV Series",
    "series":                 "TV Series",
}


def _wp_get_or_create_category(cat_name: str, headers: dict, wp_base: str,
                                is_series: bool = False) -> int | None:
    """Resolve cat_name → WP category ID on the target site."""
    raw = cat_name.strip()
    if not raw:
        raw = "TV Series" if is_series else "Movie"

    # Step 1: hardcoded map (case-insensitive)
    mapped = _SL_CAT_MAP.get(raw.lower(), raw)
    key    = mapped.strip().lower()

    if key in _wp_category_cache:
        return _wp_category_cache[key]

    # Step 2: live WP search
    try:
        r = requests.get(
            f'{wp_base}/wp-json/wp/v2/categories',
            params={'search': mapped, 'per_page': 20},
            headers=headers, timeout=10,
        )
        if r.status_code == 200:
            for cat in r.json():
                if cat['name'].strip().lower() == key:
                    _wp_category_cache[key] = cat['id']
                    print(f"    📁 WP category: '{mapped}' (ID {cat['id']})")
                    return cat['id']

        # Step 3: fallback
        fallback = "TV Series" if is_series else "Movie"
        print(f"    ⚠️ Category '{mapped}' not found → fallback to '{fallback}'")
        if mapped.strip().lower() == fallback.strip().lower():
            return None
        return _wp_get_or_create_category(fallback, headers, wp_base, is_series)

    except Exception as e:
        print(f"    ⚠️ WP category error ({mapped}): {e}")
    return None


def _wp_upload_image(image_url: str, title: str, headers: dict, wp_base: str) -> int | None:
    """Download poster from image_url and upload to WP media library."""
    try:
        img_resp = requests.get(image_url, timeout=20, stream=True)
        if img_resp.status_code != 200:
            print(f"    ⚠️ Image download failed: HTTP {img_resp.status_code}")
            return None
        content_type = img_resp.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip()
        ext_map = {
            'image/jpeg': 'jpg', 'image/jpg': 'jpg',
            'image/png': 'png', 'image/webp': 'webp', 'image/gif': 'gif',
        }
        ext      = ext_map.get(content_type, 'jpg')
        filename = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-') + f'.{ext}'

        upload_headers = {
            **headers,
            'Content-Type':        content_type,
            'Content-Disposition': f'attachment; filename="{filename}"',
        }
        r = requests.post(
            f'{wp_base}/wp-json/wp/v2/media',
            headers=upload_headers,
            data=img_resp.content,
            timeout=30,
        )
        if r.status_code == 201:
            media_id = r.json().get('id')
            print(f"    🖼️  WP image uploaded → ID {media_id}")
            return media_id
        else:
            print(f"    ⚠️ WP image upload failed: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"    ⚠️ WP image upload error: {e}")
    return None


# ══════════════════════════════════════════════════════════════
# DUPLICATE DETECTION
# ══════════════════════════════════════════════════════════════

def _wp_find_by_source_url(source_url: str, headers: dict, wp_base: str) -> dict | None:
    """
    PRIMARY dedup: query WP for a post whose custom meta field
    '_seriezloaded_source_url' matches *source_url*.

    Requires functions.php registration (see module docstring).
    Falls back to scanning the 50 most recent posts when meta_key
    filtering is not supported server-side.
    """
    if not source_url:
        return None
    source_url = _normalise_sl_url(source_url)
    try:
        r = requests.get(
            f'{wp_base}/wp-json/wp/v2/posts',
            params={
                'meta_key':   '_seriezloaded_source_url',
                'meta_value': source_url,
                'per_page':   5,
                'status':     'any',
                '_fields':    'id,title,slug,categories,meta',
                'context':    'edit',
            },
            headers=headers, timeout=10,
        )
        if r.status_code == 200:
            for post in r.json():
                stored = ''
                post_meta = post.get('meta', {})
                if isinstance(post_meta, dict):
                    stored = post_meta.get('_seriezloaded_source_url', '') or ''
                stored = _normalise_sl_url(stored.strip())
                if stored == source_url:
                    print(f"    🔎 Found by source URL (ID {post['id']})")
                    return post

        # Wider scan fallback
        r2 = requests.get(
            f'{wp_base}/wp-json/wp/v2/posts',
            params={
                'per_page': 50,
                'status':   'any',
                '_fields':  'id,title,slug,categories,meta',
                'context':  'edit',
            },
            headers=headers, timeout=15,
        )
        if r2.status_code == 200:
            for post in r2.json():
                stored = ''
                post_meta = post.get('meta', {})
                if isinstance(post_meta, dict):
                    stored = post_meta.get('_seriezloaded_source_url', '') or ''
                stored = _normalise_sl_url(stored.strip())
                if stored == source_url:
                    print(f"    🔎 Found by meta scan (ID {post['id']})")
                    return post
    except Exception as exc:
        print(f"    ⚠️ WP source-URL lookup error: {exc}")
    return None


def _strip_episode_suffix(text: str) -> str:
    """Strip episode/complete suffix for title-based matching."""
    text = re.sub(
        r'\s*[\(\[]?\s*(?:episode\s*[\d\s\-–—]+(?:added)?|complete[d]?)\s*[\)\]]?'
        r'(?:\s*[\(\[][^\)\]]*[\)\]])*\s*$',
        '', text, flags=re.IGNORECASE
    ).strip()
    text = re.sub(r'\s*\|.*$', '', text).strip()
    return text


def _wp_find_existing_post(title: str, headers: dict, wp_base: str,
                           is_series: bool = False) -> dict | None:
    """
    FALLBACK dedup: title-based search.  Used only when no source URL match found.

    Matching rules (strictest first):
      1. Exact full-title match (case-insensitive)
      2. Bare title exact match (stripped of our "| Mp4 Mkv DOWNLOAD" suffix)
      3. Complete-stripped bare title match
      4. SERIES ONLY — base title match (episode suffix stripped from both)
    """
    search_title = re.sub(r'\s*\(Complet(?:e|ed)\)\s*$', '', title, flags=re.IGNORECASE).strip()
    base_for_query = _strip_episode_suffix(search_title)
    base_for_query = re.sub(r'\s*\(\d{4}\)\s*$', '', base_for_query).strip()

    try:
        r = requests.get(
            f'{wp_base}/wp-json/wp/v2/posts',
            params={
                'search':   base_for_query,
                'per_page': 10,
                'status':   'any',
                '_fields':  'id,title,slug,categories,meta',
                'context':  'edit',
            },
            headers=headers, timeout=10,
        )
        if r.status_code != 200:
            return None

        search_lower      = search_title.strip().lower()
        title_lower       = title.strip().lower()
        base_search_lower = _strip_episode_suffix(search_lower)

        for post in r.json():
            rendered = BeautifulSoup(
                post['title']['rendered'], 'html.parser'
            ).get_text().strip().lower()
            rendered_bare = re.sub(
                r'\s*\|\s*mp4\s+mkv\s+download\s*$', '', rendered, flags=re.IGNORECASE
            ).strip()
            rendered_bare_nc = re.sub(
                r'\s*\(complet(?:e|ed)\)\s*$', '', rendered_bare, flags=re.IGNORECASE
            ).strip()
            rendered_base = _strip_episode_suffix(rendered_bare)

            matched    = rendered in (title_lower, search_lower)
            match_rule = 'exact full title'
            if not matched and rendered_bare in (title_lower, search_lower):
                matched    = True
                match_rule = 'bare title'
            if not matched and rendered_bare_nc == search_lower:
                matched    = True
                match_rule = 'complete-stripped'
            if not matched and is_series and base_search_lower and rendered_base == base_search_lower:
                matched    = True
                match_rule = 'series base title'

            if not matched:
                continue

            # Guard: reject if stored source URL belongs to a different post
            post_meta  = post.get('meta', {})
            stored_src = ''
            if isinstance(post_meta, dict):
                stored_src = _normalise_sl_url(
                    (post_meta.get('_seriezloaded_source_url') or '').strip()
                )
            title_slug_words = set(re.sub(r'[^a-z0-9]', ' ', base_search_lower or title_lower).split())
            if stored_src:
                stored_slug_words = set(re.sub(r'[^a-z0-9]', ' ', stored_src.lower()).split())
                overlap = title_slug_words & stored_slug_words
                if len(overlap) < 2:
                    print(f"    ⚠️  Title match (ID {post['id']}) rejected — different source URL")
                    continue

            print(f"    🔎 WP duplicate ({match_rule}): {post['title']['rendered']}")
            return post

    except Exception as e:
        print(f"    ⚠️ WP search error: {e}")
    return None


# ══════════════════════════════════════════════════════════════
# SLUG BUILDER
# ══════════════════════════════════════════════════════════════

def _make_slug(text: str, is_series: bool = False) -> str:
    import unicodedata
    if is_series:
        text = re.sub(
            r'\s*[\(\[]?\s*(?:episode\s*[\d\s\-–—]+(?:added)?|complete[d]?)\s*[\)\]]?\s*$',
            '', text, flags=re.IGNORECASE
        ).strip()
        text = re.sub(r'\s*\|.*$', '', text).strip()
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    text = text.lower()
    text = re.sub(r"[`']+", '', text)
    text = re.sub(r'[^a-z0-9]+', '-', text)
    return text.strip('-')


# ══════════════════════════════════════════════════════════════
# WP CONTENT BUILDER
# ══════════════════════════════════════════════════════════════

def _build_wp_content(title: str, title_b: str, description: str,
                      meta_info: dict, image_url: str, video_url: str,
                      download_links: list, is_series: bool,
                      wp_image_url: str = '') -> str:
    """
    Build the HTML post body matching the SeriezLoaded.com.ng post design.

    Layout:
      0.  Notice warning box
      0b. Poster image centred
      1.  Synopsis / description paragraph
      2.  VIDEO INFORMATION heading + blockquote card
      3.  TRAILER heading + YouTube iframe
      4.  DOWNLOAD LINKS heading + VLC tip box + download buttons
    """
    parts = []

    year = meta_info.get('vi_year', meta_info.get('year', '')).strip()
    if not year:
        _yr_m = re.search(r'\((\d{4})\)', title)
        if _yr_m:
            year = _yr_m.group(1)

    # ── 0. Notice warning ─────────────────────────────────────────
    # parts.append(
    #     '<div style="background:#fff3cd; border:1px solid #ffc107; padding:10px 14px; '
    #     'margin:10px 0; border-radius:6px; font-size:14px;">'
    #     '<strong>Notice!</strong> This Website Makes Use Of Pop Ads Which Might Be '
    #     'Annoying To Users. Kindly Close Any Unwanted Tab That Pops Up.'
    #     '</div>'
    # )

    # ── 0b. Poster image ──────────────────────────────────────────
    _inline_img_src = wp_image_url or image_url
    if _inline_img_src:
        safe_title = title.replace('"', '&quot;')
        parts.append(
            f'<p style="text-align:center;">'
            f'<img decoding="async" src="{_inline_img_src}" '
            f'class="aligncenter size-full" alt="{safe_title}" /></p>'
        )

    # ── 1. Synopsis ───────────────────────────────────────────────
    if description:
        _vi_cut = re.split(r'video\s+information', description, maxsplit=1, flags=re.IGNORECASE)
        desc    = _vi_cut[0].strip().rstrip('–—-|:,').strip()
        if desc:
            parts.append(f'<p>{desc}</p>')

    # ── 2. VIDEO INFORMATION blockquote ──────────────────────────
    filesize = meta_info.get('vi_filesize', '').strip()
    dur      = meta_info.get('vi_runtime',  '').strip()
    imdb     = meta_info.get('vi_imdb',     '').strip()
    status   = meta_info.get('vi_status',   '').strip()
    sub      = meta_info.get('vi_subtitle', '').strip()
    genre    = meta_info.get('vi_genre',    '').strip()
    stars    = meta_info.get('vi_cast',     '').strip()
    country  = meta_info.get('vi_country',  '').strip()
    lang     = meta_info.get('vi_language', '').strip()
    director = meta_info.get('vi_director', '').strip()
    total_ep = meta_info.get('vi_episodes', '').strip()
    vi_type  = meta_info.get('vi_type',     '').strip() or ('TV Series' if is_series else 'Movie')
    source   = meta_info.get('vi_filename', '').strip()

    _title_clean = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()

    info_lines = []
    if _title_clean: info_lines.append(f' Title: {_title_clean}')
    if year:         info_lines.append(f' Year: {year}')
    if genre:        info_lines.append(f' Genre: {genre}')
    if dur:          info_lines.append(f' Duration: {dur}')
    info_lines.append(          f' Type: {vi_type}')
    if country:      info_lines.append(f' Country: {country}')
    if stars:        info_lines.append(f' Stars: {stars}')
    if lang:         info_lines.append(f' Language: {lang}')
    if sub:          info_lines.append(f' Subtitle Language: {sub}')
    if director:     info_lines.append(f' Director: {director}')
    if total_ep:     info_lines.append(f' Total Episodes: {total_ep}')
    if status:       info_lines.append(f' Status: {status}')
    if filesize:     info_lines.append(f' Filesize: {filesize}')
    if source:       info_lines.append(f' Source: {source}')
    if imdb:
        info_lines.append(
            f'🌟 IMDB: <a href="{imdb}" target="_blank" rel="nofollow noopener">{imdb}</a>'
        )

    if info_lines:
        parts.append('<p><u><strong>VIDEO INFORMATION</strong></u></p>')
        inner = '<br />\n'.join(info_lines)
        parts.append(f'<blockquote><p>\n{inner}\n</p></blockquote>')

    # ── 3. TRAILER ────────────────────────────────────────────────
    if video_url:
        parts.append('<p><strong><u>Trailer</u></strong></p>')
        yt_match = re.search(
            r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([\w\-]{11})',
            video_url
        )
        embed_url = f'https://www.youtube.com/embed/{yt_match.group(1)}' if yt_match else video_url
        parts.append(
            f'<p><iframe width="780" height="439" src="{embed_url}" '
            f'title="{title.replace(chr(34), chr(39))} Trailer" '
            f'frameborder="0" allow="accelerometer; autoplay; clipboard-write; '
            f'encrypted-media; gyroscope; picture-in-picture; web-share" '
            f'allowfullscreen></iframe></p>'
        )

    # ── 4. DOWNLOAD LINKS ─────────────────────────────────────────
    parts.append('<p><strong>DOWNLOAD LINKS</strong>🚨</p>')

    # ── VLC tip box — yellow background, gold border, bold coloured text ──
    # Matches the reference screenshot exactly:
    #   • Yellow/cream background (#fffbe6)  with gold border (#e6c619)
    #   • "Highly Recommended!" in dark-gold bold
    #   • "VLC or MX Player" in red bold
    #   • "How to download from this site" in dark-gold bold + blue "Click HERE!" link
    parts.append(
        '<div style="background:#fff9e6; border:2px solid #ffd700; padding:10px 12px; '
        'margin:15px 0; border-radius:12px; font-family:Arial; line-height:1.5; text-align:left;">'
        '<span style="color:#8b4513; font-weight:bold; font-size:14px;">Highly Recommended!</span> '
        '<span style="color:#ff0000; font-weight:bold; font-size:14px;">VLC or MX Player</span> '
        '<span style="color:#5d4037; font-size:14px;">use app to watch this video (no audio or video issues).</span><br />'
        '<span style="color:#5d4037; font-size:14px;">It Also supports subtitle if stated on the post (Subtitle: English).</span><br />'
        '<span style="color:#8b4513; font-weight:bold; font-size:14px;">How to download from this site &#8212;</span> '
        '<a href="https://t.me/naijadeleyschannel/8" '
        'style="color:#0056b3; font-weight:900; text-decoration:none; font-size:14px;">Click HERE!</a>'
        '</div>'
    )

    # Green-outlined buttons matching the 9jarocks scraper's design
    _BTN_A = (
        'display:inline-flex; align-items:center; background:#fff; '
        'border:3px solid #28a745; color:#28a745; padding:8px 18px; '
        'text-decoration:none; font-weight:900; border-radius:6px; '
        'box-shadow:0 3px 10px rgba(0,0,0,.5); text-transform:uppercase; font-size:14px;'
    )
    _BTN_ICON = (
        '<img decoding="async" '
        'src="https://img.icons8.com/material-sharp/24/28a745/download.png" '
        'style="width:18px; height:18px; margin-right:10px;">'
    )
    _BTN_A_SM = (
        'display:inline-flex; align-items:center; background:#fff; '
        'border:3px solid #28a745; color:#28a745; padding:6px 15px; '
        'text-decoration:none; font-weight:900; border-radius:6px; '
        'box-shadow:0 3px 8px rgba(0,0,0,.5); text-transform:uppercase; font-size:13px;'
    )
    _BTN_ICON_SM = (
        '<img decoding="async" style="width:16px; margin-right:8px;" '
        'src="https://img.icons8.com/material-sharp/24/28a745/download.png" />'
    )

    if is_series:
        parts.append('<div style="text-align:left; font-family:Arial; margin-top:10px;">')
        for dl in download_links:
            url      = dl['url']
            ep_label = dl.get('ep_label', '').strip()
            raw_lbl  = dl.get('label', '').strip()

            if not url or not url.startswith('http'):
                continue

            # Build a clean episode heading
            if ep_label:
                se_match = re.search(r'S(\d+)E(\d+)', ep_label, re.IGNORECASE)
                e_match  = re.match(r'E(\d+)$', ep_label, re.IGNORECASE)
                if se_match:
                    sn, en = int(se_match.group(1)), int(se_match.group(2))
                    ep_heading = f'S{sn} EPISODE {en}' if sn > 1 else f'EPISODE {en}'
                elif e_match:
                    ep_heading = f'EPISODE {int(e_match.group(1))}'
                else:
                    ep_heading = ep_label
            else:
                ep_heading = raw_lbl or 'DOWNLOAD'

            parts.append(
                f'<div style="margin-bottom:8px;">'
                f'<a style="{_BTN_A_SM}" href="{url}">'
                f'{_BTN_ICON_SM}{ep_heading}</a>'
                f'</div>'
            )
        parts.append('</div>')
    else:
        for dl in download_links:
            url       = dl['url']
            raw_label = dl.get('label', '').strip()
            if not url or not url.startswith('http'):
                continue
            res_match = re.search(r'(\d{3,4}p)', raw_label, re.IGNORECASE)
            if res_match and len(download_links) > 1:
                btn_text = f'DOWNLOAD HERE ({res_match.group(1)})'
            else:
                btn_text = 'DOWNLOAD HERE'
            parts.append(
                f'<div style="text-align:left; margin:10px 0 15px; font-family:Arial;">'
                f'<a href="{url}" style="{_BTN_A}">'
                f'{_BTN_ICON}{btn_text}'
                f'</a></div>'
            )

    return '\n'.join(parts)


# ══════════════════════════════════════════════════════════════
# RANK MATH SEO BUILDER
# ══════════════════════════════════════════════════════════════

def _build_rank_math_seo(title: str, title_b: str, description: str,
                         meta_info: dict, categories: list,
                         is_series: bool) -> dict:
    year    = meta_info.get('vi_year', '').strip()
    country = meta_info.get('vi_country', '').strip()

    cat_lower = ' '.join(c.lower() for c in categories)
    if 'korean' in cat_lower or 'kdrama' in cat_lower:
        drama_type = 'Korean'
    elif 'thai' in cat_lower:
        drama_type = 'Thai'
    elif 'chinese' in cat_lower or 'cdrama' in cat_lower:
        drama_type = 'Chinese'
    elif 'anime' in cat_lower:
        drama_type = 'Anime'
    elif 'indian' in cat_lower or 'bollywood' in cat_lower or 'india' in cat_lower:
        drama_type = 'Indian'
    elif 'nollywood' in cat_lower or 'nigerian' in cat_lower:
        drama_type = 'Nollywood'
    elif 'south african' in cat_lower or 'sa series' in cat_lower:
        drama_type = 'South African'
    else:
        drama_type = country if country else ''

    is_nollywood  = 'nollywood' in cat_lower or 'nigerian' in cat_lower
    is_anime      = 'anime' in cat_lower
    is_completed  = any(x in title.lower() for x in ('complete', 'completed'))

    ep_num   = ''
    ep_match = re.search(r'episode\s*(\d+)', title_b, re.IGNORECASE)
    if ep_match:
        ep_num = ep_match.group(1)

    # Focus keyword
    if is_anime and is_series:
        focus_kw = f'Download {title} Episode {ep_num} Anime' if ep_num else f'Download {title} Anime'
    elif is_series and drama_type and is_completed:
        focus_kw = f'Download {title} Complete {drama_type} Drama'
    elif is_series and drama_type:
        focus_kw = (f'Download {title} Episode {ep_num} {drama_type} Drama'
                    if ep_num else f'Download {title} {drama_type} Drama')
    elif is_series and is_completed:
        focus_kw = f'Download {title} Season Complete'
    elif is_series:
        focus_kw = (f'{title} Episode {ep_num} Download'
                    if ep_num else f'{title} Season Download')
    elif is_nollywood:
        focus_kw = (f'Download {title} ({year}) Nollywood Movie'
                    if year else f'Download {title} Nollywood Movie')
    else:
        focus_kw = (f'Download {title} ({year}) Movie'
                    if year else f'Download {title} Movie')

    # SEO title
    if is_series and title_b:
        seo_title = f'{title} ({title_b}) - Free Download'
    elif year and f'({year})' not in title:
        seo_title = f'{title} ({year}) - Free Download'
    else:
        seo_title = f'{title} - Free Download'

    # Meta description
    if is_series and drama_type and is_completed:
        desc = (
            f'{title} complete series download, '
            f'Download {title} Complete {drama_type} Drama in 480p 720p 1080p Mkv Mp4, '
            f'Download {title} ({year}) (Complete) Free'
        )
    elif is_series and drama_type:
        desc = (
            f'{title} Episode {ep_num} {drama_type} drama download, '
            f'Download {title} Episode {ep_num} in 480p 720p 1080p Mkv Mp4'
        ) if ep_num else (
            f'{title} {drama_type} drama download, '
            f'Download {title} in 480p 720p 1080p Mkv Mp4'
        )
    elif is_series:
        desc = (
            f'{title} Episode {ep_num} download, '
            f'Download {title} ({title_b}) TV Series Free in 480p 720p 1080p'
        ) if ep_num else (
            f'{title} series download, Download {title} ({title_b}) TV Series Free'
        )
    elif is_nollywood:
        desc = (
            f'Download {title} ({year}) Nollywood Movie free in 480p 720p 1080p Mkv Mp4'
            if year else f'Download {title} Nollywood Movie free'
        )
    else:
        desc = (
            f'Download {title} ({year}) Movie free in 480p 720p 1080p Mkv Mp4'
            if year else f'Download {title} Movie free'
        )

    return {
        'rank_math_focus_keyword': focus_kw,
        'rank_math_title':         seo_title,
        'rank_math_description':   desc,
    }


# ══════════════════════════════════════════════════════════════
# WORDPRESS POST PUBLISHER  —  with smart dedup
# ══════════════════════════════════════════════════════════════

def _post_to_wordpress(
    title: str, title_b: str, description: str,
    meta_info: dict, image_url: str, video_url: str,
    download_links: list, categories: list,
    is_series: bool, wp_cat_name: str,
) -> bool:
    try:
        headers  = _get_wp_auth_header()
        wp_base  = _get_wp_base_url()

        if not wp_base:
            print("    ⚠️ WP_SITE_URL not configured — skipping.")
            return False

        full_title = (
            f'{title} ({title_b}) | Mp4 Mkv DOWNLOAD'
            if is_series and title_b
            else f'{title} | Mp4 Mkv DOWNLOAD'
        )
        excerpt_text = description[:300] if description else ''

        # ── PRIMARY: dedup by stored source URL ───────────────────
        source_url    = _normalise_sl_url(meta_info.get('source_url', ''))
        existing_post = (
            _wp_find_by_source_url(source_url, headers, wp_base)
            if source_url else None
        )

        # ── SECONDARY: dedup by title (fallback) ──────────────────
        if not existing_post:
            existing_post = _wp_find_existing_post(title, headers, wp_base, is_series=is_series)

        # ── MOVIE: skip if already exists ─────────────────────────
        if existing_post and not is_series:
            post_id = existing_post['id']
            print(f"    ⏭️  Movie already exists (ID {post_id}) — skipping.")
            return True

        # ── SERIES: skip if same episode count ────────────────────
        if existing_post and is_series:
            post_id       = existing_post['id']
            current_title = BeautifulSoup(
                existing_post['title']['rendered'], 'html.parser'
            ).get_text().strip()

            if not title_b:
                print(f"    ⏭️  Series with no episode info (ID {post_id}) — skipping.")
                return True

            if current_title.strip().lower() == full_title.strip().lower():
                print(f"    ⏭️  Series already up to date (ID {post_id}) — skipping.")
                return True

            print(f"    🆕  New episode detected — updating post (ID {post_id})...")

        # ── IMAGE UPLOAD (only for new posts or series updates) ───
        wp_media_id  = None
        wp_image_url = ''
        if image_url:
            wp_media_id = _wp_upload_image(image_url, title, headers, wp_base)
            if wp_media_id:
                try:
                    _mr = requests.get(
                        f'{wp_base}/wp-json/wp/v2/media/{wp_media_id}',
                        headers=headers, timeout=10,
                    )
                    if _mr.status_code == 200:
                        wp_image_url = _mr.json().get('source_url', '')
                except Exception:
                    pass
            if not wp_image_url:
                wp_image_url = image_url

        content = _build_wp_content(
            title, title_b, description, meta_info,
            image_url, video_url, download_links, is_series,
            wp_image_url=wp_image_url,
        )
        rank_math_meta = _build_rank_math_seo(
            title, title_b, description, meta_info, categories, is_series
        )

        cat_id  = _wp_get_or_create_category(wp_cat_name, headers, wp_base, is_series)
        cat_ids = [cat_id] if cat_id else []

        # ── SERIES: UPDATE existing post ──────────────────────────
        if existing_post and is_series:
            post_id = existing_post['id']
            from datetime import datetime, timezone as tz
            now_utc = datetime.now(tz.utc)
            patch: dict = {
                'title':   full_title,
                'content': content,
                'date':     now_utc.strftime('%Y-%m-%dT%H:%M:%S'),
                'date_gmt': now_utc.strftime('%Y-%m-%dT%H:%M:%S'),
                'meta': {
                    **rank_math_meta,
                    '_seriezloaded_source_url': source_url,
                },
            }
            if excerpt_text:
                patch['excerpt'] = excerpt_text
            if cat_ids:
                existing_cats       = existing_post.get('categories', [])
                patch['categories'] = list(set(existing_cats + cat_ids))
            if wp_media_id:
                patch['featured_media'] = wp_media_id

            r = requests.post(
                f'{wp_base}/wp-json/wp/v2/posts/{post_id}',
                headers=headers, json=patch, timeout=15,
            )
            if r.status_code == 200:
                print(f"    ✏️  WP series updated (ID {post_id}) — {full_title}")
                return True
            else:
                print(f"    ⚠️ WP update failed: {r.status_code} {r.text[:150]}")
                return False

        # ── CREATE new post ────────────────────────────────────────
        post_data: dict = {
            'title':   full_title,
            'slug':    _make_slug(title, is_series=is_series),
            'content': content,
            'status':  'publish',
            'format':  'video',
            'excerpt': excerpt_text or '',
            'meta': {
                **rank_math_meta,
                '_seriezloaded_source_url': source_url,
            },
        }
        if cat_ids:
            post_data['categories'] = cat_ids
        if wp_media_id:
            post_data['featured_media'] = wp_media_id

        r = requests.post(
            f'{wp_base}/wp-json/wp/v2/posts',
            headers=headers, json=post_data, timeout=20,
        )
        if r.status_code == 201:
            wp_id = r.json().get('id')
            print(f"    ✅ WP created (ID {wp_id}) — {full_title}")
            return True
        else:
            print(f"    ⚠️ WP create failed: {r.status_code} {r.text[:150]}")
            return False

    except Exception as e:
        print(f"    ⚠️ WordPress error: {e}")
        return False


# ══════════════════════════════════════════════════════════════
# URLS-FILE HELPER
# ══════════════════════════════════════════════════════════════

def _scrape_urls_list(urls: list, delay: float = 1.5):
    """Scrape and publish a list of specific SeriezLoaded post URLs."""
    scraper = _make_scraper()
    ok = fail = 0
    for post_url in urls:
        post_url = _normalise_sl_url(post_url.strip())
        if not _is_post_url(post_url):
            print(f"⚠️  Not a valid SeriezLoaded post URL: {post_url}")
            continue
        print(f"\n🎬 {post_url}")
        if delay > 0:
            time.sleep(delay)
        try:
            resp = scraper.get(post_url, timeout=25)
            if resp.status_code != 200:
                print(f"   ⚠️ HTTP {resp.status_code} — skipping")
                continue
            post_html = resp.text
        except Exception as e:
            print(f"   ❌ Fetch error: {e}")
            continue

        parsed = parse_post_page(post_html, post_url)
        if not parsed:
            print("   ⚠️ Could not parse post — skipping")
            continue
        if not parsed['download_links']:
            print(f"   ⛔ No download links — skipping '{parsed['title_raw']}'")
            continue

        print(f"   🔄 Resolving {len(parsed['download_links'])} download link(s)...")
        _resolve_all_download_links(parsed, scraper)

        title, title_b, is_series = clean_title_parts(parsed['title_raw'])
        if not parsed['is_series']:
            is_series = False

        print(f"   📝 Title  : {title}")
        if title_b:
            print(f"   📝 Episode: {title_b}")

        # Best-effort category from parsed data
        wp_cat_name = (
            parsed['categories'][0] if parsed['categories']
            else ('TV Series' if is_series else 'Movie')
        )

        result = _post_to_wordpress(
            title=title, title_b=title_b, description=parsed['description'],
            meta_info=parsed, image_url=parsed['image_url'],
            video_url=parsed['video_url'], download_links=parsed['download_links'],
            categories=parsed['categories'], is_series=is_series,
            wp_cat_name=wp_cat_name,
        )
        if result:
            ok += 1
        else:
            fail += 1

    print(f"\n✅ Done — published: {ok}  failed: {fail}")


# ══════════════════════════════════════════════════════════════
# DJANGO MANAGEMENT COMMAND
# ══════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = (
        'Scrape www.seriezloaded.com.ng category pages and publish directly '
        'to WordPress (no DB interaction, no social media).'
    )

    def _resolve_category_arg(self, cat_arg: str) -> list:
        if cat_arg in CATEGORY_ALIASES:
            keys = CATEGORY_ALIASES[cat_arg]
            return [_KEY_TO_DEF[k] for k in keys if k in _KEY_TO_DEF]
        if cat_arg in _SLUG_TO_DEF:
            return [_SLUG_TO_DEF[cat_arg]]
        normalised = cat_arg.replace('_', '-')
        if normalised in _SLUG_TO_DEF:
            return [_SLUG_TO_DEF[normalised]]
        normalized = cat_arg.replace('-', '_')
        if normalized in _KEY_TO_DEF:
            return [_KEY_TO_DEF[normalized]]
        return []

    def _print_category_list(self):
        print("\n📋  Available --category aliases (seriezloaded.com.ng → WP)\n")
        print(f"  {'Alias':<22} {'WP Category':<30} {'Type'}")
        print("  " + "─" * 62)
        for alias, keys in CATEGORY_ALIASES.items():
            if not keys:
                continue
            for key in keys:
                d = _KEY_TO_DEF.get(key)
                if d:
                    t = 'Series' if d['is_series'] else 'Movie'
                    print(f"  {alias:<22} {d['wp_cat']:<30} {t}")
                    break
        print()

    def add_arguments(self, parser):
        parser.add_argument(
            '--category', type=str, default='all',
            help='Category to scrape (default: all). Use --list-categories to see options.',
        )
        parser.add_argument('--startpage',       type=int,   default=1)
        parser.add_argument('--endpage',         type=int,   default=None)
        parser.add_argument('--max-pages',       type=int,   default=None)
        parser.add_argument('--delay',           type=float, default=1.5,
                            help='Seconds to wait between post requests (default: 1.5)')
        parser.add_argument('--urls-file',       type=str,   default=None,
                            help='Path to a text file with one SeriezLoaded URL per line')
        parser.add_argument('--url',             type=str,   default=None,
                            help='Scrape a single SeriezLoaded post URL')
        parser.add_argument('--list-categories', action='store_true',
                            help='Print available category aliases and exit')

    def handle(self, *args, **options):

        if options.get('list_categories'):
            self._print_category_list()
            return

        # ── Single URL mode ───────────────────────────────────────
        single_url = options.get('url')
        if single_url:
            _scrape_urls_list([single_url], delay=options['delay'])
            return

        # ── URLs-file mode ────────────────────────────────────────
        urls_file = options.get('urls_file')
        if urls_file:
            try:
                with open(urls_file, 'r', encoding='utf-8') as f:
                    raw_lines = f.readlines()
            except FileNotFoundError:
                self.stderr.write(f"❌  File not found: {urls_file}")
                return

            urls = []
            for line in raw_lines:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                # Strip leading list markers like "1. " or "- "
                line = re.sub(r'^\d+\.\s*', '', line).strip()
                line = re.sub(r'^[-*]\s*', '', line).strip()
                if line.startswith('http'):
                    urls.append(line)

            print(f"📋  Loaded {len(urls)} URLs from {urls_file}")
            _scrape_urls_list(urls, delay=options['delay'])
            return

        # ── Normal category-crawl mode ────────────────────────────
        start_page = options['startpage']
        end_page   = options['endpage']
        max_pages  = options['max_pages']
        delay      = options['delay']
        cat_arg    = (options.get('category') or 'all').strip().lower()

        cats_to_crawl = self._resolve_category_arg(cat_arg)
        if not cats_to_crawl:
            self.stderr.write(
                f"❌  Unknown category '{cat_arg}'.\n"
                f"    Run with --list-categories to see all options."
            )
            return

        print("=" * 60)
        print("🚀  scrape_seriezloaded_wp — WordPress only, no DB, no social")
        print(f"    Source site : {SITE_URL}")
        print(f"    Categories  : {', '.join(d['label'] for d in cats_to_crawl)}")
        print(f"    Pages       : {start_page} → {end_page or '∞'}"
              + (f"  (max {max_pages})" if max_pages else ""))
        print("=" * 60)

        scraper = _make_scraper()

        total_scraped   = 0
        total_wp_ok     = 0
        total_wp_fail   = 0
        consecutive_err = 0
        max_consecutive = 5

        for cat_def in cats_to_crawl:
            cat_slug_full = cat_def['slug']
            wp_cat_name   = cat_def['wp_cat']
            cat_is_series = cat_def['is_series']
            # SeriezLoaded listing URL: /movies/<slug>/ or /series/<slug>/
            cat_base_url  = f"{SITE_URL}/{cat_slug_full}"

            print(f"\n\n{'═'*60}")
            print(f"📂  Category : {cat_def['label']}")
            print(f"    Slug     : {cat_slug_full}")
            print(f"    WP cat   : {wp_cat_name}")
            print(f"    URL      : {cat_base_url}")
            print(f"{'═'*60}")

            page          = start_page
            pages_crawled = 0

            while True:
                if end_page and page > end_page:
                    print(f"\n✅ Reached end page {end_page}.")
                    break
                if max_pages and pages_crawled >= max_pages:
                    print(f"\n✅ Crawled {max_pages} pages for this category.")
                    break

                # Pagination: /movies/hollywood-movies/page/2/ etc.
                listing_url = (
                    cat_base_url + '/'
                    if page == 1
                    else f"{cat_base_url}/page/{page}/"
                )

                print(f"\n{'─'*60}")
                print(f"🌐 Listing page {page}: {listing_url}")

                try:
                    resp = scraper.get(listing_url, timeout=25)
                    if resp.status_code == 404:
                        print("   ✅ No more pages (404). Moving on.")
                        break
                    resp.raise_for_status()
                    html = resp.text
                except Exception as e:
                    print(f"   ❌ Listing page fetch failed: {e}")
                    consecutive_err += 1
                    if consecutive_err >= max_consecutive:
                        print("   ❌ Too many consecutive errors — stopping.")
                        return
                    time.sleep(5)
                    continue

                consecutive_err = 0
                pages_crawled  += 1

                post_urls = get_post_urls_from_listing_page(html, listing_url)
                print(f"   📋 Found {len(post_urls)} posts on this page")

                if not post_urls:
                    print("   ⚠️ No posts found — end of category.")
                    break

                for post_url in post_urls:
                    print(f"\n   🎬 {post_url}")
                    if delay > 0:
                        time.sleep(delay)

                    try:
                        post_resp = scraper.get(post_url, timeout=25)
                        if post_resp.status_code != 200:
                            print(f"      ⚠️ HTTP {post_resp.status_code} — skipping")
                            continue
                        post_html = post_resp.text
                    except Exception as e:
                        print(f"      ❌ Fetch error: {e}")
                        continue

                    parsed = parse_post_page(post_html, post_url)
                    if not parsed:
                        print("      ⚠️ Could not parse post — skipping")
                        continue

                    if not parsed['download_links']:
                        print(f"      ⛔ No download links — skipping '{parsed['title_raw']}'")
                        continue

                    print(f"      🔄 Resolving {len(parsed['download_links'])} download link(s)...")
                    _resolve_all_download_links(parsed, scraper)

                    title, title_b, is_series = clean_title_parts(parsed['title_raw'])

                    # Post-level detection overrides category default
                    if not parsed['is_series']:
                        is_series = False
                    # Use cat_is_series as a hint when post doesn't self-identify
                    if not is_series and cat_is_series:
                        is_series = True

                    print(f"      📝 Title    : {title}")
                    if title_b:
                        print(f"      📝 Episode  : {title_b}")
                    print(f"      🏷  WP cat   : {wp_cat_name}")

                    total_scraped += 1

                    ok = _post_to_wordpress(
                        title          = title,
                        title_b        = title_b,
                        description    = parsed['description'],
                        meta_info      = parsed,
                        image_url      = parsed['image_url'],
                        video_url      = parsed['video_url'],
                        download_links = parsed['download_links'],
                        categories     = parsed['categories'],
                        is_series      = is_series,
                        wp_cat_name    = wp_cat_name,
                    )
                    if ok:
                        total_wp_ok += 1
                    else:
                        total_wp_fail += 1

                if not has_next_page(html):
                    print(f"\n   ✅ No next page — end of '{cat_def['label']}'.")
                    break

                page += 1

        print(f"\n\n{'=' * 60}")
        print("🎉  Done!")
        print(f"    Posts scraped    : {total_scraped}")
        print(f"    WP published OK  : {total_wp_ok}")
        print(f"    WP failures      : {total_wp_fail}")
        print("=" * 60)


# ──────────────────────────────────────────────────────────────
# Quick-reference usage:
#
#   python manage.py scrape_seriezloaded_wp
#   python manage.py scrape_seriezloaded_wp --list-categories
#
#   # Scrape by category:
#   python manage.py scrape_seriezloaded_wp --category nollywood --startpage 1 --endpage 5
#   python manage.py scrape_seriezloaded_wp --category all --max-pages 10 --delay 2.0
#   python manage.py scrape_seriezloaded_wp --category kdrama --max-pages 5
#
#   # Scrape individual posts from a URLs file:
#   python manage.py scrape_seriezloaded_wp --urls-file links.txt
#   python manage.py scrape_seriezloaded_wp --urls-file links.txt --delay 2.0
#
#   # Scrape a single post:
#   python manage.py scrape_seriezloaded_wp --url https://www.seriezloaded.com.ng/michael-2026/
#
#   links.txt format:
#       https://www.seriezloaded.com.ng/michael-2026/
#       https://www.seriezloaded.com.ng/blood-sisters-season-2-episode-1-4-complete/
#       # this is a comment
# ──────────────────────────────────────────────────────────────