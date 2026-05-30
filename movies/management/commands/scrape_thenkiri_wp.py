"""
scrape_thenkiri_wp.py
=====================
Django management command that scrapes thenkiri.com by crawling category
listing pages (WordPress / Elementor HTML), then visits each post to extract
title, image, description, and download links — and publishes DIRECTLY to
WordPress.  Zero DB interaction, zero social posting.

Source site  : thenkiri.com  (HTML scraping, not REST API)
Target site  : Your WordPress site (WP REST API via WP_SITE_URL / WP_APP_PASSWORD)

Usage:
    python manage.py scrape_thenkiri_wp
    python manage.py scrape_thenkiri_wp --category hollywood
    python manage.py scrape_thenkiri_wp --category kdrama --startpage 3
    python manage.py scrape_thenkiri_wp --startpage 1 --endpage 5
    python manage.py scrape_thenkiri_wp --category all --max-pages 10

Available --category aliases:
    hollywood, kdrama, korean_movie, chinese, chinese_drama,
    bollywood, philippine, k_variety, series, all  (default: all)

Place this file at:
    <your_app>/management/commands/scrape_thenkiri_wp.py
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
# SITE / CATEGORY CONSTANTS
# ══════════════════════════════════════════════════════════════

SITE_URL = 'https://thenkiri.com'

# ── Category definitions ──────────────────────────────────────
# Each entry maps a thenkiri category slug to a WP category name
# on the target site, plus a flag for whether posts in it are
# typically series (True) or movies (False).
#
# wp_cat      : the WP category name on the TARGET site
# is_series   : used as a fallback when the post title gives no clue
#
CATEGORY_DEFINITIONS = [
    {
        'key':       'hollywood',
        'slug':      'international',
        'label':     'Hollywood / International Movies',
        'wp_cat':    'Hollywood',
        'is_series': False,
    },
    {
        'key':       'series',
        'slug':      'tv-series',
        'label':     'TV Series',
        'wp_cat':    'TV Series',
        'is_series': True,
    },
    {
        'key':       'kdrama',
        'slug':      'download-k-drama',
        'label':     'Korean Drama',
        'wp_cat':    'Korean',
        'is_series': True,
    },
    {
        'key':       'korean_movie',
        'slug':      'asian-movies/download-korean-movies',
        'label':     'Korean Movies',
        'wp_cat':    'Korean',
        'is_series': False,
    },
    {
        'key':       'bollywood',
        'slug':      'asian-movies/download-bollywood-movies',
        'label':     'Bollywood Movies',
        'wp_cat':    'Foreign',
        'is_series': False,
    },
    {
        'key':       'chinese',
        'slug':      'asian-movies/chinese-movie',
        'label':     'Chinese Movies',
        'wp_cat':    'Foreign',
        'is_series': False,
    },
    {
        'key':       'chinese_drama',
        'slug':      'chinese-dramas',
        'label':     'Chinese Dramas',
        'wp_cat':    'TV Series',
        'is_series': True,
    },
    {
        'key':       'philippine',
        'slug':      'asian-movies/download-philippine-movies',
        'label':     'Philippine Movies',
        'wp_cat':    'Foreign',
        'is_series': False,
    },
    {
        'key':       'k_variety',
        'slug':      'k-variety',
        'label':     'K-Variety',
        'wp_cat':    'Korean',
        'is_series': True,
    },
]

CATEGORY_ALIASES = {
    'hollywood':      ['hollywood'],
    'series':         ['series'],
    'kdrama':         ['kdrama'],
    'korean':         ['kdrama', 'korean_movie'],
    'korean_movie':   ['korean_movie'],
    'chinese':        ['chinese', 'chinese_drama'],
    'cdrama':         ['chinese_drama'],
    'chinese_movie':  ['chinese'],
    'bollywood':      ['bollywood'],
    'philippine':     ['philippine'],
    'filipino':       ['philippine'],
    'k_variety':      ['k_variety'],
    'all':            [d['key'] for d in CATEGORY_DEFINITIONS],
}

_SLUG_TO_DEF = {d['slug']: d for d in CATEGORY_DEFINITIONS}
_KEY_TO_DEF  = {d['key']:  d for d in CATEGORY_DEFINITIONS}


# ── Download domains / ad-skip lists ─────────────────────────

AD_DOMAINS = [
    'associationfoam.com', 'obqj2.com', 'cranialhubbed.com',
    'admiredjumper.com', 'getdirectbonus.com', 'push-sdk.com',
    'go.getdirectbonus.com',
]

KNOWN_DOWNLOAD_DOMAINS = [
    'mega.nz', 'drive.google.com', 'mediafire.com', 'pixeldrain.com',
    'terabox.com', 'gofile.io', 'mixdrop.co', 'streamtape.com',
    'doodstream.com', 'filemoon.sx', 'loadedfiles.org', 'netnaijafiles.xyz',
    'sabishares.com', 'meetdownload.com', 'webloaded.com.ng', 'wideshares.org',
    'downloadwella.com', 'netnaija.com', 'fzmovies.net', 'o2tvseries.com',
    'sojuoppa.com', 'dramabus.tv', 'my9jatv.com', 'yts.mx', 'yts.am',
    'nkirifiles.com', 'dl.', 'archive.org', 'onedrive.live.com',
]

FILE_EXTENSIONS = ['.mp4', '.mkv', '.avi', '.mov', '.zip', '.rar', '.srt']

DOWNLOAD_KEYWORDS = [
    'download', '480p', '720p', '1080p', '4k', 'hd', 'episode',
    'fast server', 'slow server', 'mirror', 'part ', 'batch',
]


# In-memory WP category cache  name → ID  (avoids repeated API calls per run)
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

def _is_post_url(href: str) -> bool:
    if not href.startswith(SITE_URL):
        return False
    path = href[len(SITE_URL):]
    if not path or path == '/':
        return False
    skip = (
        '/category/', '/tag/', '/page/', '/wp-', '/feed', '/author/',
        '/search/', '/how-to-download', '?', '#', '/movies-menu/',
        '/korean-drama-menu/', '/tv-series-menu/', '/comments/',
        '/sitemap', '.xml', '.php',
    )
    if any(s in path for s in skip):
        return False
    if '//' in path:
        return False
    segments = [s for s in path.strip('/').split('/') if s]
    if len(segments) != 1:
        return False
    return True


def get_post_urls_from_listing_page(html: str, base_url: str) -> list:
    soup  = BeautifulSoup(html, 'html.parser')
    links = set()

    for article in soup.find_all('article'):
        for a in article.find_all('a', href=True):
            href = a['href'].strip().rstrip('/')
            if _is_post_url(href):
                links.add(href)

    if not links:
        for a in soup.find_all('a', href=True):
            href = a['href'].strip().rstrip('/')
            if _is_post_url(href):
                links.add(href)

    return list(links)


def has_next_page(html: str) -> bool:
    soup = BeautifulSoup(html, 'html.parser')
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
# POST PAGE PARSER
# ══════════════════════════════════════════════════════════════

def parse_post_page(html: str, url: str) -> dict | None:
    """
    Parse a single thenkiri.com post page.

    Returns a dict with keys:
        title_raw, description, image_url, video_url,
        download_links, categories, is_series, is_complete,
        meta, vi_year, vi_country, vi_language, vi_subtitle,
        vi_genre, vi_cast, vi_episodes, vi_status, vi_runtime, vi_filesize
    or None if the page cannot be parsed.
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
        title_raw = re.sub(
            r'\s*[|\-–]\s*(Download\s+\w.*|TheNkiri.*|Nkiri.*)$',
            '', title_raw, flags=re.IGNORECASE
        ).strip()
        title_raw = re.sub(r'^DOWNLOAD\s+', '', title_raw, flags=re.IGNORECASE).strip()

    if not title_raw or len(title_raw) < 4:
        h1 = (
            soup.find('h1', class_='single-post-title') or
            soup.find('h1', class_='entry-title') or
            soup.find('h1', class_='post-title') or
            soup.find('h1')
        )
        if h1:
            title_raw = h1.get_text(strip=True)

    if not title_raw or len(title_raw) < 4:
        title_tag = soup.find('title')
        if title_tag:
            title_raw = re.sub(
                r'\s*[|\-–]\s*(TheNkiri|Nkiri|NKIRI DOWNLOAD).*$',
                '', title_tag.get_text(strip=True), flags=re.IGNORECASE
            ).strip()
            title_raw = re.sub(r'^DOWNLOAD\s+', '', title_raw, flags=re.IGNORECASE).strip()

    if not title_raw or len(title_raw) < 4:
        return None

    # ── Categories (scraped — informational only) ────────────────
    categories = []
    for a in soup.find_all('a', rel=True):
        rels = a.get('rel', [])
        if isinstance(rels, str):
            rels = rels.split()
        if 'category' in rels or 'tag' in rels:
            name = a.get_text(strip=True)
            if name and name.lower() not in ('uncategorized',):
                categories.append(name)

    # ── Content div ──────────────────────────────────────────────
    content_div = (
        soup.find('div', class_='entry-content') or
        soup.find('div', class_='post-content') or
        soup.find('div', class_='the-content') or
        soup.find('div', class_='entry') or
        soup.find('article') or
        soup.find('div', id='content') or
        soup.find('main', id='main') or
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
            src = img.get('src') or img.get('data-src') or img.get('data-lazy-src') or ''
            src = src.strip()
            if src and not src.endswith('.gif'):
                w = img.get('width', '0')
                try:
                    if int(str(w).replace('px', '')) < 80:
                        continue
                except ValueError:
                    pass
                image_url = src
                break

    # ── Video / Trailer ──────────────────────────────────────────
    # thenkiri embeds YouTube via Elementor's video widget data-settings JSON.
    video_url = ''
    for _widget in soup.find_all(attrs={'data-widget_type': 'video.default'}):
        _raw = _widget.get('data-settings', '')
        if _raw:
            try:
                _s = _json.loads(_raw)
                _yt = _s.get('youtube_url', '')
                if _yt:
                    video_url = _yt
                    break
            except Exception:
                pass

    if not video_url:
        for _iframe in soup.find_all('iframe', src=True):
            _src = _iframe['src'].strip()
            if any(d in _src for d in ['youtube.com/embed', 'youtu.be', 'youtube-nocookie.com']):
                video_url = _src
                break

    if not video_url and content_div:
        _iframe = content_div.find('iframe', src=True)
        if _iframe:
            video_url = _iframe['src'].strip()

    # ── Description ──────────────────────────────────────────────
    # Strategy 1: Elementor overview/synopsis widget (most accurate on thenkiri)
    description = ''
    for _widget in soup.find_all(attrs={'data-widget_type': True}):
        _wtype = _widget.get('data-widget_type', '')
        if 'text-editor' not in _wtype and 'text_editor' not in _wtype:
            continue
        _cls = ' '.join(_widget.get('class', []))
        if not any(x in _cls for x in ('overview', 'synopsis', 'description', 'plot')):
            continue
        _text = _widget.get_text(' ', strip=True)
        if _text and len(_text) > 40:
            description = _text[:800]
            break

    # Strategy 2: any div/section with class containing overview/synopsis
    if not description:
        for _sel in ('overview', 'synopsis', 'plot', 'movie-description',
                     'series-description', 'post-description'):
            _el = soup.find(class_=re.compile(_sel, re.IGNORECASE))
            if _el:
                _text = _el.get_text(' ', strip=True)
                if _text and len(_text) > 40:
                    description = _text[:800]
                    break

    # Strategy 3: Elementor text-editor widget that contains a long paragraph
    if not description:
        for _widget in soup.find_all(attrs={'data-widget_type': re.compile(r'text.editor', re.I)}):
            _text = _widget.get_text(' ', strip=True)
            # Must be a real synopsis: long, no URLs, not a metadata block
            if (len(_text) > 80
                    and not re.search(r'https?://', _text)
                    and not re.search(r'^\s*(genre|year|country|language|stars|status)\s*:', _text, re.I)):
                description = _text[:800]
                break

    # Strategy 4: og:description meta tag
    if not description:
        og_desc = soup.find('meta', property='og:description')
        if og_desc:
            description = og_desc.get('content', '').strip()

    # Strategy 5: first long <p> inside content div
    if not description and content_div:
        for p in content_div.find_all('p'):
            text = p.get_text(strip=True)
            if text and len(text) > 60 and not re.search(r'https?://', text):
                # Skip metadata-looking lines
                if not re.match(r'^(genre|year|country|language|stars|status|runtime|duration)\s*:', text, re.I):
                    description = text[:800]
                    break

    if description and len(description) > 800:
        description = description[:800].rsplit(' ', 1)[0] + '...'

    # ── Metadata key:value lines ─────────────────────────────────
    meta = {}
    if content_div:
        table = content_div.find('table')
        if table:
            for row in table.find_all('tr'):
                cells = row.find_all(['td', 'th'])
                if len(cells) >= 2:
                    key = cells[0].get_text(strip=True).lower().rstrip(':')
                    val = cells[1].get_text(strip=True)
                    if key and val:
                        meta[key] = val
        if not meta:
            for p in content_div.find_all('p'):
                text = p.get_text('\n')
                for line in text.splitlines():
                    if ':' in line:
                        key, _, val = line.partition(':')
                        k = key.strip().lower()
                        v = val.strip()
                        if k and v and len(k) < 30:
                            meta[k] = v
        if not meta:
            for li in content_div.find_all('li'):
                text = li.get_text(strip=True)
                if ':' in text:
                    key, _, val = text.partition(':')
                    k = key.strip().lower()
                    v = val.strip()
                    if k and v and len(k) < 30:
                        meta[k] = v

    # ── Download links ───────────────────────────────────────────
    _SKIP_BTN = {
        "can't download?", "cant download?", "cant download",
        "how to download", "how to download?",
        "report broken link", "report link", "report broken",
        "request movie", "request a movie",
        "subscribe", "follow us", "join us",
        "leave a comment", "share", "recommended",
        "notify me", "get notified",
    }
    _SKIP_HREF_FRAGS = [
        'how-to-download', 'how_to_download', '/faq', '/help',
        'report-broken', 'request-movie', 'cant-download',
    ]

    download_links = []
    seen_urls = set()

    def _get_section_season(section_el):
        if section_el is None:
            return ''
        season_label = ''
        for sib in section_el.previous_siblings:
            if not hasattr(sib, 'find_all'):
                continue
            for h2 in sib.find_all(['h2', 'h3', 'h4']):
                txt = h2.get_text(strip=True)
                if re.search(r'\bSeason\s*\d+\b', txt, re.IGNORECASE):
                    season_label = txt.strip()
                    break
            if season_label:
                break
        return season_label

    def _episode_prefix(anchor):
        # Strategy 1: Elementor 3-column layout
        el      = anchor
        section = None
        for _ in range(10):
            el = el.parent
            if el is None:
                break
            cls = ' '.join(el.get('class', []))
            if 'elementor-section' in cls or 'elementor-top-section' in cls:
                section = el
                break

        if section is not None:
            columns = section.find_all(
                'div', class_=lambda c: c and 'elementor-column' in c
            )
            if columns:
                first_col = columns[0]
                for heading in first_col.find_all(['h2', 'h3', 'h4', 'h5', 'h6']):
                    txt = heading.get_text(strip=True)
                    if txt and re.search(
                        r'episode|ep\.?\s*\d|part\s*\d|s\d{1,2}e\d',
                        txt, re.IGNORECASE
                    ):
                        season_label = _get_section_season(section)
                        if season_label and season_label.lower() not in txt.lower():
                            return f"{txt} ({season_label})"
                        return txt

        # Strategy 2: traditional sibling text
        parent = anchor.parent
        for _ in range(4):
            if parent is None:
                break
            if parent.name in ('p', 'div', 'li', 'td', 'strong', 'em', 'span'):
                break
            parent = parent.parent
        if parent is None:
            return ''
        parts = []
        for sibling in parent.children:
            if sibling is anchor:
                break
            txt = (sibling.get_text(' ', strip=True)
                   if hasattr(sibling, 'get_text')
                   else str(sibling).strip())
            if txt:
                parts.append(txt)
        prefix = ' '.join(parts).strip()
        if prefix and re.search(
            r'episode|ep\.?\s*\d|part\s*\d|zip|s\d{1,2}e\d|batch',
            prefix, re.IGNORECASE
        ):
            return prefix
        return ''

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
            if any(ad in href_lower for ad in AD_DOMAINS):
                continue
            if any(skip in href_lower for skip in [
                'facebook.com', 'twitter.com', 't.me/official', 'youtube.com/watch?',
                'imdb.com', 'wp-admin', '#respond', 'mailto:',
                'thenkiri.com/category/', 'thenkiri.com/tag/',
                'thenkiri.com/how-to', 'thenkiri.com/page/',
                'dramakey.com', 'nkiri.ink', 'tiktok.com', 'x.com/official',
            ]):
                continue
            if href_lower.startswith(SITE_URL.lower()) and not any(
                kw in href_lower for kw in KNOWN_DOWNLOAD_DOMAINS + ['/dl/', '/get/', '/file/', 'download']
            ):
                continue

            is_dl = (
                any(d in href_lower for d in KNOWN_DOWNLOAD_DOMAINS)
                or any(href_lower.endswith(ext) for ext in FILE_EXTENSIONS)
                or any(kw in btn_lower for kw in DOWNLOAD_KEYWORDS)
                or any(kw in href_lower for kw in ['/dl/', '/get/', '/file/', 'download', 'mirror'])
            )

            if is_dl and href not in seen_urls:
                seen_urls.add(href)
                prefix = _episode_prefix(a)
                label  = f"{prefix} – {btn_text}" if prefix else btn_text
                download_links.append({'url': href, 'label': label})
                print(f"   🔗 {label} → {href}")

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
        'vi_subtitle': _mv(['subtitle', 'subtitles', 'sub']),
        'vi_genre':    _mv(['genre', 'genres', 'category']),
        'vi_cast':     _mv(['stars', 'cast', 'actors', 'starring']),
        'vi_director': _mv(['director', 'directed by', 'directors']),
        'vi_episodes': _mv(['episodes', 'episode', 'total episodes', 'no of episodes']),
        'vi_status':   _mv(['status', 'series status']),
        'vi_runtime':  _mv(['running time', 'runtime', 'duration', 'run time']),
        'vi_filesize': _mv(['file size', 'filesize', 'size', 'file',
                            'download size', 'video size']),
    }

    # Fallback: pull year from title if not in metadata
    if not vi['vi_year']:
        m_yr = re.search(r'\((\d{4})\)', title_raw)
        if m_yr:
            vi['vi_year'] = m_yr.group(1)

    # Fallback: pull file size from the Elementor alert widget
    if not vi['vi_filesize']:
        for alert in soup.find_all(class_='elementor-alert-description'):
            txt = alert.get_text(strip=True)
            if 'mb' in txt.lower() or 'gb' in txt.lower():
                vi['vi_filesize'] = txt
                break

    # Fallback: pull status from any heading containing "Status :"
    if not vi['vi_status']:
        for heading in soup.find_all(['h2', 'h3', 'h4']):
            txt = heading.get_text(strip=True)
            if re.match(r'status\s*:', txt, re.IGNORECASE):
                vi['vi_status'] = re.sub(r'^status\s*:\s*', '', txt, flags=re.IGNORECASE).strip()
                break

    return {
        'title_raw':      title_raw,
        'description':    description,
        'image_url':      image_url,
        'video_url':      video_url,
        'download_links': download_links,
        'categories':     categories,
        'is_series':      is_series,
        'is_complete':    is_complete,
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

    # Strip trailing "| Category" suffix before regex
    pipe_suffix = ''
    pipe_match  = re.search(r'\s*\|\s*[^|]+$', title)
    if pipe_match:
        pipe_suffix = pipe_match.group(0)
        title       = title[:pipe_match.start()].strip()

    # Detect series (SXX / Season X)
    series_pat = re.compile(r'(?i)(.*?\b(S\d{1,2}|Season\s?\d{1,2}))[\s\-–:]*\s*(.*)')
    match       = series_pat.match(title)
    if match:
        base    = match.group(1).strip()
        title_b = re.sub(r'^\(|\)$', '', match.group(3)).strip()
        if is_complete and 'complete' not in base.lower() and 'complete' not in title_b.lower():
            base += ' (Completed)' if 'completed' in title_lower else ' (Complete)'
        return base, title_b, True

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


def _wp_get_or_create_category(cat_name: str, headers: dict, wp_base: str,
                                is_series: bool = False) -> int | None:
    """
    Resolve cat_name → WP category ID on the target site.
    NEVER creates a new category — unrecognised content falls back to
    'Drama' (series) or 'Movie' (film).
    """
    mapped = cat_name.strip()
    if not mapped:
        mapped = 'Drama' if is_series else 'Movie'

    key = mapped.strip().lower()
    if key in _wp_category_cache:
        return _wp_category_cache[key]

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
        # Fallback
        fallback = 'Drama' if is_series else 'Movie'
        print(f"    ⚠️ Category '{mapped}' not found → fallback to '{fallback}'")
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


def _wp_find_existing_post(title: str, headers: dict, wp_base: str) -> dict | None:
    """
    Search the target WP site for an existing post matching this title.
    Uses exact + prefix matching to handle series posts that get updated
    episode by episode.
    """
    search_title = re.sub(r'\s*\(Complet(?:e|ed)\)\s*$', '', title, flags=re.IGNORECASE).strip()
    try:
        r = requests.get(
            f'{wp_base}/wp-json/wp/v2/posts',
            params={'search': search_title, 'per_page': 10, 'status': 'any'},
            headers=headers, timeout=10,
        )
        if r.status_code != 200:
            return None
        search_lower = search_title.strip().lower()
        title_lower  = title.strip().lower()
        for post in r.json():
            rendered = BeautifulSoup(
                post['title']['rendered'], 'html.parser'
            ).get_text().strip().lower()
            if rendered in (title_lower, search_lower):
                print(f"    🔎 WP duplicate (exact): {post['title']['rendered']}")
                return post
            if rendered.startswith(search_lower):
                print(f"    🔎 WP duplicate (prefix): {post['title']['rendered']}")
                return post
    except Exception as e:
        print(f"    ⚠️ WP search error: {e}")
    return None


# ══════════════════════════════════════════════════════════════
# SLUG BUILDER
# ══════════════════════════════════════════════════════════════

def _make_slug(text: str, is_series: bool = False) -> str:
    """
    Build a clean WordPress slug.
    Series  : slug stops at season identifier (SEO-safe on updates).
    Movies  : full title + year.
    """
    import unicodedata
    if is_series:
        text = re.sub(
            r'\s*[\(\[]?\s*(?:episode\s*\d+\s*(?:added)?|complete[d]?)\s*[\)\]]?.*$',
            '', text, flags=re.IGNORECASE
        ).strip()
        text = re.sub(r'\s*\|.*$', '', text).strip()

    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    text = text.lower()
    text = re.sub(r"[`']+", '', text)
    text = re.sub(r'[^a-z0-9]+', '-', text)
    return text.strip('-')


# ══════════════════════════════════════════════════════════════
# WP CONTENT BUILDER  (NaijaDeleys / Jannah design)
# ══════════════════════════════════════════════════════════════

def _build_wp_content(title: str, title_b: str, description: str,
                      meta_info: dict, image_url: str, video_url: str,
                      download_links: list, is_series: bool,
                      wp_image_url: str = '') -> str:
    """
    Build the HTML post body that matches the NaijaDeleys manual-post design.

    Layout:
      0.  SEO keyword paragraph (directly under post title)
      0b. Poster image inline (visible in post body) — uses wp_image_url if available
      1.  DOWNLOAD heading (bold)
      2.  Description paragraph
      3.  VIDEO INFORMATION heading + blockquote card
      4.  TRAILER heading + YouTube iframe
      5.  VLC / MX Player tip box
      6.  Download buttons

    wp_image_url : WP-hosted URL of the uploaded poster (preferred over raw image_url).
                   Falls back to image_url if not supplied.
    """
    parts = []

    # Detect Nollywood for SEO text variation
    _cat_text = ' '.join(str(v) for v in meta_info.values()).lower()
    _is_nollywood = any(x in _cat_text for x in ('nollywood', 'nigerian', 'nigeria'))

    # ── 0. SEO keyword paragraph (first — appears right under post title) ──
    # Pull year early (needed for SEO text and DOWNLOAD heading)
    _year_early = meta_info.get('vi_year', meta_info.get('year', '')).strip()
    if not _year_early:
        _yr_m = re.search(r'\((\d{4})\)', title)
        if _yr_m:
            _year_early = _yr_m.group(1)
    year         = _year_early   # used throughout this function
    _title_no_yr = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()
    yr_str       = f' ({year})' if year else ''
    base_yr      = f'{_title_no_yr}{yr_str}'

    ep_label     = ''
    if title_b and is_series:
        ep_m = re.search(r'(episode\s*\d+)', title_b, re.IGNORECASE)
        if ep_m:
            ep_label = ep_m.group(1).title()

    season_label = ''
    if is_series:
        s_m = re.search(r'\bS(\d{1,2})\b', title, re.IGNORECASE)
        if s_m:
            season_label = f'Season {int(s_m.group(1))}'
        else:
            s_m2 = re.search(r'Season\s*(\d{1,2})', title, re.IGNORECASE)
            if s_m2:
                season_label = f'Season {int(s_m2.group(1))}'

    seas_str = f' {season_label}' if season_label else ''
    ep_str   = f' {ep_label}'     if ep_label     else ''

    if is_series:
        seo_text = (
            f'Download {base_yr}{seas_str}{ep_str} mp4 mkv, '
            f'latest Tv Series {base_yr}{seas_str} 720p 480p, '
            f'{base_yr}{seas_str}{ep_str} Tv Series Download.'
        )
    elif _is_nollywood:
        seo_text = (
            f'Download {base_yr} mp4 mkv, '
            f'latest Nollywood movie {base_yr} 720p 480p, '
            f'{base_yr} Nollywood movie Download.'
        )
    else:
        seo_text = (
            f'Download {base_yr} mp4 mkv, '
            f'latest Hollywood movie {base_yr} 720p 480p, '
            f'{base_yr} Hollywood movie Download.'
        )
    parts.append(f'<p>{seo_text}</p>')

    # ── 0b. Poster image inline (after SEO text, before DOWNLOAD heading) ──
    # Prefer the WP-hosted URL so the image always loads (no hotlink issues).
    _inline_img_src = wp_image_url or image_url
    if _inline_img_src:
        safe_title = title.replace('"', '&quot;')
        parts.append(
            f'<p style="text-align:center;">'
            f'<img decoding="async" loading="lazy" src="{_inline_img_src}" alt="{safe_title}" '
            f'style="max-width:100%;height:auto;display:block;margin:0 auto 15px;" /></p>'
        )

    # ── 1. DOWNLOAD heading ───────────────────────────────────────
    if is_series and title_b:
        dl_head = f'DOWNLOAD {title} ({title_b}) | Free DOWNLOAD Mp4'
    elif year and f'({year})' not in title:
        dl_head = f'DOWNLOAD {title} ({year}) | Free DOWNLOAD Mp4'
    else:
        dl_head = f'DOWNLOAD {title} | Free DOWNLOAD Mp4'
    parts.append(f'<p><strong>{dl_head}</strong></p>')

    # ── 2. Description ───────────────────────────────────────────
    if description:
        parts.append(f'<p>{description}</p>')

    # ── 3. VIDEO INFORMATION heading + blockquote ───────────────
    year     = meta_info.get('vi_year',     meta_info.get('year', '')).strip()
    filesize = meta_info.get('vi_filesize', meta_info.get('file size', '')).strip()
    dur      = meta_info.get('vi_runtime',  meta_info.get('running time', meta_info.get('runtime', ''))).strip()
    imdb     = meta_info.get('imdb', '').strip()
    status   = meta_info.get('vi_status',   meta_info.get('status', '')).strip()
    sub      = meta_info.get('vi_subtitle', meta_info.get('subtitle', '')).strip()
    genre    = meta_info.get('vi_genre',    meta_info.get('genre', '')).strip()
    stars    = meta_info.get('vi_cast',     meta_info.get('stars', '')).strip()
    country  = meta_info.get('vi_country',  meta_info.get('country', '')).strip()
    lang     = meta_info.get('vi_language', meta_info.get('language', '')).strip()
    director = meta_info.get('vi_director', meta_info.get('director', '')).strip()
    total_episodes = meta_info.get('vi_episodes', meta_info.get('episodes', '')).strip()

    # Title without year for the "Title:" row
    _title_clean = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()

    if is_series:
        info_lines = []
        if filesize:        info_lines.append(f'Filesize: {filesize}')
        if dur:             info_lines.append(f'Duration: {dur}')
        if imdb:
            info_lines.append(f'Imdb: <a href="{imdb}" target="_blank" rel="nofollow noopener">{imdb}</a>')
        if _title_clean:    info_lines.append(f'Title: {_title_clean}')
        if year:            info_lines.append(f'Year: {year}')
        info_lines.append('Type: TV Series')
        if country:         info_lines.append(f'Country: {country}')
        if lang:            info_lines.append(f'Language: {lang}')
        if director:        info_lines.append(f'Director: {director}')
        if genre:           info_lines.append(f'Genre: {genre}')
        if stars:           info_lines.append(f'Stars: {stars}')
        if total_episodes:  info_lines.append(f'Total Episodes: {total_episodes}')
        if status:          info_lines.append(f'Status: {status}')
        if sub:             info_lines.append(f'Subtitle: {sub}')
    else:
        info_lines = []
        if filesize:    info_lines.append(f'Filesize: {filesize}')
        if dur:         info_lines.append(f'Duration: {dur}')
        if imdb:
            info_lines.append(f'Imdb: <a href="{imdb}" target="_blank" rel="nofollow noopener">{imdb}</a>')
        if _title_clean: info_lines.append(f'Title: {_title_clean}')
        if year:         info_lines.append(f'Year: {year}')
        info_lines.append('Type: Movie')
        if country:      info_lines.append(f'Country: {country}')
        if lang:         info_lines.append(f'Language: {lang}')
        if director:     info_lines.append(f'Director: {director}')
        if genre:        info_lines.append(f'Genre: {genre}')
        if stars:        info_lines.append(f'Stars: {stars}')
        if sub:          info_lines.append(f'Subtitle: {sub}')

    if info_lines:
        parts.append('<p><strong>VIDEO INFORMATION</strong></p>')
        inner = '<br />\n'.join(info_lines)
        parts.append(f'<blockquote><p>{inner}</p></blockquote>')

    # ── 4. TRAILER / WATCH heading + embed ───────────────────────
    if video_url:
        section_head = 'TRAILER' if is_series else 'TRAILER'
        parts.append(f'<p><strong>{section_head}</strong></p>')
        yt_match = re.search(
            r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([\w\-]{11})',
            video_url
        )
        embed_url = f'https://www.youtube.com/embed/{yt_match.group(1)}' if yt_match else video_url
        parts.append(
            f'<p><iframe class="BLOG_video_class" src="{embed_url}" '
            f'width="780" height="439" allowfullscreen="allowfullscreen"></iframe></p>'
        )

    # ── 5. VLC / MX Player tip box ───────────────────────────────
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

    # ── 6. Download buttons ───────────────────────────────────────
    if download_links:
        if is_series:
            parts.append('<div style="text-align: left; font-family: Arial; margin-top: 10px;">')
            for i, dl in enumerate(download_links, 1):
                raw_label = dl.get('label', '').strip()
                url       = dl['url']
                ep_match  = re.search(r'episode\s*(\d+)', raw_label, re.IGNORECASE)
                btn_label = f'EPISODE {ep_match.group(1)}' if ep_match else f'EPISODE {i}'
                if 'zip' in url.lower() or 'zip' in raw_label.lower():
                    _s_match  = re.search(r's(\d+)', title, re.IGNORECASE)
                    _s_num    = _s_match.group(1) if _s_match else '1'
                    btn_label = f'DOWNLOAD ZIP SEASON {_s_num}'
                parts.append(
                    f'<div style="margin-bottom: 8px;">'
                    f'<a style="display: inline-flex; align-items: center; background: #fff; '
                    f'border: 3px solid #28a745; color: #28a745; padding: 6px 15px; '
                    f'text-decoration: none; font-weight: 900; border-radius: 6px; '
                    f'box-shadow: 0 3px 8px rgba(0,0,0,.5); text-transform: uppercase; font-size: 13px;" '
                    f'href="{url}">'
                    f'<img decoding="async" style="width: 16px; margin-right: 8px;" '
                    f'src="https://img.icons8.com/material-sharp/24/28a745/download.png" />'
                    f'{btn_label}</a>'
                    f'</div>'
                )
            parts.append('</div>')
        else:
            for dl in download_links:
                url = dl['url']
                parts.append(
                    '<div style="text-align:left; margin:10px 0 15px; font-family:Arial;">'
                    f'<a href="{url}" '
                    'style="display:inline-flex; align-items:center; background:#fff; '
                    'border:3px solid #28a745; color:#28a745; padding:8px 18px; '
                    'text-decoration:none; font-weight:900; border-radius:6px; '
                    'box-shadow:0 3px 10px rgba(0,0,0,.5); text-transform:uppercase; font-size:14px;">'
                    '<img decoding="async" '
                    'src="https://img.icons8.com/material-sharp/24/28a745/download.png" '
                    'style="width:18px; height:18px; margin-right:10px;">'
                    '  DOWNLOAD HERE'
                    '</a>'
                    '</div>'
                )



    return '\n'.join(parts)


# ══════════════════════════════════════════════════════════════
# RANK MATH SEO BUILDER
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
    elif 'bollywood' in cat_lower or 'indian' in cat_lower:
        drama_type = 'Indian'
    elif 'philippine' in cat_lower or 'filipino' in cat_lower:
        drama_type = 'Philippine'
    else:
        drama_type = country if country else ''

    is_nollywood = any(x in cat_lower for x in ('nollywood', 'nigerian'))
    is_anime     = 'anime' in cat_lower
    is_drama     = bool(drama_type) and not is_anime and is_series
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
        seo_title = f'{title} ({title_b}) - NaijaDeleys'
    elif year and f'({year})' not in title:
        seo_title = f'{title} ({year}) - NaijaDeleys'
    else:
        seo_title = f'{title} - NaijaDeleys'

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
            f'Download {title} ({year}) Movie For FREE In 480p, 720p, 1080p'
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
    """
    Publish (create or update) a single post on the target WordPress site.

    wp_cat_name : the WP category name derived from the thenkiri slug
                  we crawled (the authoritative category — never changes).
    categories  : scraped category strings from the source post page
                  (used for SEO / Rank Math only — not for category assignment).
    """
    try:
        headers  = _get_wp_auth_header()
        wp_base  = _get_wp_base_url()

        if not wp_base:
            print("    ⚠️ WP_SITE_URL not configured — skipping.")
            return False

        # Upload poster image first so we can use the WP URL in the post body
        wp_media_id  = None
        wp_image_url = ''
        if image_url:
            wp_media_id = _wp_upload_image(image_url, title, headers, wp_base)
            if wp_media_id:
                # Fetch the WP-hosted source URL for inline use
                try:
                    _mr = requests.get(
                        f'{wp_base}/wp-json/wp/v2/media/{wp_media_id}',
                        headers=headers, timeout=10,
                    )
                    if _mr.status_code == 200:
                        wp_image_url = _mr.json().get('source_url', '')
                except Exception:
                    pass
            # Fallback: use original URL if upload failed or source_url missing
            if not wp_image_url:
                wp_image_url = image_url

        # Build content & SEO
        content       = _build_wp_content(
            title, title_b, description, meta_info,
            image_url, video_url, download_links, is_series,
            wp_image_url=wp_image_url,
        )
        rank_math_meta = _build_rank_math_seo(
            title, title_b, description, meta_info, categories, is_series
        )

        # Resolve WP category ID using the authoritative slug-derived name
        cat_id  = _wp_get_or_create_category(wp_cat_name, headers, wp_base, is_series)
        cat_ids = [cat_id] if cat_id else []

        # Full post title  e.g.  "Zatima S04 (Episode 16 Added) | Download Tv Series"
        if is_series and title_b:
            full_title = f'{title} ({title_b}) | Download {wp_cat_name}'
        elif is_series:
            full_title = f'{title} | Download {wp_cat_name}'
        else:
            full_title = f'{title} | Download {wp_cat_name}'

        # Excerpt
        year       = meta_info.get('vi_year', meta_info.get('year', '')).strip()
        excerpt_text = ''
        if description:
            excerpt_text = description[:300]

        # Check for existing post (for series update logic)
        existing_post = _wp_find_existing_post(title, headers, wp_base)

        # ── UPDATE ───────────────────────────────────────────────
        if existing_post:
            post_id       = existing_post['id']
            current_title = BeautifulSoup(
                existing_post['title']['rendered'], 'html.parser'
            ).get_text().strip()
            title_changed = (current_title.strip().lower() != full_title.strip().lower())

            patch: dict = {'content': content, 'meta': rank_math_meta}
            if title_changed:
                patch['title'] = full_title
                # NEVER change the slug on update — preserves SEO.
                from datetime import datetime, timezone as tz
                now_utc           = datetime.now(tz.utc)
                patch['date']     = now_utc.strftime('%Y-%m-%dT%H:%M:%S')
                patch['date_gmt'] = now_utc.strftime('%Y-%m-%dT%H:%M:%S')
                print(f"    🔗 Slug preserved (SEO safe) — date bumped.")
            if excerpt_text:
                patch['excerpt'] = excerpt_text
            if cat_ids:
                existing_cats    = existing_post.get('categories', [])
                patch['categories'] = list(set(existing_cats + cat_ids))

            r = requests.post(
                f'{wp_base}/wp-json/wp/v2/posts/{post_id}',
                headers=headers, json=patch, timeout=15,
            )
            if r.status_code == 200:
                action = 'title+date bumped' if title_changed else 'content only'
                print(f"    ✏️  WP updated ({action}, ID {post_id}) — {full_title}")
                return True
            else:
                print(f"    ⚠️ WP update failed: {r.status_code} {r.text[:150]}")
                return False

        # ── CREATE ───────────────────────────────────────────────
        post_data: dict = {
            'title':   full_title,
            'slug':    _make_slug(title, is_series=is_series),
            'content': content,
            'status':  'publish',
            'format':  'video',
            'excerpt': excerpt_text or '',
            'meta':    rank_math_meta,
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
        'Scrape thenkiri.com category pages and publish directly to WordPress '
        '(no DB interaction, no social media).'
    )

    # ── argument resolution ──────────────────────────────────────

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
        print("\n📋  Available --category aliases (thenkiri.com → WP)\n")
        print(f"  {'Alias':<18} {'WP Category':<20} {'Type'}")
        print("  " + "─" * 55)
        for alias, keys in CATEGORY_ALIASES.items():
            if not keys:
                continue
            first_key = keys[0]
            if first_key not in _KEY_TO_DEF:
                continue
            defn     = _KEY_TO_DEF[first_key]
            type_str = 'Series' if defn['is_series'] else 'Movie'
            print(f"  {alias:<18} {defn['wp_cat']:<20} {type_str}")
        print()

    # ── argument declarations ────────────────────────────────────

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
                '  hollywood, kdrama, korean, chinese, cdrama,\n'
                '  bollywood, philippine, k_variety, series, all  (default: all)'
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

    # ── main entry point ─────────────────────────────────────────

    def handle(self, *args, **options):
        if options['list_categories']:
            self._print_category_list()
            return

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
        print("🚀  scrape_thenkiri_wp — WordPress only, no DB, no social")
        print(f"    Categories : {', '.join(d['label'] for d in cats_to_crawl)}")
        print(f"    Pages      : {start_page} → {end_page or '∞'}"
              + (f"  (max {max_pages})" if max_pages else ""))
        print("=" * 60)

        scraper = _make_scraper()

        total_scraped  = 0
        total_wp_ok    = 0
        total_wp_fail  = 0
        consecutive_err = 0
        max_consecutive = 5

        for cat_def in cats_to_crawl:
            cat_slug_full = cat_def['slug']
            wp_cat_name   = cat_def['wp_cat']
            cat_is_series = cat_def['is_series']  # slug-level default
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

                listing_url = (
                    cat_base_url + '/'
                    if page == 1
                    else f"{cat_base_url}/page/{page}/"
                )

                print(f"\n{'─'*60}")
                print(f"🌐 Listing page {page}: {listing_url}")

                try:
                    resp = scraper.get(listing_url, timeout=20)
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
                        post_resp = scraper.get(post_url, timeout=20)
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

                    # If the post-level title detection overrides the slug default, honour it
                    # (e.g. a movie posted under /tv-series/ is still not a series)
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
                        meta_info      = parsed,   # entire parsed dict has vi_* keys
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
#   python manage.py scrape_thenkiri_wp
#   python manage.py scrape_thenkiri_wp --list-categories
#   python manage.py scrape_thenkiri_wp --category hollywood
#   python manage.py scrape_thenkiri_wp --category kdrama --startpage 3
#   python manage.py scrape_thenkiri_wp --category series --startpage 1 --endpage 5
#   python manage.py scrape_thenkiri_wp --category all --max-pages 10 --delay 1.0
# ──────────────────────────────────────────────────────────────