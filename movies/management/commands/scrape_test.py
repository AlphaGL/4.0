"""
Management command: scrape_9jarocks
Scrapes 9jarocks.net by crawling category listing pages, then visiting
each post page to extract title, image, description, and download links.

WHY HTML scraping (not REST API):
  • /wp/v2/posts returns 404 — blocked by the site
  • /wp/v2/pages only has ~10 static pages (Privacy, About, etc.)
  • All actual content lives in /category/videodownload/page/N/

KEY IMPROVEMENTS over previous version:
  ✅ RESUME SUPPORT  — tracks scraped URLs in DB so restarts skip done posts
  ✅ CONCURRENT FETCHING — fetches multiple posts in parallel (ThreadPoolExecutor)
  ✅ RELIABLE PAGINATION — reads wp-pagenavi page count instead of guessing "Next"
  ✅ SMARTER DELAYS — concurrent batches with a single delay between batches
  ✅ CATEGORY API — uses /wp-json/wp/v2/categories to get accurate post counts

HTML structure (confirmed from live page source):
  • Download links : <a class="fa-fa-download" href="...">
  • Featured image : <p><img class="aligncenter ...">  inside .entry-content
  • Video embed    : <iframe ...> inside .entry-content
  • Post metadata  : <blockquote> containing Filename, Director, Stars, etc.
  • Categories     : <a class="post-cat ..."> in .entry-header
  • Pagination     : .wp-pagenavi .pages text like "Page 1 of 847"

Usage examples
──────────────
python manage.py scrape_9jarocks
python manage.py scrape_9jarocks --startpage 5
python manage.py scrape_9jarocks --startpage 1 --endpage 10
python manage.py scrape_9jarocks --no-social
python manage.py scrape_9jarocks --category nollywood-movie
python manage.py scrape_9jarocks --workers 5   # parallel post fetches
python manage.py scrape_9jarocks --delay 0     # no delay (fast, riskier)
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from movies.models import Movie, Category, DownloadLink
import requests
from bs4 import BeautifulSoup
import re
import cloudscraper
from urllib.parse import urlparse, unquote
import urllib3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ══════════════════════════════════════════════════════════════
# SITE CONSTANTS
# ══════════════════════════════════════════════════════════════

SITE_URL = 'https://9jarocks.net'

CATEGORIES = [
    'videodownload/nollywood-movie',
    'videodownload/nollywood-tv-series',
    'videodownload/hollywood-movie',
    'videodownload/hollywood-tv-series',
    'videodownload/foreign-movies',
    'videodownload/other-foreign-series',
    'videodownload/korean-drama',
    'videodownload/chinese-drama',
    'videodownload/thai-drama',
    'videodownload/japanese-drama',
    'videodownload/filipino-drama',
    'videodownload/anime',
    'videodownload/pro-wrestling-fighting-sports',
    'videodownload/ongoing',
    '18-section',
]

# Ad / monetization redirect domains to SKIP
AD_DOMAINS = [
    'associationfoam.com',
    'obqj2.com',
    'cranialhubbed.com',
    'admiredjumper.com',
    'getdirectbonus.com',
    'push-sdk.com',
    'go.getdirectbonus.com',
]

KNOWN_DOWNLOAD_DOMAINS = [
    'loadedfiles.org', 'mega.nz', 'drive.google.com', 'mediafire.com',
    'pixeldrain.com', 'terabox.com', 'gofile.io', 'mixdrop.co',
    'streamtape.com', 'doodstream.com', 'filemoon.sx', 'netnaijafiles.xyz',
    'sabishares.com', 'meetdownload.com', 'webloaded.com.ng',
    'wideshares.org', 'downloadwella.com', 'netnaija.com', 'fzmovies.net',
    'files.9jarocks.net', 'cdn.9jarocks.net', 'download.9jarocks.net',
]

FILE_EXTENSIONS = ['.mp4', '.mkv', '.avi', '.mov', '.zip', '.rar', '.srt']

DOWNLOAD_KEYWORDS = [
    'download', '480p', '720p', '1080p', '4k', 'hd',
    'episode', 'fast server', 'slow server', 'mirror', 'part ', 'batch',
]

# ── Thread-safe print lock ─────────────────────────────────────────────────────
_print_lock = threading.Lock()

def tprint(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)


# ══════════════════════════════════════════════════════════════
# PLATFORM LINKS
# ══════════════════════════════════════════════════════════════

PLATFORM_LINKS = {
    'telegram': 'https://t.me/Watch2D',
    'twitter':  'https://x.com/watch2download',
    'facebook': 'https://facebook.com/WATCH2D/',
    'website':  'https://watch2d.org',
}

TWITTER_FOOTER = (
    f"\n\n📱 Telegram: {PLATFORM_LINKS['telegram']}"
    f"\n📘 Facebook: {PLATFORM_LINKS['facebook']}"
    f"\n🌍 More: {PLATFORM_LINKS['website']}"
)

FACEBOOK_FOOTER = (
    "\n\n━━━━━━━━━━━━━━━━━━━\n"
    "🔔 Follow us everywhere:\n"
    f"📱 Telegram → {PLATFORM_LINKS['telegram']}\n"
    f"🐦 X/Twitter → {PLATFORM_LINKS['twitter']}\n"
    f"🌍 Website → {PLATFORM_LINKS['website']}\n"
    "━━━━━━━━━━━━━━━━━━━"
)


# ══════════════════════════════════════════════════════════════
# RATE LIMITER
# ══════════════════════════════════════════════════════════════

class _RateLimiter:
    def __init__(self):
        self._counts    = {'facebook': 0, 'twitter': 0}
        self._last_post = {'facebook': 0.0, 'twitter': 0.0}
        self._min_gap   = {'facebook': 45, 'twitter': 60}
        self._run_cap   = {'facebook': 80, 'twitter': 40}
        self._lock      = threading.Lock()

    def can_post(self, platform: str) -> bool:
        with self._lock:
            if platform not in self._counts:
                return True
            if self._counts[platform] >= self._run_cap[platform]:
                tprint(f"⚠️ {platform.title()} run cap ({self._run_cap[platform]}) reached — skipping.")
                return False
            elapsed = time.time() - self._last_post[platform]
            gap     = self._min_gap[platform]
            if elapsed < gap:
                wait = gap - elapsed
                tprint(f"⏳ {platform.title()} rate limit — waiting {wait:.0f}s...")
                time.sleep(wait)
            return True

    def record(self, platform: str):
        with self._lock:
            if platform in self._counts:
                self._counts[platform]   += 1
                self._last_post[platform] = time.time()

    def stats(self) -> str:
        return (
            f"📊 Posts this run — "
            f"Facebook: {self._counts['facebook']} | "
            f"Twitter: {self._counts['twitter']}"
        )

_limiter = _RateLimiter()


# ══════════════════════════════════════════════════════════════
# TWITTER TOKEN MANAGER
# ══════════════════════════════════════════════════════════════

class _TwitterTokenManager:
    CACHE_KEY = 'twitter_oauth2_access_token'

    @staticmethod
    def _update_env_refresh_token(new_token: str):
        import os, re as _re
        from django.conf import settings as _s

        candidates = []
        base_dir = getattr(_s, 'BASE_DIR', None)
        if base_dir:
            candidates.append(os.path.join(str(base_dir), '.env'))
        candidates.append(os.path.join(os.getcwd(), '.env'))
        env_path = next((p for p in candidates if os.path.isfile(p)), None)
        if not env_path:
            tprint(f"⚠️ SAVE MANUALLY → TWITTER_REFRESH_TOKEN={new_token}")
            return
        try:
            with open(env_path, 'r') as f:
                content = f.read()
            new_content, n = _re.subn(
                r'^(TWITTER_REFRESH_TOKEN\s*=\s*)(.+)$',
                rf'\g<1>{new_token}', content, flags=_re.MULTILINE
            )
            if n == 0:
                new_content = content.rstrip('\n') + f'\nTWITTER_REFRESH_TOKEN={new_token}\n'
            with open(env_path, 'w') as f:
                f.write(new_content)
            tprint(f"✅ Twitter: New refresh token saved to {env_path}")
        except Exception as e:
            tprint(f"⚠️ SAVE MANUALLY → TWITTER_REFRESH_TOKEN={new_token}  ({e})")

    def get_valid_token(self) -> str | None:
        from django.conf import settings
        from django.core.cache import cache

        cached = cache.get(self.CACHE_KEY)
        if cached:
            return cached

        client_id     = getattr(settings, 'TWITTER_CLIENT_ID', '')
        client_secret = getattr(settings, 'TWITTER_CLIENT_SECRET', '')
        refresh_token = getattr(settings, 'TWITTER_REFRESH_TOKEN', '')

        if not all([client_id, client_secret, refresh_token]):
            tprint("⚠️ Twitter OAuth 2.0 credentials missing — skipping.")
            return None

        tprint("🔄 Twitter: Refreshing access token...")
        try:
            resp = requests.post(
                'https://api.x.com/2/oauth2/token',
                auth=(client_id, client_secret),
                data={'grant_type': 'refresh_token', 'refresh_token': refresh_token},
                timeout=15,
            )
            resp.raise_for_status()
            data         = resp.json()
            access_token = data.get('access_token')
            expires_in   = data.get('expires_in', 7200)
            if access_token:
                cache.set(self.CACHE_KEY, access_token, timeout=expires_in - 600)
                new_refresh = data.get('refresh_token')
                if new_refresh and new_refresh != refresh_token:
                    settings.TWITTER_REFRESH_TOKEN = new_refresh
                    self._update_env_refresh_token(new_refresh)
                return access_token
            tprint(f"⚠️ Twitter token refresh failed: {data}")
        except Exception as e:
            tprint(f"⚠️ Twitter token refresh error: {e}")
        return None

_twitter_token_mgr = _TwitterTokenManager()


# ══════════════════════════════════════════════════════════════
# HASHTAG DETECTION
# ══════════════════════════════════════════════════════════════

def _detect_hashtags(movie):
    title_lower = movie.title.lower()
    try:
        cat_names = ' '.join(c.name.lower() for c in movie.categories.all())
    except Exception:
        cat_names = ''
    combined = title_lower + ' ' + cat_names

    if any(kw in combined for kw in ['korean', 'kdrama', 'k-drama', 'korea']):
        tg = "#Watch2D #KDrama #KoreanDrama #KoreanSeries #AsianDrama #FreeDownload #HDDownload #NowStreaming #MustWatch #BingeWatch #KDramaEnglishSub #WatchFree #Trending"
        tw = "#Watch2D #KDrama #KoreanDrama #AsianDrama #FreeDownload"
    elif any(kw in combined for kw in ['nigerian', 'nollywood', 'naija', 'nigeria']):
        tg = "#Watch2D #Nollywood #NigerianMovies #NaijaMovies #AfricanMovies #FreeDownload #HDDownload #NowStreaming #MustWatch #BingeWatch #AfricanCinema #WatchFree #Trending"
        tw = "#Watch2D #Nollywood #NaijaMovies #AfricanMovies #FreeDownload"
    elif any(kw in combined for kw in ['turkish', 'turkey', 'dizi']):
        tg = "#Watch2D #TurkishSeries #TurkishDrama #Dizi #FreeDownload #HDDownload #NowStreaming #MustWatch #BingeWatch #EnglishSubtitles #WatchFree #Trending"
        tw = "#Watch2D #TurkishDrama #Dizi #TurkishSeries #FreeDownload"
    elif any(kw in combined for kw in ['indian', 'bollywood', 'hindi', 'telugu', 'tamil']):
        tg = "#Watch2D #Bollywood #IndianSeries #HindiSeries #FreeDownload #HDDownload #NowStreaming #MustWatch #IndianCinema #WatchFree #Trending"
        tw = "#Watch2D #Bollywood #IndianSeries #HindiSeries #FreeDownload"
    elif any(kw in combined for kw in ['chinese', 'china', 'cdrama']):
        tg = "#Watch2D #CDrama #ChineseDrama #ChineseSeries #AsianDrama #FreeDownload #HDDownload #NowStreaming #MustWatch #BingeWatch #WatchFree #Trending"
        tw = "#Watch2D #CDrama #ChineseDrama #AsianDrama #FreeDownload"
    elif any(kw in combined for kw in ['anime']):
        tg = "#Watch2D #Anime #AnimeDownload #AnimeSeries #FreeDownload #HDDownload #NowStreaming #MustWatch #BingeWatch #WatchFree #Trending"
        tw = "#Watch2D #Anime #AnimeDownload #FreeDownload"
    elif movie.is_series:
        tg = "#Watch2D #NewSeries #TVSeries #Series #NowStreaming #FreeDownload #HDDownload #MustWatch #BingeWatch #WatchFree #Trending"
        tw = "#Watch2D #TVSeries #NowStreaming #FreeDownload #BingeWatch"
    else:
        tg = "#Watch2D #NewMovie #Hollywood #FullMovie #FreeDownload #HDMovie #NowStreaming #MustWatch #MovieLovers #WatchFree #Trending"
        tw = "#Watch2D #NewMovie #Hollywood #FreeDownload #MustWatch"
    return tg, tw, tg


# ══════════════════════════════════════════════════════════════
# SOCIAL POSTERS
# ══════════════════════════════════════════════════════════════

def _post_movie_to_twitter(movie, is_new: bool):
    if not _limiter.can_post('twitter'):
        return
    try:
        from django.conf import settings
        site_url      = getattr(settings, 'SITE_URL', 'https://watch2d.org')
        url           = f"{site_url}/movies/movie/{movie.pk}/"
        _, tw_tags, _ = _detect_hashtags(movie)
        access_token  = _twitter_token_mgr.get_valid_token()
        if not access_token:
            return
        if is_new:
            emoji = "🎬" if not movie.is_series else "📺"
            cats  = movie.categories.all()
            genre = f"({', '.join(c.name for c in cats[:2])})" if cats else ""
            hook  = f"{emoji} {movie.title} {genre} is now FREE on Watch2D!"
            tweet_text = f"{hook}\n\n▶️ {url}\n\n{tw_tags}{TWITTER_FOOTER}"
        else:
            episode_label = movie.title_b or "New Episode"
            tweet_text = f"🆕 {movie.title}\nNew: {episode_label}\n\n▶️ Watch FREE → {url}\n\n{tw_tags}{TWITTER_FOOTER}"
        tweet_text = tweet_text[:280]
        resp = requests.post(
            'https://api.x.com/2/tweets',
            headers={'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'},
            json={'text': tweet_text}, timeout=15,
        )
        if resp.status_code == 201:
            _limiter.record('twitter')
            tprint(f"🐦 Twitter: {'NEW' if is_new else 'UPDATE'} posted — {movie.title}")
        else:
            tprint(f"⚠️ Twitter post failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        tprint(f"⚠️ Twitter post failed: {e}")


def _post_movie_to_facebook(movie, is_new: bool):
    if not _limiter.can_post('facebook'):
        return
    try:
        from django.conf import settings
        page_id      = getattr(settings, 'FB_PAGE_ID', '')
        access_token = getattr(settings, 'FB_ACCESS_TOKEN', '')
        if not all([page_id, access_token]):
            return
        site_url      = getattr(settings, 'SITE_URL', 'https://watch2d.org')
        url           = f"{site_url}/movies/movie/{movie.pk}/"
        _, _, fb_tags = _detect_hashtags(movie)
        if is_new:
            emoji = "🎬" if not movie.is_series else "📺"
            lines = [f"{emoji} {movie.title}", ""]
            if movie.description:
                lines += [f"{movie.description[:300]}...", ""]
            cats = movie.categories.all()
            if cats:
                lines.append(f"🏷 Genre: {', '.join(c.name for c in cats[:4])}")
            lines += ["", f"▶️ Watch FREE on Watch2D: {url}", "", fb_tags, FACEBOOK_FOOTER]
        else:
            episode_label = movie.title_b or "New Episode"
            lines = [
                "🆕 New Episode Available!", "",
                f"📺 {movie.title}", f"🎬 Episode: {episode_label}", "",
                f"▶️ Watch FREE Now: {url}", "", fb_tags, FACEBOOK_FOOTER,
            ]
        caption = "\n".join(lines)
        if movie.image_url:
            api_url = f"https://graph.facebook.com/v19.0/{page_id}/photos"
            data    = {"url": movie.image_url, "caption": caption, "access_token": access_token}
        else:
            api_url = f"https://graph.facebook.com/v19.0/{page_id}/feed"
            data    = {"message": caption, "access_token": access_token}
        res    = requests.post(api_url, data=data, timeout=15)
        result = res.json()
        if "error" in result:
            tprint(f"⚠️ Facebook post failed: {result['error'].get('message', result['error'])}")
        else:
            _limiter.record('facebook')
            tprint(f"📘 Facebook: {'NEW' if is_new else 'UPDATE'} posted — {movie.title}")
    except Exception as e:
        tprint(f"⚠️ Facebook post failed: {e}")


def _post_to_all_platforms(movie, is_new: bool):
    # ⚠️ Telegram is DISABLED — uncomment when ready
    # _post_movie_to_telegram(movie, is_new=is_new)
    _post_movie_to_twitter(movie,  is_new=is_new)
    _post_movie_to_facebook(movie, is_new=is_new)


# ══════════════════════════════════════════════════════════════
# SCRAPER SESSION
# ══════════════════════════════════════════════════════════════

def _make_scraper():
    scraper = cloudscraper.create_scraper()
    scraper.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": SITE_URL,
    })
    return scraper

# Thread-local scraper so each thread has its own session
_thread_local = threading.local()

def _get_scraper():
    if not hasattr(_thread_local, 'scraper'):
        _thread_local.scraper = _make_scraper()
    return _thread_local.scraper


# ══════════════════════════════════════════════════════════════
# PAGINATION DETECTION
# ══════════════════════════════════════════════════════════════

def get_total_pages(html: str) -> int | None:
    """
    Read the total page count from wp-pagenavi.
    9jarocks shows "Page 1 of 847" inside .wp-pagenavi .pages
    Returns page count if found, None means rely on 404 detection.
    """
    soup = BeautifulSoup(html, 'html.parser')

    # Method 1: wp-pagenavi "Page X of Y" text  ← most reliable
    pages_span = soup.select_one('.wp-pagenavi .pages')
    if pages_span:
        m = re.search(r'of\s+(\d+)', pages_span.get_text())
        if m:
            return int(m.group(1))

    # Method 2: highest page number in wp-pagenavi links
    max_page = 1
    for a in soup.select('.wp-pagenavi a[href]'):
        m = re.search(r'/page/(\d+)/', a.get('href', ''))
        if m:
            max_page = max(max_page, int(m.group(1)))
    if max_page > 1:
        return max_page

    # Method 3: can't determine total — return None, rely on 404
    # (never return 9999 — causes infinite loops)
    return None


# ══════════════════════════════════════════════════════════════
# POST URL EXTRACTION
# ══════════════════════════════════════════════════════════════

def get_post_urls_from_listing_page(html: str) -> list[str]:
    soup  = BeautifulSoup(html, 'html.parser')
    links = set()

    # Primary: article elements with post links
    for article in soup.select('article.tie-standard, .post-item, .mag-box .post-item'):
        for a in article.find_all('a', href=True):
            href = a['href']
            if SITE_URL in href and '/videodownload/' in href:
                links.add(href.rstrip('/'))

    # Fallback: any videodownload link
    if not links:
        for a in soup.find_all('a', href=True):
            href = a['href']
            if '/videodownload/' in href and href.startswith(SITE_URL):
                if '-id' in href or href.endswith('.html'):
                    links.add(href.rstrip('/'))

    return list(links)


# ══════════════════════════════════════════════════════════════
# POST PAGE PARSER
# ══════════════════════════════════════════════════════════════

def parse_post_page(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, 'html.parser')

    h1 = soup.find('h1', class_='post-title') or soup.find('h1', class_='entry-title')
    if not h1:
        return None
    title_raw = h1.get_text(strip=True)
    if not title_raw:
        return None

    # Categories
    categories = []
    for a in soup.select('a.post-cat'):
        name = a.get_text(strip=True)
        if name and name.lower() not in ('video', 'uncategorized'):
            categories.append(name)

    content_div = soup.find('div', class_='entry-content')
    if not content_div:
        return None

    # Featured image
    image_url = ''
    for img in content_div.find_all('img'):
        classes = ' '.join(img.get('class', []))
        src     = img.get('src') or img.get('data-src') or ''
        if src and 'thumb' not in src.lower() and 'screenshot' not in src.lower():
            if 'aligncenter' in classes or 'size-full' in classes:
                image_url = src
                break
    if not image_url:
        og = soup.find('meta', property='og:image')
        if og:
            image_url = og.get('content', '')

    # Video embed
    video_url = ''
    iframe     = content_div.find('iframe')
    if iframe and iframe.get('src'):
        video_url = iframe['src']

    # Description
    description = ''
    blockquote  = content_div.find('blockquote')
    if blockquote:
        for sib in blockquote.find_all_previous():
            if sib.name == 'p':
                text = sib.get_text(strip=True)
                if text and len(text) > 30 and 'mp4 download' not in text.lower():
                    description = text
                    break
    if not description:
        og_desc = soup.find('meta', property='og:description')
        if og_desc:
            description = og_desc.get('content', '').split('VIDEO INFORMATION')[0].strip()

    # Metadata
    meta = {}
    if blockquote:
        for line in blockquote.get_text('\n').splitlines():
            if ':' in line:
                key, _, val = line.partition(':')
                meta[key.strip().lower()] = val.strip()

    # Download links
    download_links = []
    seen_urls = set()

    for a in content_div.find_all('a', class_='fa-fa-download'):
        href  = a.get('href', '').strip()
        label = a.get_text(strip=True) or href
        if not href or href in seen_urls:
            continue
        if any(ad in href.lower() for ad in AD_DOMAINS):
            tprint(f"   🚫 [ad skipped] {label} → {href[:80]}")
            continue
        seen_urls.add(href)
        download_links.append({'url': href, 'label': label})

    # Fallback link detection
    if not download_links:
        for a in content_div.find_all('a', href=True):
            href       = a.get('href', '').strip()
            label      = a.get_text(strip=True) or href
            href_lower = href.lower()
            if not href or href.startswith('#') or 'javascript' in href_lower:
                continue
            if any(ad in href_lower for ad in AD_DOMAINS):
                continue
            if any(skip in href_lower for skip in [
                'facebook.com', 'twitter.com', 't.me', 'youtube.com/watch?',
                'imdb.com', 'wp-admin', '#respond', 'mailto:',
                '9jarocks.net/category', '9jarocks.net/tag',
            ]):
                continue
            is_dl = (
                any(d in href_lower for d in KNOWN_DOWNLOAD_DOMAINS)
                or any(href_lower.endswith(ext) for ext in FILE_EXTENSIONS)
                or any(kw in label.lower() for kw in DOWNLOAD_KEYWORDS)
                or any(kw in href_lower for kw in ['/dl/', '/get/', '/file/', 'download'])
            )
            if is_dl and href not in seen_urls:
                seen_urls.add(href)
                download_links.append({'url': href, 'label': label})

    is_series = bool(re.search(
        r'\bS\d{1,2}\b|\bSeason\s?\d{1,2}\b|\bEpisode\b|\bEp\.?\s?\d+\b|Series\b',
        title_raw, re.IGNORECASE
    ))
    is_complete = bool(re.search(r'\bcomplete(d)?\b', title_raw, re.IGNORECASE))

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
    }


# ══════════════════════════════════════════════════════════════
# TITLE CLEANING
# ══════════════════════════════════════════════════════════════

def clean_title_parts(raw: str) -> tuple[str, str]:
    title       = re.sub(r'\s+', ' ', raw).strip()
    title_lower = title.lower()
    is_complete = bool(re.search(r'\bcomplete(d)?\b', title_lower))

    series_re = re.compile(r'(?i)(.*?\b(?:S\d{1,2}|Season\s?\d{1,2}))[\s\-–|:]*\s*(.*)')
    m = series_re.match(title)
    if m:
        base   = m.group(1).strip()
        ep_lbl = m.group(2).strip()
        ep_lbl = re.sub(r'\s*[\-–|:]*\s*\bcomplete(d)?\b', '', ep_lbl, flags=re.IGNORECASE).strip()
        if is_complete and 'complete' not in base.lower():
            suffix = '(Completed)' if 'completed' in title_lower else '(Complete)'
            base   = f"{base} {suffix}"
        return base, ep_lbl

    year_m = re.search(r'^(.*?\(\d{4}\))', title)
    if year_m:
        return year_m.group(1).strip(), ''

    clean = re.sub(r'\s*[\-–|:]*\s*\bcomplete(d)?\b\s*$', '', title, flags=re.IGNORECASE).strip()
    if is_complete:
        suffix = '(Completed)' if 'completed' in title_lower else '(Complete)'
        clean  = f"{clean} {suffix}"
    return clean, ''


# ══════════════════════════════════════════════════════════════
# DB HELPERS
# ══════════════════════════════════════════════════════════════

def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    return unquote(f"{parsed.scheme}://{parsed.netloc}{parsed.path}").lower()


_db_lock = threading.Lock()


def extract_series_base(title: str) -> str:
    """
    Strip episode/complete suffixes to get the bare series name.

    Examples:
      "Kimse Bilmez Season 1 (Episode 1-8 Added)"  → "Kimse Bilmez Season 1"
      "Kimse Bilmez Season 1 (Complete)"            → "Kimse Bilmez Season 1"
      "Kimse Bilmez Season 1 (Completed)"           → "Kimse Bilmez Season 1"
      "Love is Deep Season 1 (Episode 23-35 Added)" → "Love is Deep Season 1"
      "Monica 2 (2026)"                             → "Monica 2 (2026)"   ← movie, unchanged
    """
    # Remove trailing episode/complete markers
    cleaned = re.sub(
        r'\s*[\(\[]?\s*(?:Episode\s*[\d\s\-–,]+(?:Added|End)?|Complete[d]?|Batch)\s*[\)\]]?\s*$',
        '', title, flags=re.IGNORECASE
    ).strip()
    # Remove trailing punctuation left over (dash, pipe, colon)
    cleaned = re.sub(r'[\s\-–|:]+$', '', cleaned).strip()
    return cleaned or title


def find_existing_movie(title: str, max_retries: int = 3):
    """
    Find a matching Movie in the DB. Tries multiple title variants so that
    series whose episode label has changed are still matched correctly.

    Match priority:
      1. Exact title match
      2. Base title (no episode/complete suffix) exact match
      3. DB title starts with the same series base  (catches episode updates)
      4. Source URL match (if field exists)
    """
    from django.db import connection

    # Build variant list
    base = re.sub(r'\s*\((complete|completed)\)\s*$', '', title, flags=re.IGNORECASE).strip()
    series_base = extract_series_base(title)

    exact_variants = list(dict.fromkeys([
        title,
        base,
        f"{base} (Complete)",
        f"{base} (Completed)",
        series_base,
        f"{series_base} (Complete)",
        f"{series_base} (Completed)",
    ]))

    for attempt in range(max_retries):
        try:
            # 1 & 2: exact / complete-suffix variants
            movie = Movie.objects.filter(title__in=exact_variants).first()
            if movie:
                return movie

            # 3: series base prefix match — catches episode-count changes
            # e.g. DB has "Kimse Bilmez Season 1 (Episode 1-8 Added)"
            #      new title is "Kimse Bilmez Season 1 (Episode 1-12 Added)"
            # Both share series_base "Kimse Bilmez Season 1"
            if series_base and series_base != title:
                movie = Movie.objects.filter(
                    title__istartswith=series_base
                ).first()
                if movie:
                    return movie

            return None

        except Exception as e:
            tprint(f"   ⚠️ DB error (attempt {attempt+1}): {e}")
            if attempt < max_retries - 1:
                connection.close()
                time.sleep(2 ** attempt)
            else:
                raise
    return None


def extract_episode_range(label: str) -> tuple[int, int] | tuple[None, None]:
    """
    Parse an episode label like "Episode 1-8 Added" → (1, 8)
    or "Episode 12" → (12, 12).
    Returns (None, None) if no episode range found.
    """
    m = re.search(r'episode\s*(\d+)\s*[-–]\s*(\d+)', label, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r'episode\s*(\d+)', label, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        return n, n
    return None, None


def is_episode_update(old_label: str, new_label: str) -> bool:
    """
    Returns True if new_label represents more episodes than old_label.
    e.g. old="Episode 1-8 Added", new="Episode 1-12 Added" → True
    """
    if not new_label:
        return False  # No new label = nothing to announce
    _, old_end = extract_episode_range(old_label or '')
    _, new_end = extract_episode_range(new_label or '')
    if old_end is None or new_end is None:
        # Can't compare numerically — treat any label change as an update
        return old_label != new_label
    return new_end > old_end


def get_scraped_urls() -> set:
    """
    Return the set of source URLs already in the DB.
    Used to skip posts we've already scraped on resume.
    
    NOTE: This requires a `source_url` field on your Movie model.
    If you don't have it yet, add this migration:
    
        source_url = models.URLField(max_length=500, blank=True, default='')
    
    Without this field, resume support is disabled and all posts are re-checked
    (still fast because find_existing_movie() skips the DB write).
    """
    try:
        urls = Movie.objects.filter(scraped=True).exclude(source_url='').values_list('source_url', flat=True)
        return set(urls)
    except Exception:
        # source_url field doesn't exist yet — resume disabled
        return set()


# ══════════════════════════════════════════════════════════════
# SINGLE POST PROCESSOR  (called from thread pool)
# ══════════════════════════════════════════════════════════════

def _dispatch_social(movie, is_new: bool, no_social: bool, telegram_only: bool):
    """Central social dispatch — respects --no-social and --telegram-only."""
    if no_social:
        return
    if telegram_only:
        _post_movie_to_telegram(movie, is_new=is_new)
    else:
        _post_to_all_platforms(movie, is_new=is_new)


def process_post(post_url: str, no_social: bool, scraped_urls: set, telegram_only: bool = False) -> dict:
    """
    Fetch + parse + save one post. Returns status dict.
    Safe to call from multiple threads.
    """
    result = {'url': post_url, 'status': 'skipped', 'title': ''}

    # Resume: skip if already in DB
    if post_url.rstrip('/') in scraped_urls or post_url in scraped_urls:
        result['status'] = 'skipped_resume'
        return result

    scraper = _get_scraper()

    try:
        post_resp = scraper.get(post_url, timeout=25)
        if post_resp.status_code != 200:
            result['status'] = f'http_{post_resp.status_code}'
            return result
        post_html = post_resp.text
    except Exception as e:
        result['status'] = f'fetch_error'
        result['error']  = str(e)
        return result

    parsed = parse_post_page(post_html, post_url)
    if not parsed:
        result['status'] = 'parse_failed'
        return result

    if not parsed['download_links']:
        result['status'] = 'no_links'
        result['title']  = parsed.get('title_raw', '')
        return result

    title, title_b = clean_title_parts(parsed['title_raw'])
    result['title'] = title

    # ── DB write (serialized with lock) ───────────────────────
    from django.db import connection

    with _db_lock:
        try:
            movie   = find_existing_movie(title)
            created = False
            updated = False

            if not movie:
                movie = Movie.objects.create(
                    title              = title,
                    title_b            = title_b,
                    title_b_updated_at = timezone.now() if title_b else None,
                    description        = parsed['description'],
                    video_url          = parsed['video_url'],
                    download_url       = parsed['download_links'][0]['url'],
                    image_url          = parsed['image_url'],
                    completed          = parsed['is_complete'],
                    is_series          = parsed['is_series'],
                    scraped            = True,
                    # source_url       = post_url,  # uncomment after adding field
                )
                created = True

                _dispatch_social(movie, is_new=True, no_social=no_social, telegram_only=telegram_only)

            else:
                # ── Series title update ────────────────────────────────
                # The stored title may have an old episode label, e.g.:
                #   DB:   "Kimse Bilmez Season 1 (Episode 1-8 Added)"
                #   Now:  "Kimse Bilmez Season 1 (Episode 1-12 Added)"
                # We always update to the latest full title from the page.
                if movie.title != title:
                    tprint(f"   📝 Title updated: '{movie.title}' → '{title}'")
                    movie.title = title
                    updated = True

                # ── Episode label (title_b) update ─────────────────────
                # title_b holds the part after "Season N", e.g. "Episode 1-8 Added"
                # Fire social post only when episode count actually increases.
                new_ep_is_bigger = is_episode_update(movie.title_b or '', title_b or '')

                if title_b and movie.title_b != title_b:
                    movie.title_b            = title_b
                    movie.title_b_updated_at = timezone.now()
                    updated = True

                    if new_ep_is_bigger:
                        tprint(f"   🆕 New episodes: {movie.title_b!r} → {title_b!r}")
                        # Also update download_url to point at the newest episode's first link
                        if parsed['download_links']:
                            movie.download_url = parsed['download_links'][0]['url']
                        _dispatch_social(movie, is_new=False, no_social=no_social, telegram_only=telegram_only)
                    else:
                        tprint(f"   🔄 Episode label changed (no new eps): {title_b!r}")

                # ── Completion status ──────────────────────────────────
                if not movie.completed and parsed['is_complete']:
                    movie.completed = True
                    updated = True
                    tprint(f"   ✅ Series marked COMPLETE: {title}")
                    _dispatch_social(movie, is_new=False, no_social=no_social, telegram_only=telegram_only)

                # ── Other field fills ──────────────────────────────────
                if not movie.video_url and parsed['video_url']:
                    movie.video_url = parsed['video_url']
                    updated = True
                if not movie.image_url and parsed['image_url']:
                    movie.image_url = parsed['image_url']
                    updated = True
                if not getattr(movie, 'is_series', False) and parsed['is_series']:
                    movie.is_series = parsed['is_series']
                    updated = True

                if updated:
                    movie.save()

            # ── Categories ────────────────────────────────────────────
            for cat_name in parsed['categories']:
                cat_obj, _ = Category.objects.get_or_create(name=cat_name.strip().capitalize())
                movie.categories.add(cat_obj)

            # ── Download link sync ─────────────────────────────────────
            # For SERIES we ACCUMULATE links (never delete old episode links).
            # For MOVIES we sync (remove stale links, add new ones).
            #
            # Logic:
            #   - Always add links that don't exist yet (by normalized URL).
            #   - For movies: delete links no longer on the page.
            #   - For series: keep all historical links; only add new ones.
            existing = {normalize_url(dl.url): dl for dl in movie.download_links.all()}
            current  = {normalize_url(dl['url']): dl for dl in parsed['download_links']}
            added    = 0

            for norm, dl in current.items():
                if norm not in existing:
                    DownloadLink.objects.create(movie=movie, label=dl['label'], url=dl['url'])
                    added += 1
                elif existing[norm].label != dl['label']:
                    # Update label if it changed (e.g. quality tag added)
                    existing[norm].label = dl['label']
                    existing[norm].save()

            if not (parsed['is_series'] or getattr(movie, 'is_series', False)):
                # Movie: remove links that disappeared from the page
                for norm in set(existing) - set(current):
                    existing[norm].delete()
            # Series: intentionally keep old episode links even if they're
            # no longer listed on the page (9jarocks sometimes rotates them)

            result['status']  = 'created' if created else ('updated' if updated else 'unchanged')
            result['added']   = added
            result['n_links'] = len(parsed['download_links'])

        except Exception as e:
            tprint(f"   💥 DB error for {post_url}: {e}")
            import traceback; traceback.print_exc()
            connection.close()
            result['status'] = 'db_error'
            result['error']  = str(e)

    return result


# ══════════════════════════════════════════════════════════════
# MANAGEMENT COMMAND
# ══════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = (
        'Scrape 9jarocks.net category pages → save to DB → '
        'optionally post to Twitter + Facebook'
    )

    def add_arguments(self, parser):
        parser.add_argument('--startpage', type=int, default=1,
                            help='Category page to start from (default: 1)')
        parser.add_argument('--endpage',   type=int, default=None,
                            help='Stop after this page number (inclusive)')
        parser.add_argument('--max-pages', type=int, default=None,
                            help='Max listing pages to crawl per category')
        parser.add_argument('--category',  type=str, default=None,
                            help='Single category slug, e.g. "nollywood-movie"')
        parser.add_argument('--no-social', action='store_true', default=False,
                            help='Skip all social posts')
        parser.add_argument('--telegram-only', action='store_true', default=False,
                            help='Post to Telegram only — skip Twitter & Facebook')
        parser.add_argument('--delay',   type=float, default=0.0,
                            help='Seconds between BATCHES (default: 0 — fast)')
        parser.add_argument('--workers', type=int, default=4,
                            help='Parallel post fetchers (default: 4)')
        parser.add_argument('--no-resume', action='store_true', default=False,
                            help='Ignore already-scraped URLs — re-check everything')

    def handle(self, *args, **options):
        start_page = options['startpage']
        end_page   = options['endpage']
        max_pages  = options['max_pages']
        no_social     = options['no_social']
        telegram_only = options['telegram_only']
        delay         = options['delay']
        workers    = options['workers']
        cat_slug   = options.get('category')
        no_resume  = options['no_resume']

        if cat_slug:
            cats_to_crawl = [f"videodownload/{cat_slug}" if '/' not in cat_slug else cat_slug]
        else:
            cats_to_crawl = CATEGORIES

        # Load already-scraped URLs for resume support
        scraped_urls: set = set() if no_resume else get_scraped_urls()

        print("=" * 65)
        print("🚀  9jarocks.net scraper — FAST PARALLEL MODE")
        print(f"    Categories : {', '.join(cats_to_crawl)}")
        print(f"    Pages      : {start_page} → {end_page or '∞'}")
        print(f"    Workers    : {workers} parallel fetchers")
        print(f"    Batch delay: {delay}s")
        print(f"    Resume     : {'OFF (--no-resume)' if no_resume else f'ON ({len(scraped_urls):,} URLs already done)'}")
        social_mode = 'DISABLED' if no_social else ('Telegram only' if telegram_only else 'Telegram + Twitter + Facebook')
        print(f"    Social     : {social_mode}")
        print("=" * 65)

        main_scraper = _make_scraper()

        total_new       = 0
        total_updated   = 0
        total_skipped   = 0
        total_processed = 0

        for cat_slug_full in cats_to_crawl:
            cat_base_url = f"{SITE_URL}/category/{cat_slug_full}"
            print(f"\n\n{'═'*65}")
            print(f"📂  {cat_slug_full}  →  {cat_base_url}")
            print(f"{'═'*65}")

            page          = start_page
            pages_crawled  = 0
            total_pages    = None  # will be detected from first page
            page_retries   = 0
            MAX_PAGE_RETRIES = 3

            while True:
                if end_page and page > end_page:
                    print(f"\n✅ Reached end page {end_page}.")
                    break
                if max_pages and pages_crawled >= max_pages:
                    print(f"\n✅ Crawled {max_pages} pages for this category.")
                    break
                if total_pages and page > total_pages:
                    print(f"\n✅ All {total_pages} pages done for '{cat_slug_full}'.")
                    break

                listing_url = (
                    f"{cat_base_url}/" if page == 1
                    else f"{cat_base_url}/page/{page}/"
                )
                print(f"\n{'─'*65}")
                print(f"🌐  Page {page}{f'/{total_pages}' if total_pages else ''}: {listing_url}")

                try:
                    resp = main_scraper.get(listing_url, timeout=30)
                    if resp.status_code == 404:
                        print(f"   ✅ 404 — no more pages.")
                        break
                    resp.raise_for_status()
                    html = resp.text
                    page_retries = 0  # reset on success
                except Exception as e:
                    page_retries += 1
                    wait = min(5 * page_retries, 30)  # 5s, 10s, 30s max
                    print(f"   ❌ Listing fetch error (attempt {page_retries}/{MAX_PAGE_RETRIES}): {e}")
                    if page_retries >= MAX_PAGE_RETRIES:
                        print(f"   ⚠️ Skipping page {page} after {MAX_PAGE_RETRIES} failures.")
                        page += 1
                        page_retries = 0
                    else:
                        print(f"   ⏳ Retrying same page in {wait}s...")
                        time.sleep(wait)
                    continue

                # Detect total pages on first listing fetch for this category
                if total_pages is None:
                    total_pages = get_total_pages(html)
                    if total_pages and total_pages > 1:
                        print(f"   📊 Total pages for this category: {total_pages}")

                pages_crawled += 1
                post_urls = get_post_urls_from_listing_page(html)
                print(f"   📋 {len(post_urls)} posts on this page")

                if not post_urls:
                    print("   ⚠️ No posts found — stopping category.")
                    break

                # ── Parallel post processing ───────────────────
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {
                        executor.submit(process_post, url, no_social, scraped_urls, telegram_only): url
                        for url in post_urls
                    }
                    for future in as_completed(futures):
                        r = future.result()
                        total_processed += 1

                        if r['status'] == 'skipped_resume':
                            total_skipped += 1
                            continue
                        elif r['status'] == 'created':
                            total_new += 1
                            tprint(f"   ✅ NEW    {r['title']}  ({r.get('n_links',0)} links, +{r.get('added',0)} new)")
                        elif r['status'] == 'updated':
                            total_updated += 1
                            tprint(f"   🔄 UPD    {r['title']}")
                        elif r['status'] == 'unchanged':
                            tprint(f"   ✔  OK     {r['title']}")
                        elif r['status'] == 'no_links':
                            tprint(f"   ⛔ SKIP   {r['title']} (no download links)")
                        elif r['status'] == 'skipped_resume':
                            pass
                        else:
                            tprint(f"   ❌ {r['status'].upper():10} {r['url']} — {r.get('error','')[:60]}")

                if delay > 0:
                    time.sleep(delay)

                page += 1

        print(f"\n\n{'='*65}")
        print(f"🎉  Done!")
        print(f"    Processed : {total_processed}")
        print(f"    New       : {total_new}")
        print(f"    Updated   : {total_updated}")
        print(f"    Skipped   : {total_skipped} (resume / already done)")
        print(f"    {_limiter.stats()}")
        print("=" * 65)