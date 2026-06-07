"""
scrape_9jarocks_wp.py
=====================
Django management command that scrapes 9jarocks.net by crawling category
listing pages (WordPress / Jannah theme HTML), then visits each post to
extract title, image, description, video info, and download links —
and publishes DIRECTLY to WordPress.  Zero DB interaction, zero social posting.

Source site  : 9jarocks.net  (HTML scraping via WP REST API + HTML)
Target site  : Your WordPress site (WP REST API via WP_SITE_URL / WP_APP_PASSWORD)

Usage:
    python manage.py scrape_9jarocks_wp
    python manage.py scrape_9jarocks_wp --category hollywood
    python manage.py scrape_9jarocks_wp --category kdrama --startpage 3
    python manage.py scrape_9jarocks_wp --startpage 1 --endpage 5
    python manage.py scrape_9jarocks_wp --category all --max-pages 10

    # Scrape individual post URLs from a text file (one URL per line):
    python manage.py scrape_9jarocks_wp --urls-file links.txt
    python manage.py scrape_9jarocks_wp --urls-file links.txt --delay 1.0

    The URLs file should contain one 9jarocks.net post URL per line.
    Blank lines and lines starting with # are ignored.
    Example links.txt:
        https://9jarocks.net/videodownload/in-the-grey-2026-id393368.html
        https://9jarocks.net/videodownload/totally-funny-animals-season-2-id394058.html
        # This line is a comment and will be skipped

WORDPRESS ONE-TIME SETUP (required for smart dedup/update):
    Add this to your theme's functions.php so the script can store and query
    the original 9jarocks source URL on each post:

        add_action('init', function() {
            register_post_meta('post', '_9jarocks_source_url', [
                'show_in_rest' => true,
                'single'       => true,
                'type'         => 'string',
                'auth_callback' => '__return_true',
            ]);
        });

    How it works:
        - MOVIES  : if the source URL already exists on your WP site → SKIP (no duplicate)
        - SERIES  : if the source URL already exists → UPDATE the post in place
                    (title gets new episode count, download links refreshed,
                     slug never changes so Google ranking is preserved)
        - NEW     : if not found → CREATE a new post with a clean slug

Available --category aliases:
    hollywood, nollywood, nollywood_series, hollywood_series,
    kdrama, chinese_drama, thai_drama, filipino_drama, japanese_drama,
    anime, foreign, foreign_series, wrestling, ongoing, all  (default: all)

Place this file at:
    <your_app>/management/commands/scrape_9jarocks_wp.py
"""

from django.core.management.base import BaseCommand
import requests
from bs4 import BeautifulSoup
import re
import json as _json
import cloudscraper
from urllib.parse import urlparse, unquote
import urllib3
import time
import base64

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ══════════════════════════════════════════════════════════════
# SITE / CATEGORY CONSTANTS  —  9jarocks.net
# ══════════════════════════════════════════════════════════════

SITE_URL = 'https://9jarocks.net'

# 9jarocks uses /category/videodownload/<slug>  for all video content.
# The REST API categories endpoint (document index 3) confirms these IDs.
CATEGORY_DEFINITIONS = [
    {
        'key':       'hollywood',
        'slug':      'videodownload/hollywood-movie',
        'label':     'Hollywood Movie',
        'wp_cat':    'Hollywood movie',
        'is_series': False,
    },
    {
        'key':       'hollywood_series',
        'slug':      'videodownload/hollywood-tv-series',
        'label':     'Hollywood TV Series',
        'wp_cat':    'Hollywood Series',
        'is_series': True,
    },
    {
        'key':       'nollywood',
        'slug':      'videodownload/nollywood-movie',
        'label':     'Nollywood Movie',
        'wp_cat':    'Nollywood movie',
        'is_series': False,
    },
    {
        'key':       'nollywood_series',
        'slug':      'videodownload/nollywood-tv-series',
        'label':     'Nollywood TV Series',
        'wp_cat':    'Nollywood tv series',
        'is_series': True,
    },
    {
        'key':       'kdrama',
        'slug':      'videodownload/korean-drama',
        'label':     'Korean Drama',
        'wp_cat':    'Korean Drama',
        'is_series': True,
    },
    {
        'key':       'chinese_drama',
        'slug':      'videodownload/chinese-drama',
        'label':     'Chinese Drama',
        'wp_cat':    'Chinese drama',
        'is_series': True,
    },
    {
        'key':       'thai_drama',
        'slug':      'videodownload/thai-drama',
        'label':     'Thai Drama',
        'wp_cat':    'Thai drama',
        'is_series': True,
    },
    {
        'key':       'filipino_drama',
        'slug':      'videodownload/filipino-drama',
        'label':     'Filipino Drama',
        'wp_cat':    'Filipino Drama',
        'is_series': True,
    },
    {
        'key':       'japanese_drama',
        'slug':      'videodownload/japanese-drama',
        'label':     'Japanese Drama',
        'wp_cat':    'Japanese drama',
        'is_series': True,
    },
    {
        'key':       'anime',
        'slug':      'videodownload/anime',
        'label':     'Anime',
        'wp_cat':    'Anime',
        'is_series': True,
    },
    {
        'key':       'foreign',
        'slug':      'videodownload/foreign-movies',
        'label':     'Other Foreign Movies',
        'wp_cat':    'Other foreign movies',
        'is_series': False,
    },
    {
        'key':       'foreign_series',
        'slug':      'videodownload/other-foreign-series',
        'label':     'Other Foreign Series',
        'wp_cat':    'Other Foreign Series',
        'is_series': True,
    },
    {
        'key':       'wrestling',
        'slug':      'videodownload/pro-wrestling-fighting-sports',
        'label':     'Pro Wrestling & Fighting Sports',
        'wp_cat':    'Wrestling',
        'is_series': True,
    },
    {
        'key':       'ongoing',
        'slug':      'videodownload/ongoing',
        'label':     'Ongoing Series',
        'wp_cat':    'Ongoing',
        'is_series': True,
    },
]

CATEGORY_ALIASES = {
    'hollywood':        ['hollywood'],
    'hollywood_series': ['hollywood_series'],
    'series':           ['hollywood_series', 'nollywood_series'],
    'nollywood':        ['nollywood'],
    'nollywood_series': ['nollywood_series'],
    'kdrama':           ['kdrama'],
    'korean':           ['kdrama'],
    'chinese':          ['chinese_drama'],
    'cdrama':           ['chinese_drama'],
    'chinese_drama':    ['chinese_drama'],
    'thai':             ['thai_drama'],
    'thai_drama':       ['thai_drama'],
    'filipino':         ['filipino_drama'],
    'philippine':       ['filipino_drama'],
    'filipino_drama':   ['filipino_drama'],
    'japanese':         ['japanese_drama'],
    'japanese_drama':   ['japanese_drama'],
    'anime':            ['anime'],
    'foreign':          ['foreign'],
    'foreign_series':   ['foreign_series'],
    'wrestling':        ['wrestling'],
    'ongoing':          ['ongoing'],
    'all':              [d['key'] for d in CATEGORY_DEFINITIONS],
}

_SLUG_TO_DEF = {d['slug']: d for d in CATEGORY_DEFINITIONS}
_KEY_TO_DEF  = {d['key']:  d for d in CATEGORY_DEFINITIONS}


# ── Ad / skip domains ─────────────────────────────────────────
# Sourced from 9jarocks.net page HTML (associationfoam, obqj2, etc.)

AD_DOMAINS = [
    'associationfoam.com', 'obqj2.com', 'cranialhubbed.com',
    'admiredjumper.com', 'getdirectbonus.com', 'push-sdk.com',
    'go.getdirectbonus.com', 't.me/naijarockss', 'push-sdk.com',
    'cloudflareinsights.com', 'googletagmanager.com',
]

KNOWN_DOWNLOAD_DOMAINS = [
    'mega.nz', 'drive.google.com', 'mediafire.com', 'pixeldrain.com',
    'terabox.com', 'gofile.io', 'mixdrop.co', 'streamtape.com',
    'doodstream.com', 'filemoon.sx', 'loadedfiles.org', 'netnaijafiles.xyz',
    'sabishares.com', 'meetdownload.com', 'webloaded.com.ng', 'wideshares.org',
    'downloadwella.com', 'netnaija.com', 'fzmovies.net', 'o2tvseries.com',
    'sojuoppa.com', 'dramabus.tv', 'my9jatv.com', 'yts.mx', 'yts.am',
    'nkirifiles.com', 'dl.', 'archive.org', 'onedrive.live.com',
    'my9jarocks.wf',   # 9jarocks mirror domain
]

FILE_EXTENSIONS = ['.mp4', '.mkv', '.avi', '.mov', '.zip', '.rar', '.srt']

DOWNLOAD_KEYWORDS = [
    'download', '480p', '540p', '720p', '1080p', '4k', 'hd', 'episode',
    'fast server', 'slow server', 'mirror', 'part ', 'batch',
]

# In-memory WP category cache  name → ID
_wp_category_cache: dict = {}


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    return unquote(f"{parsed.scheme}://{parsed.netloc}{parsed.path}").lower()


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


# ══════════════════════════════════════════════════════════════
# LISTING PAGE / PAGINATION HELPERS
# ══════════════════════════════════════════════════════════════

def _normalise_9jarocks_url(href: str) -> str:
    """Strip www. prefix so https://www.9jarocks.net/... → https://9jarocks.net/..."""
    return re.sub(r'^(https?://)www\.', r'\1', href, count=1)


def _is_post_url(href: str) -> bool:
    """
    9jarocks post URLs follow the pattern:
      https://9jarocks.net/videodownload/<slug>-id<number>.html
    Also accepts https://www.9jarocks.net/... (www. variant).
    """
    href = _normalise_9jarocks_url(href)
    if not href.startswith(SITE_URL):
        return False
    path = href[len(SITE_URL):]
    if not path or path == '/':
        return False
    skip = (
        '/category/', '/tag/', '/page/', '/wp-', '/feed', '/author/',
        '/search/', '?', '#', '/sitemap', '.xml', '.php',
        '/a-z', '/about', '/privacy', '/terms', '/advertisement',
        '/requests-upload-center', '/staff-pick', '/user',
        '/tech/', '/findx', '/date/',
    )
    if any(s in path for s in skip):
        return False
    # Must be under /videodownload/ and end with .html
    if '/videodownload/' not in path:
        return False
    if not path.endswith('.html'):
        return False
    return True


def get_post_urls_from_listing_page(html: str, base_url: str) -> list:
    soup  = BeautifulSoup(html, 'html.parser')
    links = set()

    # 9jarocks uses <article> tags with post links
    for article in soup.find_all('article'):
        for a in article.find_all('a', href=True):
            href = a['href'].strip().rstrip('/')
            # Restore .html if rstrip removed it (it won't — but safety)
            if _is_post_url(href):
                links.add(href)

    # Fallback: scan all anchors
    if not links:
        for a in soup.find_all('a', href=True):
            href = a['href'].strip()
            if _is_post_url(href):
                links.add(href)

    return list(links)


def has_next_page(html: str) -> bool:
    """
    9jarocks uses wp-pagenavi or standard next-page links.
    """
    soup = BeautifulSoup(html, 'html.parser')
    # wp-pagenavi
    navi = soup.find(class_='wp-pagenavi')
    if navi:
        next_a = navi.find('a', class_='nextpostslink')
        if next_a:
            return True
    # Generic next link
    for a in soup.find_all('a', href=True):
        text = a.get_text(strip=True).lower()
        cls  = ' '.join(a.get('class', []))
        if (
            text in ('next', '»', 'next page', '›') or
            'next' in cls or 'nextpostslink' in cls or
            'page-numbers next' in cls or 'next page-numbers' in cls
        ):
            return True
    return False


# ══════════════════════════════════════════════════════════════
# POST PAGE PARSER  —  9jarocks.net / Jannah theme
# ══════════════════════════════════════════════════════════════

def parse_post_page(html: str, url: str) -> dict | None:
    """
    Parse a single 9jarocks.net post page.

    9jarocks post structure (from the Wingman example):
      - og:title / og:description / og:image  meta tags
      - <h1 class="post-title entry-title">
      - <div class="entry-content entry">
        - Italic SEO keyword line (filename, resolution keywords)
        - Poster <img>
        - Synopsis <p>
        - <p><strong>VIDEO INFORMATION</strong></p>
        - <blockquote> with Filename, Filesize, Duration, Imdb, Title, Year,
                        Type, Country, Language, Director, Genre, Stars, Subtitle
        - <p><strong>TRAILER</strong></p> + <iframe> (YouTube embed)
        - <p><strong>DOWNLOAD LINKS</strong></p>
        - <a class="fa-fa-download" href="...">DOWNLOAD FAST SERVER</a>
        - <a class="fa-fa-download" href="...">DOWNLOAD</a>
        - Screenshot <img>

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
        # Strip site name suffix: "Wingman (2025) – 9jarocks" or "... - 9jarocks"
        title_raw = re.sub(
            r'\s*[|\-–]\s*(9jarocks.*|9JAROCKS.*|Mp4\s+Mkv\s+Download.*)$',
            '', title_raw, flags=re.IGNORECASE
        ).strip()
        title_raw = re.sub(r'^DOWNLOAD\s+', '', title_raw, flags=re.IGNORECASE).strip()

    if not title_raw or len(title_raw) < 4:
        h1 = (
            soup.find('h1', class_='post-title') or
            soup.find('h1', class_='entry-title') or
            soup.find('h1', class_='single-post-title') or
            soup.find('h1')
        )
        if h1:
            title_raw = h1.get_text(strip=True)

    if not title_raw or len(title_raw) < 4:
        title_tag = soup.find('title')
        if title_tag:
            title_raw = re.sub(
                r'\s*[|\-–]\s*(9jarocks|9JAROCKS|Mp4\s+Mkv\s+Download).*$',
                '', title_tag.get_text(strip=True), flags=re.IGNORECASE
            ).strip()

    if not title_raw or len(title_raw) < 4:
        return None

    # ── Categories (from breadcrumb / post-cat spans) ────────────
    categories = []
    for a in soup.find_all('a', class_=re.compile(r'post-cat', re.I)):
        name = a.get_text(strip=True)
        if name and name.lower() not in ('uncategorized', 'video'):
            categories.append(name)
    # Also grab from rel="category tag" anchors
    for a in soup.find_all('a', rel=True):
        rels = a.get('rel', [])
        if isinstance(rels, str):
            rels = rels.split()
        if 'category' in rels or 'tag' in rels:
            name = a.get_text(strip=True)
            if name and name.lower() not in ('uncategorized', 'video') and name not in categories:
                categories.append(name)

    # ── Content div ──────────────────────────────────────────────
    content_div = (
        soup.find('div', class_='entry-content') or
        soup.find('div', class_='entry') or
        soup.find('div', class_='post-content') or
        soup.find('article') or
        soup.find('div', id='content') or
        soup.find('body') or
        soup
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
            # Skip tiny / icon images
            w = img.get('width', '0')
            try:
                if int(str(w).replace('px', '')) < 80:
                    continue
            except ValueError:
                pass
            # Skip screenshot thumbnail (comes after download section)
            alt = (img.get('alt') or '').lower()
            if 'screenshot' in alt or 'thumb' in alt:
                continue
            image_url = src
            break

    # ── Video / Trailer ──────────────────────────────────────────
    # 9jarocks embeds YouTube via <iframe> inside entry-content.
    # Rocket Loader may obfuscate the type attribute but src stays intact.
    video_url = ''
    yt_domains = ['youtube.com/embed', 'youtu.be', 'youtube-nocookie.com']

    # Search all iframes — check both src and data-src (Rocket Loader)
    for _iframe in soup.find_all('iframe'):
        _src = (_iframe.get('src') or _iframe.get('data-src') or '').strip()
        if _src and any(d in _src for d in yt_domains):
            video_url = _src
            break

    # Fallback: search raw HTML for YouTube embed URLs in case BS4 missed it
    if not video_url:
        _yt_re = re.search(
            r'(?:src|data-src)=["\']([^"\']*(?:youtube\.com/embed|youtu\.be)[^"\']*)["\']',
            html
        )
        if _yt_re:
            video_url = _yt_re.group(1).strip()

    # Last resort: any iframe src in content div
    if not video_url and content_div:
        _iframe = content_div.find('iframe')
        if _iframe:
            video_url = (_iframe.get('src') or _iframe.get('data-src') or '').strip()

    # ── Description / Synopsis ───────────────────────────────────
    # On 9jarocks the synopsis is a plain <p> tag immediately after the poster
    # image and before the VIDEO INFORMATION block.
    description = ''

    # Strategy 1: og:description
    og_desc = soup.find('meta', property='og:description')
    if og_desc:
        description = og_desc.get('content', '').strip()

    # Strategy 2: first substantial <p> in content that isn't metadata/SEO
    if not description and content_div:
        for p in content_div.find_all('p'):
            text = p.get_text(strip=True)
            if not text or len(text) < 50:
                continue
            if re.search(r'https?://', text):
                continue
            if re.match(
                r'^(mp4|mkv|download|filename|filesize|duration|imdb|title|year|type|'
                r'country|language|director|genre|stars|subtitle|video\s+information|'
                r'trailer|download\s+links|screenshot)',
                text, re.IGNORECASE
            ):
                continue
            # Skip the italic SEO keyword line that starts with "Mp4 Download ..."
            if re.match(r'^mp4\s+download\s+', text, re.IGNORECASE):
                continue
            description = text[:800]
            break

    if description and len(description) > 800:
        description = description[:800].rsplit(' ', 1)[0] + '...'

    # ── Metadata from VIDEO INFORMATION blockquote ───────────────
    # 9jarocks stores video info inside a <blockquote> as line-separated
    # "Key: Value" text.  Example from the Wingman post:
    #   Filename: Wingman.2025.540p.X265.AAC.[9jaRocks.Com].mkv
    #   Filesize: 237.23 MB
    #   Duration: 100 min
    #   Imdb: https://www.imdb.com/title/tt1724996
    #   Title: Wingman   Year: 2025   Type: Movie   Country: ...
    meta = {}
    if content_div:
        bq = content_div.find('blockquote')
        if bq:
            bq_text = bq.get_text('\n')
            for line in bq_text.splitlines():
                line = line.strip()
                if ':' in line:
                    key, _, val = line.partition(':')
                    k = key.strip().lower()
                    v = val.strip()
                    # For IMDB the value starts with https:// — capture the link
                    if k == 'imdb' and not v:
                        # Try getting the href from the anchor inside blockquote
                        imdb_a = bq.find('a', href=re.compile(r'imdb\.com', re.I))
                        if imdb_a:
                            v = imdb_a['href'].strip()
                    if k and v and len(k) < 40:
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

    # Capture IMDB link from blockquote anchor if not already in meta
    if 'imdb' not in meta and content_div:
        bq = content_div.find('blockquote')
        if bq:
            imdb_a = bq.find('a', href=re.compile(r'imdb\.com', re.I))
            if imdb_a:
                meta['imdb'] = imdb_a['href'].strip()

    # ── Download links ───────────────────────────────────────────
    # 9jarocks uses <a class="fa-fa-download"> for download buttons.
    # There are also ad-redirect links (associationfoam.com) which we skip.

    _SKIP_BTN = {
        "can't download?", "cant download?", "cant download",
        "how to download", "how to download?",
        "report broken link", "report link", "report broken",
        "request movie", "request a movie",
        "subscribe", "follow us", "join us",
        "leave a comment", "share", "recommended",
        "notify me", "get notified",
        "click here", "click here!", "how to download from this site",
    }
    _SKIP_HREF_FRAGS = [
        'how-to-download', 'how_to_download', '/faq', '/help',
        'report-broken', 'request-movie', 'cant-download',
        'requests-upload-center', '/tech/',
    ]

    # ── Ad / fast-server redirect domains to skip ─────────────
    _FAST_SERVER_SKIP = [
        'associationfoam.com', 'obqj2.com', 'cranialhubbed.com',
        'admiredjumper.com', 'getdirectbonus.com',
    ]

    download_links = []
    seen_urls = set()

    if content_div:
        for a in content_div.find_all('a', href=True):
            href      = a.get('href', '').strip()
            btn_text  = a.get_text(strip=True) or 'Download'
            href_lower = href.lower()
            btn_lower  = btn_text.lower().strip().rstrip('?')

            if not href or href.startswith('#') or 'javascript' in href_lower:
                continue
            if btn_lower in _SKIP_BTN:
                continue
            if any(frag in href_lower for frag in _SKIP_HREF_FRAGS):
                continue
            # Skip fast-server ad redirects (associationfoam etc.) — not real download links
            if any(ad in href_lower for ad in _FAST_SERVER_SKIP):
                continue
            if any(ad in href_lower for ad in AD_DOMAINS):
                continue
            if any(skip in href_lower for skip in [
                'facebook.com', 'twitter.com', 't.me/', 'youtube.com/watch?',
                'imdb.com', 'wp-admin', '#respond', 'mailto:',
                '9jarocks.net/category/', '9jarocks.net/tag/',
                '9jarocks.net/page/', '9jarocks.net/a-z',
                'tiktok.com', 'x.com/', 'telegram.org',
                'googletagmanager', 'cloudflare',
            ]):
                continue
            # ── CRITICAL: block ALL 9jarocks internal post URLs ────────
            # 9jarocks posts live at /videodownload/<slug>-id<n>.html
            # The word "download" in that path would otherwise fool the
            # is_dl check below into treating them as real file links.
            if '9jarocks.net' in href_lower and '/videodownload/' in href_lower:
                continue
            # Also block any other 9jarocks.net internal link that isn't
            # pointing to a known external file host
            if href_lower.startswith(SITE_URL.lower()):
                continue

            # 9jarocks download buttons have class "fa-fa-download" —
            # but ONLY treat as definitive when the URL is an external file host
            # (internal 9jarocks URLs are already blocked above, so this is a
            # belt-and-braces guard for any edge-cases that still reach here)
            a_classes = ' '.join(a.get('class', []))
            _is_external_host = '9jarocks.net' not in href_lower
            is_download_btn = 'fa-fa-download' in a_classes and _is_external_host

            is_dl = is_download_btn or (
                any(d in href_lower for d in KNOWN_DOWNLOAD_DOMAINS)
                or any(href_lower.endswith(ext) for ext in FILE_EXTENSIONS)
                or any(kw in btn_lower for kw in DOWNLOAD_KEYWORDS)
                or any(kw in href_lower for kw in ['/dl/', '/get/', '/file/', 'mirror'])
            )

            if is_dl and href not in seen_urls:
                seen_urls.add(href)
                # Try to find episode label from preceding <em> tag
                # 9jarocks pattern: <em>EPISODE 1</em><br/><a ...>[SERVER 1]</a>
                ep_label = ''
                try:
                    prev_p = a.find_parent('p')
                    if prev_p:
                        prev_sib = prev_p.find_previous_sibling('p')
                        if prev_sib:
                            em = prev_sib.find('em')
                            if em:
                                ep_label = em.get_text(strip=True)
                    # Also check inline em in same parent
                    if not ep_label and a.parent:
                        for sib in a.parent.children:
                            if sib == a:
                                break
                            if hasattr(sib, 'name') and sib.name == 'em':
                                ep_label = sib.get_text(strip=True)
                except Exception:
                    pass
                label = btn_text.strip() or 'DOWNLOAD'
                # Derive a reliable episode label from the URL filename.
                # 9jarocks uses multiple naming patterns:
                #   Standard  : S01E02  → season 1 episode 2
                #   No-E form : S0102   → season 01, episode 02 (2-digit each)
                #               S0103   → season 01, episode 03
                # We ALWAYS prefer the URL filename over the <em> tag because
                # 9jarocks often copies the wrong <em> label (e.g. "EPISODE 1"
                # for what is actually episode 2, 3, etc.).
                _fname = href.rstrip('/').split('/')[-1]
                _zip_url = re.search(r'[Ss](\d+).*\.zip', _fname, re.IGNORECASE)
                # Pattern 1: SxxExx  (standard — must check first)
                _se = re.search(r'[Ss](\d+)[Ee](\d+)', _fname)
                # Pattern 2: Sxxyy where xx=season(2 digits), yy=episode(2 digits)
                # e.g. S0102 → S01 E02,  S0103 → S01 E03
                # Only match when there is NO 'E' between the two digit groups.
                _se_noe = re.search(r'[Ss](\d{2})(\d{2})(?!\d)', _fname) if not _se else None

                if _se:
                    _sn, _en = int(_se.group(1)), int(_se.group(2))
                    ep_label = f'S{_sn:02d}E{_en:02d}' if _sn > 1 else f'E{_en:02d}'
                elif _se_noe:
                    _sn, _en = int(_se_noe.group(1)), int(_se_noe.group(2))
                    ep_label = f'S{_sn:02d}E{_en:02d}' if _sn > 1 else f'E{_en:02d}'
                elif _zip_url:
                    ep_label = f'ZIP S{int(_zip_url.group(1)):02d}'
                # keep existing ep_label from <em> if URL gave nothing
                download_links.append({'url': href, 'label': label, 'ep_label': ep_label})
                print(f"   🔗 [{ep_label or 'MOVIE'}] {label} → {href}")

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
        'vi_language': _mv(['language', 'audio', 'en']),
        'vi_subtitle': _mv(['subtitle', 'subtitles', 'sub']),
        'vi_genre':    _mv(['genre', 'genres', 'category']),
        'vi_cast':     _mv(['stars', 'cast', 'actors', 'starring']),
        'vi_director': _mv(['director', 'directed by', 'directors']),
        'vi_episodes': _mv(['episodes', 'episode', 'total episodes', 'no of episodes']),
        'vi_status':   _mv(['status', 'series status']),
        'vi_runtime':  _mv(['running time', 'runtime', 'duration', 'run time', 'duration']),
        'vi_filesize': _mv(['filesize', 'file size', 'size', 'file',
                            'download size', 'video size']),
        'vi_filename': _mv(['filename', 'file name']),
        'vi_type':     _mv(['type']),
        'vi_imdb':     _mv(['imdb']),
    }

    # Fallback: pull year from title if not in metadata
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
        'source_url':     url,          # original 9jarocks post URL — used for dedup/update
        'meta':           meta,
        **vi,
    }


# ══════════════════════════════════════════════════════════════
# TITLE CLEANING
# ══════════════════════════════════════════════════════════════

def clean_title_parts(raw: str):
    """
    Returns (title, title_b, is_series).

    Series  : title = "Show Name S01",  title_b = "Episode 5 Added"
    Movie   : title = "Movie Name (2024)", title_b = ""
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
        base        = match.group(1).strip()
        rest        = match.group(3).strip()
        # rest may look like "(Episode 9-10 Added) (Chinese Drama)" —
        # extract only the first parenthesised episode block if present,
        # otherwise take the whole rest (stripped of outer parens).
        ep_block = re.match(r'[\(\[](Episode\s*[\d\s\-–—]+(?:Added)?|Complete[d]?)[\)\]]',
                            rest, re.IGNORECASE)
        if ep_block:
            title_b = ep_block.group(1).strip()
        else:
            # Strip outer parens and any trailing "(Category)" suffix
            title_b = re.sub(r'^\(|\)$', '', rest).strip()
            title_b = re.sub(r'\s*\([^)]{2,30}\)\s*$', '', title_b).strip()
        if is_complete and 'complete' not in base.lower() and 'complete' not in title_b.lower():
            base += ' (Completed)' if 'completed' in title_lower else ' (Complete)'
        return base, title_b, True

    # 9jarocks often puts episode info in parens: "Show (Episode 3 Added)"
    ep_in_paren = re.search(r'^(.*?)\s*\((Episode\s*\d+.*?)\)\s*$', title, re.IGNORECASE)
    if ep_in_paren:
        return ep_in_paren.group(1).strip(), ep_in_paren.group(2).strip(), True

    # Movie with year
    movie_match = re.search(r'^(.*?\(\d{4}\))', title)
    if movie_match:
        return movie_match.group(1).strip(), '', False

    return title, '', False


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


# ── 9jarocks source category name → NaijaDeleys target category name ──────────
# This map is the single source of truth.  The keys are the exact strings that
# come from the 9jarocks source (og:title category labels + CATEGORY_DEFINITIONS
# wp_cat values).  The values are the EXACT category names on naijadeleys.com.ng
# as seen in the WP editor (document index 7 / 2).
#
# Rule: if a name is not in this map the function does a live WP search, and if
# that still fails it uses the fallback  "TV Series" / "Movie"  (both of which
# exist on NaijaDeleys).
_NAIJADELEYS_CAT_MAP: dict[str, str] = {
    # ── Hollywood ──────────────────────────────────────────────────────────────
    "hollywood movie":                    "Hollywood movie",
    "hollywood tv series":                "Hollywood Series",
    "hollywood series":                   "Hollywood Series",
    "hollywood tv series":                "Hollywood Series",
    # ── Nollywood ─────────────────────────────────────────────────────────────
    "nollywood movie":                    "Nollywood movie",
    "nollywood tv series":                "Nollywood tv series",
    "nollywood":                          "Nollywood movie",
    # ── Asian Dramas ──────────────────────────────────────────────────────────
    "korean drama":                       "Korean Drama",
    "kdrama":                             "Korean Drama",
    "k-drama":                            "Korean Drama",
    "chinese drama":                      "Chinese drama",
    "cdrama":                             "Chinese drama",
    "thai drama":                         "Thai drama",
    "filipino drama":                     "Filipino Drama",
    "philippine drama":                   "Filipino Drama",
    "japanese drama":                     "Japanese drama",
    "turkish drama":                      "Turkish Drama",
    "spanish drama":                      "Spanish drama",
    # ── Anime ─────────────────────────────────────────────────────────────────
    "anime":                              "Anime",
    "chinese anime":                      "Anime",
    "japanese anime":                     "Anime",
    # ── Other Foreign ─────────────────────────────────────────────────────────
    "other foreign movies":               "Other foreign movies",
    "foreign movies":                     "Other foreign movies",
    "other foreign series":               "Other Foreign Series",
    "foreign series":                     "Other Foreign Series",
    "foreign":                            "Other foreign movies",
    # ── Sports / Wrestling ────────────────────────────────────────────────────
    "pro wrestling & fighting sports":    "Wrestling",
    "pro wrestling":                      "Wrestling",
    "wrestling":                          "Wrestling",
    # ── Ongoing / other ───────────────────────────────────────────────────────
    "ongoing":                            "Ongoing",
    "ongoing series":                     "Ongoing",
    # ── Genres (used as fallback WP categories on NaijaDeleys) ────────────────
    "action":                             "Action",
    "adventure":                          "Adventure",
    "animation":                          "Animation",
    "biography":                          "Biography",
    "comedy":                             "Comedy",
    "crime":                              "Crime",
    "documentary":                        "Documentary",
    "drama":                              "TV Series",      # "Drama" → TV Series
    "entertainment":                      "Entertainment",
    "family":                             "Family",
    "fantasy":                            "Fantasy",
    "history":                            "History",
    "horror":                             "Horror",
    "mystery":                            "Mystery",
    "reality-tv":                         "Reality-tv",
    "romance":                            "Romance",
    "sci-fi":                             "Sci-fi",
    "thriller":                           "Thriller",
    "war":                                "War",
    "western":                            "Western",
    # ── Fallback safety nets ──────────────────────────────────────────────────
    "movie":                              "Movie",
    "movies":                             "Movie",
    "tv series":                          "TV Series",
    "series":                             "TV Series",
    "hollywood":                          "Hollywood movie",
}


def _wp_get_or_create_category(cat_name: str, headers: dict, wp_base: str,
                                is_series: bool = False) -> int | None:
    """
    Resolve cat_name → WP category ID on the target site (naijadeleys.com.ng).

    Resolution order:
      1. Hardcoded _NAIJADELEYS_CAT_MAP  (instant, no network call needed)
      2. Live WP category search         (handles any future categories added to the site)
      3. Fallback to "TV Series" / "Movie" — both guaranteed to exist on NaijaDeleys
    """
    raw = cat_name.strip()
    if not raw:
        raw = "TV Series" if is_series else "Movie"

    # ── Step 1: hardcoded map lookup (case-insensitive) ─────────────────────
    mapped = _NAIJADELEYS_CAT_MAP.get(raw.lower(), raw)

    key = mapped.strip().lower()
    if key in _wp_category_cache:
        return _wp_category_cache[key]

    # ── Step 2: live WP search ───────────────────────────────────────────────
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

        # ── Step 3: fallback ─────────────────────────────────────────────────
        fallback = "TV Series" if is_series else "Movie"
        print(f"    ⚠️ Category '{mapped}' not found on NaijaDeleys → fallback to '{fallback}'")
        if mapped.strip().lower() == fallback.strip().lower():
            # Avoid infinite recursion if fallback itself is missing
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


def _wp_find_by_source_url(source_url: str, headers: dict, wp_base: str) -> dict | None:
    """
    Query the target WP site for a post whose custom meta field
    '_9jarocks_source_url' matches *source_url*.

    This is the primary duplicate / update detection mechanism.
    It is reliable even when the post title changes (e.g. new episode added).

    Requires the WP REST API to expose custom post meta — ensure your WP
    theme/plugin exposes 'meta' in the posts endpoint, or add this to
    functions.php:
        register_post_meta('post', '_9jarocks_source_url', [
            'show_in_rest' => true, 'single' => true, 'type' => 'string',
        ]);
    """
    if not source_url:
        return None
    # Normalise: strip www. so stored values and queried values always match
    source_url = _normalise_9jarocks_url(source_url)
    try:
        # Request meta fields so we can verify the match ourselves.
        # NOTE: WP's default REST API silently ignores meta_key/meta_value
        # filtering unless a plugin is installed.  We therefore fetch candidates
        # and manually verify the meta field rather than trusting server-side filter.
        r = requests.get(
            f'{wp_base}/wp-json/wp/v2/posts',
            params={
                'meta_key':   '_9jarocks_source_url',
                'meta_value': source_url,
                'per_page':   5,
                'status':     'any',
                '_fields':    'id,title,slug,categories,meta',
                'context':    'edit',   # required to get meta in response
            },
            headers=headers, timeout=10,
        )
        if r.status_code == 200:
            for post in r.json():
                # Verify the meta actually matches — WP often ignores the
                # meta_key/meta_value filter and returns unrelated recent posts.
                stored = ''
                post_meta = post.get('meta', {})
                if isinstance(post_meta, dict):
                    stored = post_meta.get('_9jarocks_source_url', '') or ''
                stored = _normalise_9jarocks_url(stored.strip())
                if stored == source_url:
                    print(f"    🔎 Found existing post by source URL (ID {post['id']})")
                    return post
        # meta_key filter didn't work or returned no verified match.
        # Try a wider scan: fetch the 50 most recent posts and check meta.
        # This is slower but works even without plugin support for meta filtering.
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
                    stored = post_meta.get('_9jarocks_source_url', '') or ''
                stored = _normalise_9jarocks_url(stored.strip())
                if stored == source_url:
                    print(f"    🔎 Found existing post by meta scan (ID {post['id']})")
                    return post
    except Exception as exc:
        print(f"    ⚠️ WP source-URL lookup error: {exc}")
    return None


def _strip_episode_suffix(text: str) -> str:
    """
    Strip episode/complete suffix from a title so we can match across episode updates.
    e.g. "Double Helix Season 1 (Episode 8 Added) (Chinese Drama)" →
         "Double Helix Season 1"
    Also strips trailing parenthesised words that follow the episode info
    (like "(Chinese Drama)" in the example above).
    """
    # Strip (Episode X Added), (Episode X-Y Added), (Complete), (Completed)
    text = re.sub(
        r'\s*[\(\[]?\s*(?:episode\s*[\d\s\-–—]+(?:added)?|complete[d]?)\s*[\)\]]?'
        r'(?:\s*[\(\[][^\)\]]*[\)\]])*\s*$',
        '', text, flags=re.IGNORECASE
    ).strip()
    # Strip trailing pipe
    text = re.sub(r'\s*\|.*$', '', text).strip()
    return text


def _wp_find_existing_post(title: str, headers: dict, wp_base: str,
                           is_series: bool = False) -> dict | None:
    """
    Fallback title-based search — used only when no source URL match found.

    Matching rules (strictest first):
      1. Exact match on the rendered post title  (case-insensitive)
      2. The search title (with "(Complete/Completed)" stripped) exactly matches
         the bare rendered title (our publisher appends "| Mp4 Mkv DOWNLOAD").
      3. Complete-stripped bare title exact match.
      4. SERIES ONLY — base title match (episode suffix stripped from both sides).
         Handles the case where an existing post has a different episode count,
         e.g. "Double Helix Season 1 (Episode 8 Added)" matched by scraping
         "Double Helix Season 1 (Episode 9-10 Added)".

    IMPORTANT: WP full-text search is fuzzy — e.g. searching "UFC Fight Night X"
    can return older "UFC Fight Night Y" posts.  We ONLY accept a result when the
    stored title (after stripping our suffix) exactly equals the search title.
    """
    search_title = re.sub(r'\s*\(Complet(?:e|ed)\)\s*$', '', title, flags=re.IGNORECASE).strip()
    # For the WP search query use only the base title (no episode info, no year)
    # so we always get candidate posts back regardless of episode number.
    base_title_for_query = _strip_episode_suffix(search_title)
    base_title_for_query = re.sub(r'\s*\(\d{4}\)\s*$', '', base_title_for_query).strip()
    # Use the base title as the search query — broad enough to find all episode variants
    search_query = base_title_for_query
    try:
        r = requests.get(
            f'{wp_base}/wp-json/wp/v2/posts',
            params={
                'search':   search_query,
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
            # Strip the "| Mp4 Mkv DOWNLOAD" suffix our publisher appends
            rendered_bare = re.sub(
                r'\s*\|\s*mp4\s+mkv\s+download\s*$', '', rendered, flags=re.IGNORECASE
            ).strip()
            rendered_bare_no_complete = re.sub(
                r'\s*\(complet(?:e|ed)\)\s*$', '', rendered_bare, flags=re.IGNORECASE
            ).strip()
            rendered_base = _strip_episode_suffix(rendered_bare)

            # Rule 1: exact full-title match (re-running same scrape)
            matched    = rendered in (title_lower, search_lower)
            match_rule = 'exact full title'
            # Rule 2: bare title exact match
            if not matched and rendered_bare in (title_lower, search_lower):
                matched    = True
                match_rule = 'bare title'
            # Rule 3: complete-stripped bare title exact match
            if not matched and rendered_bare_no_complete == search_lower:
                matched    = True
                match_rule = 'complete-stripped'
            # Rule 4: series base-title match (episode suffix stripped from both)
            if not matched and is_series and base_search_lower and rendered_base == base_search_lower:
                matched    = True
                match_rule = 'series base title'

            if not matched:
                continue

            # ── Extra guard: if this post already has a DIFFERENT source URL
            # stored in its meta, it belongs to a different 9jarocks post —
            # reject it (WP fuzzy-search false positive).
            post_meta  = post.get('meta', {})
            stored_src = ''
            if isinstance(post_meta, dict):
                stored_src = _normalise_9jarocks_url(
                    (post_meta.get('_9jarocks_source_url') or '').strip()
                )
            title_slug_words = set(re.sub(r'[^a-z0-9]', ' ', base_search_lower or title_lower).split())
            if stored_src:
                stored_slug_words = set(re.sub(r'[^a-z0-9]', ' ', stored_src.lower()).split())
                overlap = title_slug_words & stored_slug_words
                if len(overlap) < 3:
                    print(f"    ⚠️  Title match (ID {post['id']}) rejected — belongs to different source URL")
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
        # Strip any episode-count / complete suffix so the slug is stable
        # across all future episode updates.
        # Handles patterns like:
        #   (Episode 1 Added)          (Episode 1 – 3 Added)
        #   (Episode 5)                (Complete) / (Completed)
        #   Episode 3 Added            [Episode 2 Added]
        text = re.sub(
            r'\s*[\(\[]?\s*(?:episode\s*[\d\s\-––]+(?:added)?|complete[d]?)\s*[\)\]]?\s*$',
            '', text, flags=re.IGNORECASE
        ).strip()
        # Also strip trailing pipe and everything after
        text = re.sub(r'\s*\|.*$', '', text).strip()

    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    text = text.lower()
    text = re.sub(r"[`']+", '', text)
    text = re.sub(r'[^a-z0-9]+', '-', text)
    return text.strip('-')


# ══════════════════════════════════════════════════════════════
# WP CONTENT BUILDER  —  matching 9jarocks / Jannah design
# ══════════════════════════════════════════════════════════════

def _build_wp_content(title: str, title_b: str, description: str,
                      meta_info: dict, image_url: str, video_url: str,
                      download_links: list, is_series: bool,
                      wp_image_url: str = '') -> str:
    """
    Build the HTML post body matching the 9jarocks.net manual-post design.

    Layout (mirrors the Wingman post structure):
      0.  SEO keyword italic line  (Mp4 Download … Mkv Download)
      0b. Poster image centred
      1.  Synopsis / description paragraph
      2.  VIDEO INFORMATION heading + blockquote card
      3.  TRAILER heading + YouTube iframe
      4.  DOWNLOAD LINKS heading + VLC tip box + download buttons
      5.  SCREENSHOT placeholder (if available)
    """
    parts = []

    # Detect content region
    _cat_text    = ' '.join(str(v) for v in meta_info.values()).lower()
    _is_nollywood = any(x in _cat_text for x in ('nollywood', 'nigerian', 'nigeria'))
    _is_anime     = any(x in _cat_text for x in ('anime',))

    # Pull year early
    year = meta_info.get('vi_year', meta_info.get('year', '')).strip()
    if not year:
        _yr_m = re.search(r'\((\d{4})\)', title)
        if _yr_m:
            year = _yr_m.group(1)

    _title_no_yr = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()
    yr_str       = f' ({year})' if year else ''
    base_yr      = f'{_title_no_yr}{yr_str}'

    # Resolution string from filename if available
    _filename = meta_info.get('vi_filename', '').strip()
    _res_match = re.search(r'(540p|480p|720p|1080p)', _filename, re.IGNORECASE)
    _res       = _res_match.group(1) if _res_match else '720p 480p'

    # ── 0. SEO keyword italic line ────────────────────────────────
    # Mirrors the <i> tag 9jarocks uses at the top of each post
    if is_series:
        seo_text = (
            f'Mp4 Download {base_yr} {title_b}, '
            f'{base_yr}, x265 x264 , torrent , HD bluray popcorn, '
            f'magnet {base_yr} mkv Download'
        )
    else:
        seo_text = (
            f'Mp4 Download {base_yr} {_res} , {base_yr} , '
            f'x265 x264 , torrent , HD bluray popcorn, '
            f'magnet {base_yr} mkv Download'
        )
    parts.append(f'<p><i>{seo_text}</i></p>')

    # ── 0b. Poster image ──────────────────────────────────────────
    _inline_img_src = wp_image_url or image_url
    if _inline_img_src:
        safe_title = title.replace('"', '&quot;')
        parts.append(
            f'<p style="text-align:center;">'
            f'<img decoding="async" src="{_inline_img_src}" '
            f'class="aligncenter size-full" alt="{safe_title}" /></p>'
        )
    
    if is_series and title_b:
        dl_head = f'DOWNLOAD {title} ({title_b}) | Free DOWNLOAD Mp4'
    elif year and f'({year})' not in title:
        dl_head = f'DOWNLOAD {title} ({year}) | Free DOWNLOAD Mp4'
    else:
        dl_head = f'DOWNLOAD {title} | Free DOWNLOAD Mp4'
    parts.append(f'<p><strong>{dl_head}</strong></p>')

    # ── 1. Synopsis ───────────────────────────────────────────────
    if description:
        # Strip any content from "VIDEO INFORMATION" onward — the og:description
        # or fallback <p> sometimes bleeds into the metadata block
        _vi_cut = re.split(r'video\s+information', description, maxsplit=1, flags=re.IGNORECASE)
        description = _vi_cut[0].strip().rstrip('–—-|:,').strip()
        if description:
            parts.append(f'<p>{description}</p>')

    # ── 2. VIDEO INFORMATION blockquote ──────────────────────────
    filesize = meta_info.get('vi_filesize', meta_info.get('filesize', '')).strip()
    dur      = meta_info.get('vi_runtime',  meta_info.get('duration', '')).strip()
    imdb     = meta_info.get('vi_imdb',     meta_info.get('imdb', '')).strip()
    status   = meta_info.get('vi_status',   meta_info.get('status', '')).strip()
    sub      = meta_info.get('vi_subtitle', meta_info.get('subtitle', '')).strip()
    genre    = meta_info.get('vi_genre',    meta_info.get('genre', '')).strip()
    stars    = meta_info.get('vi_cast',     meta_info.get('stars', '')).strip()
    country  = meta_info.get('vi_country',  meta_info.get('country', '')).strip()
    lang     = meta_info.get('vi_language', meta_info.get('language', '')).strip()
    director = meta_info.get('vi_director', meta_info.get('director', '')).strip()
    total_ep = meta_info.get('vi_episodes', meta_info.get('episodes', '')).strip()
    vi_type  = meta_info.get('vi_type',     '').strip() or ('TV Series' if is_series else 'Movie')

    _title_clean = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()

    info_lines = []
    if filesize:    info_lines.append(f'Filesize: \t{filesize}')
    if dur:         info_lines.append(f'Duration:       {dur}')
    if imdb:
        info_lines.append(
            f'Imdb: <a href="{imdb}" target="_blank" rel="nofollow noopener">{imdb}</a>'
        )
    if _title_clean: info_lines.append(f'Title: {_title_clean}')
    if year:         info_lines.append(f'Year: {year}')
    info_lines.append(f'Type: {vi_type}')
    if country:      info_lines.append(f'Country: {country}')
    if lang:         info_lines.append(f'Language: {lang}')
    if director:     info_lines.append(f'Director: {director}')
    if genre:        info_lines.append(f'Genre:  {genre}')
    if stars:        info_lines.append(f'Stars:  {stars}')
    if total_ep:     info_lines.append(f'Total Episodes: {total_ep}')
    if status:       info_lines.append(f'Status: {status}')
    if sub:          info_lines.append(f'Subtitle: {sub}')

    if info_lines:
        parts.append('<p><strong>VIDEO INFORMATION</strong></p>')
        inner = '<br />\n'.join(info_lines)
        parts.append(f'<blockquote><p>\n{inner}\n</p></blockquote>')

    # ── 3. TRAILER heading + embed ────────────────────────────────
    if video_url:
        parts.append('<p><strong>TRAILER</strong></p>')
        yt_match = re.search(
            r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([\w\-]{11})',
            video_url
        )
        embed_url = f'https://www.youtube.com/embed/{yt_match.group(1)}' if yt_match else video_url
        parts.append(
            f'<div>\n'
            f'<p><iframe title="{title.replace(chr(34), chr(39))} Trailer" '
            f'width="780" height="439" src="{embed_url}" '
            f'frameborder="0" allow="accelerometer; autoplay; clipboard-write; '
            f'encrypted-media; gyroscope; picture-in-picture; web-share" '
            f'referrerpolicy="strict-origin-when-cross-origin" '
            f'allowfullscreen></iframe></p>\n</div>'
        )

    # ── 4. DOWNLOAD LINKS heading + VLC tip + buttons ─────────────
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

    # ── Download buttons — inline-flex, white bg, green border, icons8 download icon ──
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
    # Series buttons use slightly smaller padding to match sample
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
            url        = dl['url']
            link_text  = dl.get('ep_label', '').strip() or dl.get('label', '').strip()

            # Skip broken / missing links
            if not url or not url.startswith('http'):
                continue

            # ZIP: "ZIP S01" → "DOWNLOAD ZIP SEASON 1"
            zip_match = re.search(r'zip\s+s(\d+)', link_text, re.IGNORECASE)
            if zip_match:
                ep_heading = f'DOWNLOAD ZIP SEASON {int(zip_match.group(1))}'

            # SxxExx: "S02E05" → "S2 EPISODE 5" / "S01E03" → "EPISODE 3"
            elif se_match := re.search(r'S(\d+)E(\d+)', link_text, re.IGNORECASE):
                s_num = int(se_match.group(1))
                e_num = int(se_match.group(2))
                ep_heading = f'S{s_num} EPISODE {e_num}' if s_num > 1 else f'EPISODE {e_num}'

            # Exx only: "E03" → "EPISODE 3"
            elif e_match := re.match(r'E(\d+)$', link_text, re.IGNORECASE):
                ep_heading = f'EPISODE {int(e_match.group(1))}'

            # Fallback: extract SxxExx directly from URL
            elif se_match := re.search(r'S(\d+)E(\d+)', url, re.IGNORECASE):
                s_num = int(se_match.group(1))
                e_num = int(se_match.group(2))
                ep_heading = f'S{s_num} EPISODE {e_num}' if s_num > 1 else f'EPISODE {e_num}'

            else:
                ep_heading = link_text or 'DOWNLOAD'

            parts.append(
                f'<div style="margin-bottom:8px;">'
                f'<a style="{_BTN_A_SM}" href="{url}">'
                f'{_BTN_ICON_SM}{ep_heading}</a>'
                f'</div>'
            )

        parts.append('</div>')

    else:
        # Movie: one "DOWNLOAD HERE" button per link
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

def _build_rank_math_seo(title: str, title_b: str, description: str,
                          meta_info: dict, categories: list,
                          is_series: bool) -> dict:
    year    = meta_info.get('vi_year',    meta_info.get('year', '')).strip()
    country = meta_info.get('vi_country', meta_info.get('country', '')).strip()

    cat_lower = ' '.join(c.lower() for c in categories)
    if 'korean' in cat_lower or 'kdrama' in cat_lower or 'k-drama' in cat_lower:
        drama_type = 'Korean'
    elif 'thai' in cat_lower:
        drama_type = 'Thai'
    elif 'chinese' in cat_lower or 'cdrama' in cat_lower:
        drama_type = 'Chinese'
    elif 'japanese' in cat_lower:
        drama_type = 'Japanese'
    elif 'anime' in cat_lower:
        drama_type = 'Anime'
    elif 'bollywood' in cat_lower or 'indian' in cat_lower:
        drama_type = 'Indian'
    elif 'philippine' in cat_lower or 'filipino' in cat_lower:
        drama_type = 'Filipino'
    elif 'nollywood' in cat_lower or 'nigerian' in cat_lower:
        drama_type = 'Nollywood'
    else:
        drama_type = country if country else ''

    is_nollywood = 'nollywood' in cat_lower or 'nigerian' in cat_lower
    is_anime     = 'anime' in cat_lower
    is_drama     = bool(drama_type) and not is_nollywood and is_series
    is_completed = any(x in title.lower() for x in ('complete', 'completed'))

    ep_num   = ''
    ep_match = re.search(r'episode\s*(\d+)', title_b, re.IGNORECASE)
    if ep_match:
        ep_num = ep_match.group(1)

    # Focus keyword
    if is_anime and is_series:
        focus_kw = f'Download {title} Episode {ep_num} Anime' if ep_num else f'Download {title} Anime'
    elif is_anime:
        focus_kw = f'Download {title} ({year}) Anime Movie' if year else f'Download {title} Anime'
    elif is_series and is_drama and is_completed:
        focus_kw = f'Download {title} Complete {drama_type} Drama'
    elif is_series and is_drama:
        focus_kw = (f'Download {title} Episode {ep_num} {drama_type} Drama'
                    if ep_num else f'Download {title} {drama_type} Drama')
    elif is_series and is_completed:
        focus_kw = f'Download {title} Season Complete Series'
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
        seo_title = f'{title} ({title_b}) - 9jarocks'
    elif year and f'({year})' not in title:
        seo_title = f'{title} ({year}) - 9jarocks'
    else:
        seo_title = f'{title} - 9jarocks'

    # Meta description
    if is_series and is_drama and is_completed:
        ep_range     = meta_info.get('vi_episodes', meta_info.get('episodes', ''))
        ep_range_str = f'1 - {ep_range}' if ep_range else 'complete'
        desc = (
            f'{title}, {title} {year} episode {ep_range_str} {drama_type} series download, '
            f'Download {title} complete episodes, '
            f'Download {title} {drama_type} drama in 480p Mkv Mp4, '
            f'DOWNLOAD {title} ({year}) (Complete) | Free DOWNLOAD Mp4, '
            f'DOWNLOAD {title} Complete {drama_type} Drama For FREE In 480p, 720p, 1080p'
        )
    elif is_series and is_drama:
        desc = (
            f'{title}, {title} {year} episode {ep_num} {drama_type} series download, '
            f'Download {title} Episode {ep_num}, '
            f'Download {title} {drama_type} drama in 480p Mkv Mp4, '
            f'DOWNLOAD {title} ({year}) | Free DOWNLOAD Mp4, '
            f'DOWNLOAD {title} Episode {ep_num} {drama_type} Drama For FREE In 480p, 720p, 1080p'
        )
    elif is_series and is_completed:
        ep_range     = meta_info.get('vi_episodes', meta_info.get('episodes', ''))
        ep_range_str = f'Episode 1 - {ep_range} Complete' if ep_range else 'Complete'
        desc = (
            f'{title}, {title} {year} complete series download, '
            f'Download {title} {ep_range_str}, '
            f'Download {title} complete series in 480p Mkv Mp4, '
            f'DOWNLOAD {title} ({year}) Complete | Free DOWNLOAD Mp4, '
            f'DOWNLOAD {title} (Complete) TV Series For FREE In 480p, 720p, 1080p'
        )
    elif is_series:
        desc = (
            f'{title} Episode {ep_num}, {title} Episode {ep_num} {year} series download, '
            f'Download {title} Episode {ep_num}, '
            f'DOWNLOAD {title} ({title_b}) Tv Series | Free DOWNLOAD Mp4, '
            f'DOWNLOAD {title} Episode {ep_num} Tv Series For FREE In 480p, 720p, 1080p'
        )
    elif is_nollywood:
        desc = (
            f'{title} ({year}), Download {title} ({year}) Mp4 Mkv Nigerian Movie, '
            f'Download {title} ({year}) Nollywood Movie, '
            f'DOWNLOAD {title} ({year}) Nollywood Movie | Free DOWNLOAD, '
            f'Download {title} ({year}) Nollywood Movie For FREE In 480p, 720p, 1080p'
        )
    else:
        desc = (
            f'{title} ({year}), {title} ({year}) Movie Download, '
            f'Download {title} ({year}) Movie in 480p 4K Mkv Mp4, '
            f'DOWNLOAD {title} ({year}) | Free DOWNLOAD Mp4, '
            f'DOWNLOAD {title} ({year}) Movie For FREE In 480p, 720p, 1080p'
        )

    return {
        'rank_math_focus_keyword': focus_kw,
        'rank_math_title':         seo_title,
        'rank_math_description':   desc,
    }


# ══════════════════════════════════════════════════════════════
# WORDPRESS POST PUBLISHER
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

        # Full post title
        if is_series and title_b:
            full_title = f'{title} ({title_b}) | Mp4 Mkv DOWNLOAD'
        else:
            full_title = f'{title} | Mp4 Mkv DOWNLOAD'

        excerpt_text = description[:300] if description else ''

        # ── Duplicate / update detection (BEFORE image upload) ────
        # Check WP first so we don't waste time uploading images for
        # movies that already exist, or series with no new episodes.
        source_url    = _normalise_9jarocks_url(meta_info.get('source_url', ''))
        existing_post = (
            _wp_find_by_source_url(source_url, headers, wp_base)
            if source_url else None
        )
        if not existing_post:
            existing_post = _wp_find_existing_post(title, headers, wp_base, is_series=is_series)

        # ── MOVIE: skip immediately, no image upload needed ───────
        if existing_post and not is_series:
            post_id = existing_post['id']
            print(f"    ⏭️  Movie already exists (ID {post_id}) — skipping.")
            return True

        # ── SERIES: check if episode has actually changed ─────────
        if existing_post and is_series:
            post_id       = existing_post['id']
            current_title = BeautifulSoup(
                existing_post['title']['rendered'], 'html.parser'
            ).get_text().strip()

            # If this "series" has no episode info (e.g. a wrestling event, UFC card,
            # or any one-off that 9jarocks categorises under a series category),
            # treat it as a movie — skip entirely, no update needed.
            if not title_b:
                print(f"    ⏭️  Series post with no episode info (ID {post_id}) — treating as movie, skipping.")
                return True

            title_changed = (current_title.strip().lower() != full_title.strip().lower())

            if not title_changed:
                # Same episode count — nothing to update, skip image upload
                print(f"    ⏭️  Series already up to date (ID {post_id}) — no new episode, skipping.")
                return True

            # New episode detected — fall through to image upload + update below
            print(f"    🆕  New episode detected — updating post (ID {post_id})...")

        # ── IMAGE UPLOAD (only reached for new posts or series updates) ──
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

        # Build content & SEO
        content = _build_wp_content(
            title, title_b, description, meta_info,
            image_url, video_url, download_links, is_series,
            wp_image_url=wp_image_url,
        )
        rank_math_meta = _build_rank_math_seo(
            title, title_b, description, meta_info, categories, is_series
        )

        # Resolve WP category ID
        cat_id  = _wp_get_or_create_category(wp_cat_name, headers, wp_base, is_series)
        cat_ids = [cat_id] if cat_id else []

        # ── SERIES: update in place (slug / URL never changes) ───
        if existing_post and is_series:
            post_id = existing_post['id']
            # title_changed is guaranteed True here (same-episode was skipped above)
            patch: dict = {
                'title':   full_title,
                'content': content,
                'meta': {
                    **rank_math_meta,
                    '_9jarocks_source_url': source_url,
                },
            }
            from datetime import datetime, timezone as tz
            now_utc           = datetime.now(tz.utc)
            patch['date']     = now_utc.strftime('%Y-%m-%dT%H:%M:%S')
            patch['date_gmt'] = now_utc.strftime('%Y-%m-%dT%H:%M:%S')
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
                print(f"    ✏️  WP series updated — new episode + date bumped (ID {post_id})")
                print(f"    🔗  Slug unchanged (SEO safe) ← {full_title}")
                return True
            else:
                print(f"    ⚠️ WP update failed: {r.status_code} {r.text[:150]}")
                return False

        # ── CREATE (new post — movie or series) ───────────────────
        post_data: dict = {
            'title':   full_title,
            'slug':    _make_slug(title, is_series=is_series),
            'content': content,
            'status':  'publish',
            'format':  'video',
            'excerpt': excerpt_text or '',
            'meta': {
                **rank_math_meta,
                # Save the original 9jarocks URL so future runs can find
                # this post without relying on title matching.
                '_9jarocks_source_url': source_url,
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
# DJANGO MANAGEMENT COMMAND
# ══════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = (
        'Scrape 9jarocks.net category pages and publish directly to WordPress '
        '(no DB interaction, no social media).'
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
        print("\n📋  Available --category aliases (9jarocks.net → WP)\n")
        print(f"  {'Alias':<20} {'WP Category':<30} {'Type'}")
        print("  " + "─" * 60)
        for alias, keys in CATEGORY_ALIASES.items():
            if not keys:
                continue
            first_key = keys[0]
            if first_key not in _KEY_TO_DEF:
                continue
            defn     = _KEY_TO_DEF[first_key]
            type_str = 'Series' if defn['is_series'] else 'Movie'
            print(f"  {alias:<20} {defn['wp_cat']:<30} {type_str}")
        print()

    def add_arguments(self, parser):
        parser.add_argument(
            '--startpage', type=int, default=1,
            help='Category listing page to start from (default: 1)',
        )
        parser.add_argument(
            '--endpage', type=int, default=None,
            help='Stop after this listing page (inclusive)',
        )
        parser.add_argument(
            '--max-pages', type=int, default=None,
            help='Maximum listing pages to crawl per category',
        )
        parser.add_argument(
            '--category', type=str, default='all',
            help=(
                'Which category to scrape. Friendly aliases:\n'
                '  hollywood, nollywood, kdrama, chinese_drama,\n'
                '  thai_drama, filipino_drama, anime, foreign,\n'
                '  wrestling, ongoing, all  (default: all)'
            ),
        )
        parser.add_argument(
            '--delay', type=float, default=0.5,
            help='Seconds between individual post requests (default: 0.5)',
        )
        parser.add_argument(
            '--list-categories', action='store_true', default=False,
            help='Print all available --category aliases and exit',
        )
        parser.add_argument(
            '--url', type=str, default=None,
            metavar='URL',
            help=(
                'Scrape a single 9jarocks post URL directly. '
                'Example: --url https://9jarocks.net/videodownload/some-movie-id123.html'
            ),
        )
        parser.add_argument(
            '--urls-file', type=str, default=None,
            metavar='FILE',
            help=(
                'Path to a plain-text file containing individual 9jarocks post URLs '
                'to scrape (one URL per line). Blank lines and lines starting with # '
                'are ignored. When this option is used, --category / --startpage / '
                '--endpage / --max-pages are all ignored.'
            ),
        )

    # ── helpers ────────────────────────────────────────────────
    def _load_urls_file(self, filepath: str) -> list:
        """
        Read a plain-text file of post URLs.
        - One URL per line.
        - Lines starting with # (after stripping) are treated as comments.
        - Blank lines are skipped.
        - Duplicate URLs are removed while preserving order.
        """
        import os
        if not os.path.isfile(filepath):
            raise FileNotFoundError(f"URLs file not found: {filepath}")

        seen = set()
        urls = []
        with open(filepath, 'r', encoding='utf-8') as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue
                # Tolerate lines like "1. https://..." (numbered lists)
                line = re.sub(r'^\d+[\.\)]\s*', '', line).strip()
                if not line:
                    continue
                # Normalise www. variant → canonical URL
                line = _normalise_9jarocks_url(line)
                if line in seen:
                    print(f"   ⚠️  Duplicate URL skipped: {line}")
                    continue
                if not _is_post_url(line):
                    print(f"   ⚠️  Skipping non-post URL: {line}")
                    continue
                seen.add(line)
                urls.append(line)
        return urls

    def _infer_wp_cat_from_parsed(self, parsed: dict) -> tuple:
        """
        Given a parsed post dict, infer (wp_cat_name, is_series) by looking at
        the categories list extracted from the page.  Falls back to 'Video' / False.
        """
        cats = [c.lower() for c in parsed.get('categories', [])]

        # Priority order: try to match a known category definition
        for defn in CATEGORY_DEFINITIONS:
            label_lower = defn['label'].lower()
            wp_cat_lower = defn['wp_cat'].lower()
            slug_part = defn['slug'].split('/')[-1].replace('-', ' ')
            if any(
                label_lower in c or wp_cat_lower in c or slug_part in c
                for c in cats
            ):
                return defn['wp_cat'], defn['is_series']

        # Fallback heuristics on raw category strings
        all_cats = ' '.join(cats)
        if 'movie' in all_cats:
            if 'nollywood' in all_cats:
                return 'Nollywood movie', False
            if 'bollywood' in all_cats or 'foreign' in all_cats:
                return 'Other foreign movies', False
            return 'Hollywood movie', False
        if 'series' in all_cats or 'ongoing' in all_cats:
            return 'Hollywood Series', True
        if 'anime' in all_cats:
            return 'Anime', True
        if 'korean' in all_cats or 'kdrama' in all_cats:
            return 'Korean Drama', True
        if 'chinese' in all_cats:
            return 'Chinese drama', True
        if 'thai' in all_cats:
            return 'Thai drama', True
        if 'filipino' in all_cats or 'philippine' in all_cats:
            return 'Filipino Drama', True
        if 'japanese' in all_cats:
            return 'Japanese drama', True

        # No match — treat as generic video / movie
        return 'Video', False

    def _scrape_urls_from_file(self, filepath: str, delay: float):
        """
        Main routine for --urls-file mode.
        Loads URLs from *filepath*, scrapes each post, and publishes to WordPress.
        """
        print("=" * 60)
        print("🚀  scrape_9jarocks_wp — URLS-FILE mode")
        print(f"    Source site : {SITE_URL}")
        print(f"    URLs file   : {filepath}")
        print(f"    Delay       : {delay}s between requests")
        print("=" * 60)

        try:
            post_urls = self._load_urls_file(filepath)
        except FileNotFoundError as exc:
            self.stderr.write(f"❌  {exc}")
            return

        if not post_urls:
            self.stderr.write("❌  No valid URLs found in the file. Nothing to do.")
            return

        print(f"\n📋  {len(post_urls)} unique post URL(s) loaded from file.\n")

        scraper = _make_scraper()

        total_scraped = 0
        total_wp_ok   = 0
        total_wp_fail = 0

        for idx, post_url in enumerate(post_urls, start=1):
            print(f"\n{'─'*60}")
            print(f"[{idx}/{len(post_urls)}] 🎬  {post_url}")

            if delay > 0 and idx > 1:
                time.sleep(delay)

            try:
                resp = scraper.get(post_url, timeout=25)
                if resp.status_code != 200:
                    print(f"   ⚠️  HTTP {resp.status_code} — skipping")
                    continue
                post_html = resp.text
            except Exception as exc:
                print(f"   ❌  Fetch error: {exc}")
                continue

            parsed = parse_post_page(post_html, post_url)
            if not parsed:
                print("   ⚠️  Could not parse post — skipping")
                continue

            if not parsed['download_links']:
                print(f"   ⛔  No download links found — skipping '{parsed['title_raw']}'")
                continue

            # Auto-detect wp category from the page's own category tags
            wp_cat_name, cat_is_series = self._infer_wp_cat_from_parsed(parsed)

            title, title_b, is_series = clean_title_parts(parsed['title_raw'])
            if not parsed['is_series']:
                is_series = False
            # If the inferred cat says it's a series, trust it too
            if cat_is_series:
                is_series = True

            print(f"   📝  Title    : {title}")
            if title_b:
                print(f"   📝  Episode  : {title_b}")
            print(f"   🏷   WP cat   : {wp_cat_name}  (auto-detected)")
            print(f"   📥  Links    : {len(parsed['download_links'])}")

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

        print(f"\n\n{'=' * 60}")
        print("🎉  Done (URLs-file mode)!")
        print(f"    Posts scraped    : {total_scraped}")
        print(f"    WP published OK  : {total_wp_ok}")
        print(f"    WP failures      : {total_wp_fail}")
        print("=" * 60)

    def handle(self, *args, **options):
        if options['list_categories']:
            self._print_category_list()
            return

        # ── Single URL mode ───────────────────────────────────────
        single_url = options.get('url')
        if single_url:
            single_url = _normalise_9jarocks_url(single_url)
            if not _is_post_url(single_url):
                self.stderr.write(f"❌  Not a valid 9jarocks post URL: {single_url}")
                return
            self._scrape_urls_from_file(None, delay=options['delay'],
                                        single_urls=[single_url])
            return

        # ── URLs-file mode ────────────────────────────────────────
        urls_file = options.get('urls_file')
        if urls_file:
            self._scrape_urls_from_file(urls_file, delay=options['delay'])
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
        print("🚀  scrape_9jarocks_wp — WordPress only, no DB, no social")
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
            # 9jarocks listing URL: /category/videodownload/<slug>
            cat_base_url  = f"{SITE_URL}/category/{cat_slug_full}"

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

                # 9jarocks pagination: /category/<slug>/page/<n>/
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

                consecutive_err  = 0
                pages_crawled   += 1

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

                    title, title_b, is_series = clean_title_parts(parsed['title_raw'])

                    # Post-level title detection overrides slug default
                    if not parsed['is_series']:
                        is_series = False

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
#   python manage.py scrape_9jarocks_wp
#   python manage.py scrape_9jarocks_wp --list-categories
#
#   # Scrape by category (existing behaviour):
#   python manage.py scrape_9jarocks_wp --category nollywood --startpage 1 --endpage 5
#   python manage.py scrape_9jarocks_wp --category all --max-pages 10 --delay 1.0
#   python manage.py scrape_9jarocks_wp --category anime --max-pages 5
#
#   # Scrape individual posts from a URLs file (NEW):
#   python manage.py scrape_9jarocks_wp --urls-file links.txt
#   python manage.py scrape_9jarocks_wp --urls-file links.txt --delay 1.5
#
#   links.txt format (one URL per line, # = comment, blank lines OK):
#       https://9jarocks.net/videodownload/in-the-grey-2026-id393368.html
#       https://9jarocks.net/videodownload/totally-funny-animals-season-2-id394058.html
#       # this is a comment
#       1. https://9jarocks.net/videodownload/some-movie-id123456.html
# ──────────────────────────────────────────────────────────────