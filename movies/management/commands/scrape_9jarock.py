"""
Management command: scrape_9jarocks
Scrapes 9jarocks.net by crawling category listing pages, then visiting
each post page to extract title, image, description, and download links.

WHY HTML scraping (not REST API):
  • /wp/v2/posts returns 404 — blocked by the site
  • /wp/v2/pages only has ~10 static pages (Privacy, About, etc.)
  • All actual content lives in /category/videodownload/page/N/

HTML structure (confirmed from live page source):
  • Download links : <a class="fa-fa-download" href="...">
  • Featured image : <p><img class="aligncenter ...">  inside .entry-content
  • Video embed    : <iframe ...> inside .entry-content
  • Post metadata  : <blockquote> containing Filename, Director, Stars, etc.
  • Categories     : <a class="post-cat ..."> in .entry-header

Usage examples
──────────────
# Scrape → Django DB + WordPress + Social (default, all enabled)
python manage.py scrape_9jarocks

# Scrape → Django DB only (skip WordPress and social)
python manage.py scrape_9jarocks --no-wordpress --no-social

# Scrape → WordPress only (skip Django DB and social)
python manage.py scrape_9jarocks --no-django --no-social

# Scrape → Django DB + WordPress, skip social
python manage.py scrape_9jarocks --no-social

# Sync existing Django movies → WordPress (no scraping)
python manage.py scrape_9jarocks --sync-wordpress --no-social

# Other options
python manage.py scrape_9jarocks --startpage 5
python manage.py scrape_9jarocks --startpage 1 --endpage 10
python manage.py scrape_9jarocks --category nollywood-movie
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from movies.models import Movie, Category, DownloadLink
import requests
from bs4 import BeautifulSoup
import re
import cloudscraper
from urllib.parse import urlparse, urljoin, unquote
import urllib3
import time
import base64

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ══════════════════════════════════════════════════════════════
# SITE CONSTANTS
# ══════════════════════════════════════════════════════════════

SITE_URL     = 'https://9jarocks.net'

# All category slugs — scraper visits each one
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

# ── Ad / monetization redirect domains to SKIP ───────────────
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
    '9jarocks.net/download',
]

FILE_EXTENSIONS = ['.mp4', '.mkv', '.avi', '.mov', '.zip', '.rar', '.srt']

DOWNLOAD_KEYWORDS = [
    'download', '480p', '720p', '1080p', '4k', 'hd',
    'episode', 'fast server', 'slow server', 'mirror',
    'part ', 'batch',
]


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
# WORDPRESS PUBLISHER
# ══════════════════════════════════════════════════════════════

# ── WordPress credentials ─────────────────────────────────────
# Store these in your Django settings.py or .env:
#   WP_SITE_URL      = 'https://naijadeleys.com.ng'
#   WP_USERNAME      = 'AlphaDev_'
#   WP_APP_PASSWORD  = 'scK9 fIaZ FUmY tDWo Mhqb rXbq'

def _get_wp_auth_header() -> dict:
    """Build the Basic Auth header from Django settings."""
    from django.conf import settings
    username = getattr(settings, 'WP_USERNAME', 'AlphaDev_')
    password = getattr(settings, 'WP_APP_PASSWORD', 'scK9 fIaZ FUmY tDWo Mhqb rXbq')
    token    = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {
        'Authorization': f'Basic {token}',
        'Content-Type':  'application/json',
    }


def _get_wp_base_url() -> str:
    from django.conf import settings
    return getattr(settings, 'WP_SITE_URL', 'https://naijadeleys.com.ng').rstrip('/')


def _build_wp_post_content(parsed: dict, title: str, title_b: str) -> str:
    """
    Build the HTML content for the WordPress post.
    Includes: description, metadata block, styled download buttons.
    """
    parts = []

    # ── Featured image (shown inline if WP featured image upload fails) ──
    if parsed.get('image_url'):
        parts.append(
            f'<p style="text-align:center">'
            f'<img src="{parsed["image_url"]}" alt="{title}" '
            f'style="max-width:100%;height:auto;border-radius:8px;" /></p>'
        )

    # ── Episode label ─────────────────────────────────────────
    if title_b:
        parts.append(
            f'<p style="background:#1a1a2e;color:#e94560;padding:10px 16px;'
            f'border-radius:6px;font-weight:bold;font-size:15px;">'
            f'🎬 Now Available: {title_b}</p>'
        )

    # ── Description ───────────────────────────────────────────
    if parsed.get('description'):
        parts.append(
            f'<div style="background:#f9f9f9;border-left:4px solid #e94560;'
            f'padding:12px 16px;margin:16px 0;border-radius:0 6px 6px 0;">'
            f'<p>{parsed["description"]}</p></div>'
        )

    # ── Metadata table ────────────────────────────────────────
    meta = parsed.get('meta', {})
    if meta:
        rows = ''.join(
            f'<tr><td style="font-weight:bold;padding:6px 12px;width:35%;'
            f'background:#f0f0f0;">{k.title()}</td>'
            f'<td style="padding:6px 12px;">{v}</td></tr>'
            for k, v in meta.items() if v
        )
        if rows:
            parts.append(
                f'<table style="width:100%;border-collapse:collapse;'
                f'margin:16px 0;font-size:14px;border:1px solid #ddd;">'
                f'<tbody>{rows}</tbody></table>'
            )

    # ── Download buttons ──────────────────────────────────────
    dl_links = parsed.get('download_links', [])
    if dl_links:
        parts.append(
            '<div style="margin:24px 0;">'
            '<p style="font-weight:bold;font-size:16px;margin-bottom:12px;">'
            '⬇️ Download Links</p>'
        )
        for i, dl in enumerate(dl_links, 1):
            label = dl.get('label') or f'Download Link {i}'
            url   = dl['url']
            # Alternate button colours for visual variety
            bg    = '#e94560' if i % 2 == 1 else '#1a1a2e'
            parts.append(
                f'<a href="{url}" target="_blank" rel="nofollow noopener" '
                f'style="display:inline-block;background:{bg};color:#fff;'
                f'padding:12px 24px;border-radius:6px;text-decoration:none;'
                f'font-weight:bold;margin:4px 6px 4px 0;font-size:14px;">'
                f'⬇️ {label}</a>'
            )
        parts.append('</div>')

    # ── Video embed ───────────────────────────────────────────
    if parsed.get('video_url'):
        parts.append(
            f'<div style="position:relative;padding-bottom:56.25%;height:0;'
            f'overflow:hidden;margin:20px 0;">'
            f'<iframe src="{parsed["video_url"]}" frameborder="0" allowfullscreen '
            f'style="position:absolute;top:0;left:0;width:100%;height:100%;">'
            f'</iframe></div>'
        )

    return '\n'.join(parts)


# In-memory cache: category name → WP ID (avoids repeated API calls per run)
_wp_category_cache: dict = {}


def _wp_get_or_create_category(cat_name: str, headers: dict, wp_base: str) -> int | None:
    """Get WP category ID by name, creating it if needed. Cached per run."""
    key = cat_name.strip().lower()
    if key in _wp_category_cache:
        return _wp_category_cache[key]
    try:
        r = requests.get(
            f'{wp_base}/wp-json/wp/v2/categories',
            params={'search': cat_name, 'per_page': 5},
            headers=headers, timeout=10,
        )
        if r.status_code == 200:
            for cat in r.json():
                if cat['name'].lower() == key:
                    _wp_category_cache[key] = cat['id']
                    return cat['id']
        r = requests.post(
            f'{wp_base}/wp-json/wp/v2/categories',
            headers=headers,
            json={'name': cat_name},
            timeout=10,
        )
        if r.status_code == 201:
            cid = r.json().get('id')
            _wp_category_cache[key] = cid
            return cid
    except Exception as e:
        print(f"      ⚠️ WP category error ({cat_name}): {e}")
    return None


def _wp_upload_image(image_url: str, title: str, headers: dict, wp_base: str) -> int | None:
    """Download an image and upload it to WordPress media library."""
    try:
        img_resp = requests.get(image_url, timeout=15, stream=True)
        if img_resp.status_code != 200:
            return None

        # Detect content type
        content_type = img_resp.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip()
        ext_map = {
            'image/jpeg': 'jpg', 'image/jpg': 'jpg',
            'image/png': 'png', 'image/webp': 'webp',
            'image/gif': 'gif',
        }
        ext      = ext_map.get(content_type, 'jpg')
        filename = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-') + f'.{ext}'

        upload_headers = {**headers}
        upload_headers['Content-Type']        = content_type
        upload_headers['Content-Disposition'] = f'attachment; filename="{filename}"'

        r = requests.post(
            f'{wp_base}/wp-json/wp/v2/media',
            headers=upload_headers,
            data=img_resp.content,
            timeout=30,
        )
        if r.status_code == 201:
            media_id = r.json().get('id')
            print(f"      🖼️  WP image uploaded → ID {media_id}")
            return media_id
        else:
            print(f"      ⚠️ WP image upload failed: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"      ⚠️ WP image upload error: {e}")
    return None


def _wp_find_existing_post(title: str, headers: dict, wp_base: str) -> dict | None:
    """Search WordPress for an existing post with this title."""
    try:
        r = requests.get(
            f'{wp_base}/wp-json/wp/v2/posts',
            params={'search': title, 'per_page': 5, 'status': 'any'},
            headers=headers, timeout=10,
        )
        if r.status_code == 200:
            for post in r.json():
                # Compare rendered title (strips HTML entities)
                rendered = BeautifulSoup(post['title']['rendered'], 'html.parser').get_text()
                if rendered.strip().lower() == title.strip().lower():
                    return post
    except Exception as e:
        print(f"      ⚠️ WP search error: {e}")
    return None


def _post_to_wordpress(movie, parsed: dict, title: str, title_b: str,
                        is_new: bool, categories: list,
                        skip_existence_check: bool = False) -> bool:
    """
    Create or update a post on naijadeleys.com.ng via the WordPress REST API.

    - NEW movie  → creates a full post with image, content, categories
    - UPDATE     → updates content (new download links / episode label only)

    Returns True on success, False on failure.
    """
    try:
        headers = _get_wp_auth_header()
        wp_base = _get_wp_base_url()

        # ── Build content ──────────────────────────────────────
        content = _build_wp_post_content(parsed, title, title_b)

        # ── Category IDs ───────────────────────────────────────
        cat_ids = []
        for cat_name in categories:
            cid = _wp_get_or_create_category(cat_name.strip().capitalize(), headers, wp_base)
            if cid:
                cat_ids.append(cid)

        # ── Check if post already exists (skipped when scraping fresh) ──
        if not skip_existence_check:
            existing_post = _wp_find_existing_post(title, headers, wp_base)
            if existing_post:
                if not is_new:
                    post_id = existing_post['id']
                    r = requests.post(
                        f'{wp_base}/wp-json/wp/v2/posts/{post_id}',
                        headers=headers,
                        json={'content': content},
                        timeout=15,
                    )
                    if r.status_code == 200:
                        print(f"      📝 WP updated (episode/links) → ID {post_id}")
                        return True
                    else:
                        print(f"      ⚠️ WP update failed: {r.status_code} {r.text[:120]}")
                        return False
                else:
                    print(f"      ℹ️  WP post already exists — skipping create")
                    return True

        # ── CREATE new post ─────────────────────────────────────
        # 1. Upload featured image
        featured_media_id = None
        if parsed.get('image_url'):
            featured_media_id = _wp_upload_image(parsed['image_url'], title, headers, wp_base)

        # 2. Build post payload
        post_data = {
            'title':   title,
            'content': content,
            'status':  'publish',
            'categories': cat_ids if cat_ids else [],
        }
        if featured_media_id:
            post_data['featured_media'] = featured_media_id

        # 3. Create the post
        r = requests.post(
            f'{wp_base}/wp-json/wp/v2/posts',
            headers=headers,
            json=post_data,
            timeout=20,
        )
        if r.status_code == 201:
            wp_post_id = r.json().get('id')
            wp_link    = r.json().get('link', '')
            print(f"      ✅ WP created → ID {wp_post_id} | {wp_link}")
            return True
        elif r.status_code == 400 and 'already exists' in r.text.lower():
            print(f"      ℹ️  WP slug already exists — skipping")
            return True
        else:
            print(f"      ⚠️ WP create failed: {r.status_code} {r.text[:200]}")
            return False

    except Exception as e:
        print(f"      💥 WP publisher error: {e}")
        import traceback; traceback.print_exc()
        return False


# ══════════════════════════════════════════════════════════════
# RATE LIMITER
# ══════════════════════════════════════════════════════════════

class _RateLimiter:
    def __init__(self):
        self._counts    = {'facebook': 0, 'twitter': 0}
        self._last_post = {'facebook': 0.0, 'twitter': 0.0}
        self._min_gap   = {'facebook': 45, 'twitter': 60}
        self._run_cap   = {'facebook': 80, 'twitter': 40}

    def can_post(self, platform: str) -> bool:
        if platform not in self._counts:
            return True
        if self._counts[platform] >= self._run_cap[platform]:
            print(f"⚠️ {platform.title()} run cap ({self._run_cap[platform]}) reached — skipping.")
            return False
        elapsed = time.time() - self._last_post[platform]
        gap     = self._min_gap[platform]
        if elapsed < gap:
            wait = gap - elapsed
            print(f"⏳ {platform.title()} rate limit — waiting {wait:.0f}s...")
            time.sleep(wait)
        return True

    def record(self, platform: str):
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
# TWITTER OAuth 2.0 TOKEN MANAGER
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
            print(f"⚠️ SAVE MANUALLY → TWITTER_REFRESH_TOKEN={new_token}")
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
            print(f"✅ Twitter: New refresh token saved to {env_path}")
        except Exception as e:
            print(f"⚠️ SAVE MANUALLY → TWITTER_REFRESH_TOKEN={new_token}  ({e})")

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
            print("⚠️ Twitter OAuth 2.0 credentials missing — skipping.")
            return None

        print("🔄 Twitter: Refreshing access token...")
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
            print(f"⚠️ Twitter token refresh failed: {data}")
        except Exception as e:
            print(f"⚠️ Twitter token refresh error: {e}")
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
# TWITTER POSTER
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
            print(f"🐦 Twitter: {'NEW' if is_new else 'UPDATE'} posted — {movie.title}")
        else:
            print(f"⚠️ Twitter post failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"⚠️ Twitter post failed: {e}")


# ══════════════════════════════════════════════════════════════
# FACEBOOK POSTER
# ══════════════════════════════════════════════════════════════

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
            print(f"⚠️ Facebook post failed: {result['error'].get('message', result['error'])}")
        else:
            _limiter.record('facebook')
            print(f"📘 Facebook: {'NEW' if is_new else 'UPDATE'} posted — {movie.title}")
    except Exception as e:
        print(f"⚠️ Facebook post failed: {e}")


# ══════════════════════════════════════════════════════════════
# MASTER POSTER
# ══════════════════════════════════════════════════════════════

def _post_to_all_platforms(movie, parsed: dict, title: str, title_b: str,
                            categories: list, is_new: bool, no_wordpress: bool):
    # WordPress (naijadeleys.com.ng)
    if not no_wordpress:
        _post_to_wordpress(movie, parsed, title, title_b, is_new, categories)

    # ⚠️  Telegram is DISABLED — uncomment below line when ready
    # _post_movie_to_telegram(movie, is_new=is_new)
    _post_movie_to_twitter(movie,  is_new=is_new)
    _post_movie_to_facebook(movie, is_new=is_new)


# ══════════════════════════════════════════════════════════════
# HTML PARSERS
# ══════════════════════════════════════════════════════════════

def _make_scraper():
    """Return a cloudscraper session with browser-like headers."""
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


def get_post_urls_from_listing_page(html: str, base_url: str) -> list[str]:
    soup  = BeautifulSoup(html, 'html.parser')
    links = set()

    for article in soup.select('article.tie-standard, .post-item, .mag-box .post-item'):
        for a in article.find_all('a', href=True):
            href = a['href']
            if SITE_URL in href and '/videodownload/' in href:
                links.add(href.rstrip('/'))

    if not links:
        for a in soup.find_all('a', href=True):
            href = a['href']
            if '/videodownload/' in href and href.startswith(SITE_URL):
                if '-id' in href or href.endswith('.html'):
                    links.add(href.rstrip('/'))

    return list(links)


def has_next_page(html: str) -> bool:
    soup = BeautifulSoup(html, 'html.parser')
    for a in soup.find_all('a', href=True):
        text = a.get_text(strip=True).lower()
        cls  = ' '.join(a.get('class', []))
        if text in ('next', '»', 'next page') or 'next' in cls or 'nextpostslink' in cls:
            return True
    return False


def parse_post_page(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, 'html.parser')

    h1 = soup.find('h1', class_='post-title')
    if not h1:
        h1 = soup.find('h1', class_='entry-title')
    if not h1:
        return None
    title_raw = h1.get_text(strip=True)
    if not title_raw:
        return None

    categories = []
    for a in soup.select('a.post-cat'):
        name = a.get_text(strip=True)
        if name and name.lower() not in ('video', 'uncategorized'):
            categories.append(name)
    if not categories:
        breadcrumb = soup.find(id='breadcrumb')
        if breadcrumb:
            crumb_links = breadcrumb.find_all('a')
            for a in crumb_links[1:]:
                name = a.get_text(strip=True)
                if name.lower() not in ('video', 'home'):
                    categories.append(name)

    content_div = soup.find('div', class_='entry-content')
    if not content_div:
        return None

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

    video_url = ''
    iframe     = content_div.find('iframe')
    if iframe and iframe.get('src'):
        video_url = iframe['src']

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

    meta = {}
    if blockquote:
        bq_text = blockquote.get_text('\n')
        for line in bq_text.splitlines():
            if ':' in line:
                key, _, val = line.partition(':')
                meta[key.strip().lower()] = val.strip()

    download_links = []
    seen_urls = set()

    for a in content_div.find_all('a', class_='fa-fa-download'):
        href  = a.get('href', '').strip()
        label = a.get_text(strip=True) or href
        if not href or href in seen_urls:
            continue
        if any(ad in href.lower() for ad in AD_DOMAINS):
            print(f"   🚫 [ad skipped] {label} → {href[:80]}")
            continue
        seen_urls.add(href)
        download_links.append({'url': href, 'label': label})
        print(f"   🔗 [fa-fa-download] {label} → {href}")

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
                or any(kw in href_lower  for kw in ['/dl/', '/get/', '/file/', 'download'])
            )

            if is_dl and href not in seen_urls:
                seen_urls.add(href)
                download_links.append({'url': href, 'label': label})
                print(f"   🔗 [fallback] {label} → {href}")

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

    series_re = re.compile(
        r'(?i)(.*?\b(?:S\d{1,2}|Season\s?\d{1,2}))[\s\-–|:]*\s*(.*)'
    )
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


def find_existing_movie(title: str, max_retries: int = 3):
    from django.db import connection

    base_title = re.sub(r'\s*\((complete|completed)\)\s*$', '', title, flags=re.IGNORECASE).strip()
    variants   = list(dict.fromkeys([
        title, base_title,
        f"{base_title} (Complete)", f"{base_title} (Completed)",
    ]))

    for attempt in range(max_retries):
        try:
            movie = Movie.objects.filter(title__in=variants).first()
            if movie:
                print(f"   ✅ Match: '{movie.title}'")
            return movie
        except Exception as e:
            print(f"   ⚠️ DB error (attempt {attempt+1}): {e}")
            if attempt < max_retries - 1:
                connection.close()
                time.sleep(2 ** attempt)
            else:
                raise
    return None


# ══════════════════════════════════════════════════════════════
# MANAGEMENT COMMAND
# ══════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = (
        'Scrape 9jarocks.net → save to DB → '
        'publish to naijadeleys.com.ng WordPress → '
        'post to Twitter + Facebook'
    )

    def _run_sync_wordpress(self, no_social: bool):
        """
        Loop through every Movie in Django DB.
        If it doesn't exist on WordPress yet → create it.
        Skips movies that are already published.
        """
        headers  = _get_wp_auth_header()
        wp_base  = _get_wp_base_url()
        movies   = Movie.objects.prefetch_related('categories', 'download_links').all()
        total    = movies.count()
        pushed   = 0
        skipped  = 0

        print("=" * 60)
        print(f"🔄  WordPress sync — {total} movies in Django DB")
        print(f"    Target: {wp_base}")
        print("=" * 60)

        for i, movie in enumerate(movies, 1):
            print(f"\n[{i}/{total}] {movie.title}")

            existing = _wp_find_existing_post(movie.title, headers, wp_base)
            if existing:
                print(f"   ⏭️  Already on WP (ID {existing['id']}) — skipping")
                skipped += 1
                continue

            # Build a parsed-like dict from the Django movie object
            parsed = {
                'image_url':      movie.image_url or '',
                'video_url':      movie.video_url or '',
                'description':    movie.description or '',
                'meta':           {},
                'download_links': [
                    {'url': dl.url, 'label': dl.label}
                    for dl in movie.download_links.all()
                ],
            }
            # Fallback: if no related download links, use download_url field
            if not parsed['download_links'] and getattr(movie, 'download_url', ''):
                parsed['download_links'] = [{'url': movie.download_url, 'label': 'Download'}]

            categories = [c.name for c in movie.categories.all()]
            title_b    = getattr(movie, 'title_b', '') or ''

            wp_ok = _post_to_wordpress(
                movie, parsed, movie.title, title_b,
                is_new=True, categories=categories,
            )
            if wp_ok:
                pushed += 1
                if not no_social:
                    _post_movie_to_twitter(movie,  is_new=True)
                    _post_movie_to_facebook(movie, is_new=True)
            # Small delay to avoid hammering WP REST API
            time.sleep(0.5)

        print(f"\n\n{'=' * 60}")
        print(f"🎉  Sync complete!")
        print(f"    Total in DB   : {total}")
        print(f"    Pushed to WP  : {pushed}")
        print(f"    Already on WP : {skipped}")
        if not no_social:
            print(f"    {_limiter.stats()}")
        print("=" * 60)

    def add_arguments(self, parser):
        parser.add_argument('--startpage',       type=int,   default=1)
        parser.add_argument('--endpage',         type=int,   default=None)
        parser.add_argument('--max-pages',       type=int,   default=None)
        parser.add_argument('--category',        type=str,   default=None)
        parser.add_argument('--delay',           type=float, default=0.3)

        # ── Destination flags (all ON by default) ─────────────
        parser.add_argument(
            '--no-django', action='store_true', default=False,
            help='Skip saving to Django DB (scrape + push to WP only)',
        )
        parser.add_argument(
            '--no-wordpress', action='store_true', default=False,
            help='Skip publishing to naijadeleys.com.ng WordPress',
        )
        parser.add_argument(
            '--no-social', action='store_true', default=False,
            help='Skip Twitter and Facebook posts',
        )

        # ── Sync mode: push existing Django movies → WordPress ─
        parser.add_argument(
            '--sync-wordpress', action='store_true', default=False,
            help=(
                'No scraping — loop through every Django Movie and push '
                'any that are missing from WordPress. '
                'Combine with --no-social to skip social posts.'
            ),
        )

    def handle(self, *args, **options):
        from django.db import connection

        start_page     = options['startpage']
        end_page       = options['endpage']
        max_pages      = options['max_pages']
        no_django      = options['no_django']
        no_wordpress   = options['no_wordpress']
        no_social      = options['no_social']
        sync_wordpress = options['sync_wordpress']
        delay          = options['delay']
        cat_slug       = options.get('category')

        # ── SYNC MODE ──────────────────────────────────────────
        if sync_wordpress:
            self._run_sync_wordpress(no_social)
            return

        if cat_slug:
            cats_to_crawl = [f"videodownload/{cat_slug}" if '/' not in cat_slug else cat_slug]
        else:
            cats_to_crawl = CATEGORIES

        print("=" * 60)
        print("🚀  9jarocks.net scraper starting")
        print(f"    Cats      : {', '.join(cats_to_crawl)}")
        print(f"    Pages     : {start_page} → {end_page or '∞'}"
              + (f"  (max {max_pages})" if max_pages else ""))
        print(f"    Django DB : {'DISABLED (--no-django)' if no_django else '✅ ON'}")
        print(f"    WordPress : {'DISABLED (--no-wordpress)' if no_wordpress else _get_wp_base_url()}")
        print(f"    Social    : {'DISABLED (--no-social)' if no_social else 'Twitter + Facebook'}")
        print("=" * 60)

        scraper = _make_scraper()

        total_posts_scraped = 0
        total_new           = 0
        total_updated       = 0
        total_wp_published  = 0

        for cat_slug_full in cats_to_crawl:
            cat_base_url = f"{SITE_URL}/category/{cat_slug_full}"
            print(f"\n\n{'═'*60}")
            print(f"📂 Category: {cat_slug_full}")
            print(f"{'═'*60}")

            page            = start_page
            pages_crawled   = 0
            consecutive_err = 0

            while True:
                if end_page and page > end_page:
                    print(f"\n✅ Reached end page {end_page}.")
                    break
                if max_pages and pages_crawled >= max_pages:
                    print(f"\n✅ Crawled {max_pages} pages for this category.")
                    break

                listing_url = cat_base_url + '/' if page == 1 else f"{cat_base_url}/page/{page}/"
                print(f"\n{'─'*60}")
                print(f"🌐 Listing page {page}: {listing_url}")

                try:
                    resp = scraper.get(listing_url, timeout=20)
                    if resp.status_code == 404:
                        print("   ✅ No more pages (404).")
                        break
                    resp.raise_for_status()
                    html = resp.text
                except Exception as e:
                    print(f"   ❌ Failed to fetch listing page: {e}")
                    consecutive_err += 1
                    if consecutive_err >= 5:
                        print("   ❌ Too many errors — moving to next category.")
                        break
                    time.sleep(5)
                    continue

                consecutive_err = 0
                pages_crawled  += 1

                post_urls = get_post_urls_from_listing_page(html, listing_url)
                print(f"   📋 Found {len(post_urls)} posts on this page")

                if not post_urls:
                    print("   ⚠️ No posts found — stopping category.")
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

                    title, title_b = clean_title_parts(parsed['title_raw'])
                    print(f"      📝 Title: {title}")
                    if title_b:
                        print(f"      📝 Episode: {title_b}")

                    # ── Django DB write ───────────────────────────
                    movie   = None
                    created = False
                    updated = False

                    if not no_django:
                        try:
                            movie   = find_existing_movie(title)

                            if not movie:
                                movie = Movie.objects.create(
                                    title       = title,
                                    title_b     = title_b,
                                    title_b_updated_at = timezone.now() if title_b else None,
                                    description = parsed['description'],
                                    video_url   = parsed['video_url'],
                                    download_url= parsed['download_links'][0]['url'],
                                    image_url   = parsed['image_url'],
                                    completed   = parsed['is_complete'],
                                    is_series   = parsed['is_series'],
                                    scraped     = True,
                                )
                                created = True
                                total_new += 1
                                print(f"      ✅ DB created: {title}")

                            else:
                                if movie.title != title:
                                    movie.title = title
                                    updated = True

                                if title_b and movie.title_b != title_b:
                                    movie.title_b            = title_b
                                    movie.title_b_updated_at = timezone.now()
                                    updated = True
                                    print(f"      🆕 Episode updated → {title_b}")

                                if not movie.video_url and parsed['video_url']:
                                    movie.video_url = parsed['video_url']
                                    updated = True

                                if not movie.image_url and parsed['image_url']:
                                    movie.image_url = parsed['image_url']
                                    updated = True

                                if parsed['download_links']:
                                    new_dl_url = parsed['download_links'][0]['url']
                                    if movie.download_url and normalize_url(movie.download_url) != normalize_url(new_dl_url):
                                        movie.download_url = new_dl_url
                                        updated = True

                                if movie.completed != parsed['is_complete']:
                                    movie.completed = parsed['is_complete']
                                    updated = True

                                if not getattr(movie, 'is_series', False) and parsed['is_series']:
                                    movie.is_series = parsed['is_series']
                                    updated = True

                                if updated:
                                    movie.save()
                                    total_updated += 1

                            # Categories
                            for cat_name in parsed['categories']:
                                cat_obj, _ = Category.objects.get_or_create(
                                    name=cat_name.strip().capitalize()
                                )
                                movie.categories.add(cat_obj)

                            # Download link sync
                            existing = {normalize_url(dl.url): dl for dl in movie.download_links.all()}
                            current  = {normalize_url(dl['url']): dl for dl in parsed['download_links']}
                            added    = 0

                            for norm, dl in current.items():
                                if norm not in existing:
                                    DownloadLink.objects.create(movie=movie, label=dl['label'], url=dl['url'])
                                    added += 1
                                else:
                                    if existing[norm].label != dl['label']:
                                        existing[norm].label = dl['label']
                                        existing[norm].save()

                            for norm in set(existing) - set(current):
                                existing[norm].delete()

                            total_posts_scraped += 1
                            status = "created" if created else ("updated" if updated else "unchanged")
                            print(f"      📋 DB {status} | links: {len(parsed['download_links'])} (+{added} new)")

                        except Exception as db_err:
                            print(f"      💥 DB error: {db_err}")
                            import traceback; traceback.print_exc()
                            connection.close()
                            continue

                    else:
                        # --no-django: still count it so WP/social can run
                        total_posts_scraped += 1
                        print(f"      ⏭️  Django DB skipped (--no-django)")

                    # ── WordPress ─────────────────────────────────
                    # Publish if: WP enabled AND (new movie OR episode update)
                    # When --no-django, always attempt WP (scrape-to-WP only mode)
                    should_post_wp = not no_wordpress and (
                        no_django or created or (updated and title_b)
                    )
                    if should_post_wp:
                        wp_ok = _post_to_wordpress(
                            movie, parsed, title, title_b,
                            is_new=(created or no_django),
                            categories=parsed['categories'],
                            # skip search API call when scraping fresh to WP only
                            skip_existence_check=no_django,
                        )
                        if wp_ok:
                            total_wp_published += 1

                    # ── Social ────────────────────────────────────
                    if not no_social and movie and (created or (updated and title_b)):
                        _post_movie_to_twitter(movie,  is_new=created)
                        _post_movie_to_facebook(movie, is_new=created)

                if not has_next_page(html):
                    print(f"\n   ✅ No next page — end of category '{cat_slug_full}'.")
                    break

                page += 1

        print(f"\n\n{'=' * 60}")
        print(f"🎉  Scraping complete!")
        print(f"    Posts processed  : {total_posts_scraped}")
        print(f"    New entries      : {total_new}")
        print(f"    Updated entries  : {total_updated}")
        print(f"    WP published     : {total_wp_published}")
        print(f"    {_limiter.stats()}")
        print("=" * 60)


# Command Django DBWordPress Social

# scrape_9jarock✅✅✅
# scrape_9jarock --no-social✅✅❌
# scrape_9jarock --no-wordpress --no-social✅❌❌
# scrape_9jarock --no-django --no-social❌✅❌
# scrape_9jarock --sync-wordpress --no-socialreads only✅ missing ones❌