"""
Management command: scrape_thenkiri
Scrapes thenkiri.com by crawling category listing pages (WordPress / Elementor),
then visiting each post page to extract title, image, description, and download links.

WHY HTML scraping (not REST API):
  • thenkiri.com is a WordPress site but the REST API is not reliable for all posts.
  • All actual content lives under /category/<slug>/page/N/

HTML structure (confirmed from live page source):
  • Listing page   : standard WordPress <article> elements, also Elementor post-grids
  • Post URLs      : any link under thenkiri.com that looks like a post permalink
  • Download links : <a> tags containing download-related text/domains inside .entry-content
  • Featured image : og:image meta tag OR first <img> in .entry-content
  • Post metadata  : <table> or key:value lines inside the post body
  • Categories     : WordPress breadcrumb or <a rel="category tag">

Usage examples
──────────────
python manage.py scrape_thenkiri
python manage.py scrape_thenkiri --startpage 5
python manage.py scrape_thenkiri --startpage 1 --endpage 10
python manage.py scrape_thenkiri --no-social
python manage.py scrape_thenkiri --category kdrama
python manage.py scrape_thenkiri --category hollywood
python manage.py scrape_thenkiri --category series
python manage.py scrape_thenkiri --category all

Available friendly --category values:
  hollywood, kdrama, korean_movie, chinese, chinese_drama,
  bollywood, philippine, series, all
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from movies.models import Movie, Category, DownloadLink
from movies.scraper_utils import is_valid_download_url, get_or_create_category
import requests
from bs4 import BeautifulSoup
import re
import cloudscraper
from urllib.parse import urlparse, urljoin, unquote
import urllib3
import time
import os
import tempfile
import shutil
import asyncio

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ══════════════════════════════════════════════════════════════
# SITE CONSTANTS
# ══════════════════════════════════════════════════════════════

SITE_URL = 'https://thenkiri.com'

# ── Category definitions ──────────────────────────────────────
# Each entry:
#   'slug'     : thenkiri URL path under /category/
#   'db_cats'  : exact DB Category names to assign on your site
#
# Category slugs confirmed from thenkiri.com WordPress REST API:
#   /category/international/              → Hollywood / foreign movies
#   /category/download-k-drama/          → Korean dramas
#   /category/asian-movies/download-korean-movies/ → Korean movies
#   /category/asian-movies/download-bollywood-movies/ → Bollywood
#   /category/asian-movies/chinese-movie/ → Chinese movies
#   /category/chinese-dramas/            → Chinese dramas
#   /category/asian-movies/download-philippine-movies/ → Philippine movies
#   /category/k-variety/                 → K-Variety (reality/variety shows)
#   /category/tv-series/                 → TV Series (Hollywood/general)

CATEGORY_DEFINITIONS = [
    {
        'key':     'hollywood',
        'slug':    'international',
        'label':   'Hollywood / International Movies',
        'db_cats': ['Hollywood movies'],
    },
    {
        'key':     'series',
        'slug':    'tv-series',
        'label':   'TV Series',
        'db_cats': ['Hollywood tv series', 'Series'],
    },
    {
        'key':     'kdrama',
        'slug':    'download-k-drama',
        'label':   'Korean Drama',
        'db_cats': ['Korean drama'],
    },
    {
        'key':     'korean_movie',
        'slug':    'asian-movies/download-korean-movies',
        'label':   'Korean Movies',
        'db_cats': ['Korean drama'],
    },
    {
        'key':     'bollywood',
        'slug':    'asian-movies/download-bollywood-movies',
        'label':   'Bollywood Movies',
        'db_cats': ['Bollywood movies'],
    },
    {
        'key':     'chinese',
        'slug':    'asian-movies/chinese-movie',
        'label':   'Chinese Movies',
        'db_cats': ['Chinese drama'],
    },
    {
        'key':     'chinese_drama',
        'slug':    'chinese-dramas',
        'label':   'Chinese Dramas',
        'db_cats': ['Chinese drama'],
    },
    {
        'key':     'philippine',
        'slug':    'asian-movies/download-philippine-movies',
        'label':   'Philippine Movies',
        'db_cats': ['Filipino drama', 'Series'],
    },
    {
        'key':     'k_variety',
        'slug':    'k-variety',
        'label':   'K-Variety',
        'db_cats': ['Korean drama', 'Series'],
    },
]

# ── Friendly alias groups (for --category flag) ────────────────
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

# Build a lookup from slug → definition for easy access later
_SLUG_TO_DEF = {d['slug']: d for d in CATEGORY_DEFINITIONS}
_KEY_TO_DEF  = {d['key']:  d for d in CATEGORY_DEFINITIONS}


# ── Ad / monetization redirect domains to SKIP ────────────────
AD_DOMAINS = [
    'associationfoam.com',
    'obqj2.com',
    'cranialhubbed.com',
    'admiredjumper.com',
    'getdirectbonus.com',
    'push-sdk.com',
    'go.getdirectbonus.com',
]

# Download domains and keywords for fallback link detection
KNOWN_DOWNLOAD_DOMAINS = [
    # Cloud storage / file hosts
    'mega.nz',
    'drive.google.com',
    'mediafire.com',
    'pixeldrain.com',
    'terabox.com',
    'gofile.io',
    'mixdrop.co',
    'streamtape.com',
    'doodstream.com',
    'filemoon.sx',
    'loadedfiles.org',
    'netnaijafiles.xyz',
    'sabishares.com',
    'meetdownload.com',
    'webloaded.com.ng',
    'wideshares.org',
    'downloadwella.com',
    'netnaija.com',
    'fzmovies.net',
    # thenkiri-specific / Nigerian download hosts
    'o2tvseries.com',
    'sojuoppa.com',
    'dramabus.tv',
    'my9jatv.com',
    'yts.mx',
    'yts.am',
    'nkirifiles.com',
    'thenkiri.com/wp-content/uploads',  # direct uploads
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
    'telegram': 'https://t.me/+oFCiWwxKmT5jNDM8',
    'twitter':  'https://x.com/watch2download',
    'facebook': 'https://facebook.com/WATCH2D/',
    'website':  'https://watch2d.org',
    # Where "Get the App" points. Swap for your APKPure page if you prefer.
    'app':      'https://watch2d.org',
}

TWITTER_FOOTER = (
    f"\n\n📱 Telegram: {PLATFORM_LINKS['telegram']}"
    f"\n📘 Facebook: {PLATFORM_LINKS['facebook']}"
    f"\n🌍 More: {PLATFORM_LINKS['website']}"
)

TELEGRAM_FOOTER = (
    "\n\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
    "📣 <b>Stay Connected:</b>\n"
    f"📱 <a href='{PLATFORM_LINKS['telegram']}'>Join our Telegram Channel</a>\n"
    f"📘 <a href='{PLATFORM_LINKS['facebook']}'>Like us on Facebook</a>\n"
    f"🐦 <a href='{PLATFORM_LINKS['twitter']}'>Follow us on X/Twitter</a>\n"
    f"🌍 <a href='{PLATFORM_LINKS['website']}'>Visit Watch2D.org</a>\n"
    "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"
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
        self._counts    = {'facebook': 0, 'twitter': 0, 'telegram': 0}
        self._last_post = {'facebook': 0.0, 'twitter': 0.0, 'telegram': 0.0}
        self._min_gap   = {'facebook': 45, 'twitter': 60, 'telegram': 5}
        self._run_cap   = {'facebook': 80, 'twitter': 40, 'telegram': 200}

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
            f"Telegram: {self._counts['telegram']} | "
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
# TELEGRAM POSTER
# ══════════════════════════════════════════════════════════════

def _post_movie_to_telegram(movie, is_new: bool):
    if not _limiter.can_post('telegram'):
        return
    try:
        from django.conf import settings
        from automation.telegram import send_photo, send_message

        channel  = getattr(settings, 'TELEGRAM_MOVIES_CHANNEL', '')
        site_url = getattr(settings, 'SITE_URL', 'https://watch2d.org')
        if not channel:
            return

        url      = f"{site_url}/movies/movie/{movie.pk}/"
        tg_tags, _, _ = _detect_hashtags(movie)

        if is_new:
            emoji  = "🎬" if not movie.is_series else "📺"
            kind   = "SERIES" if movie.is_series else "MOVIE"

            # ── Header ───────────────────────────────────────────
            lines = [
                # f"{'━' * 22}",
                # f"{emoji}  <b>NEW {kind} AVAILABLE!</b>",
                # f"{'━' * 22}",
                # "",
                f"🎞  <b>{movie.title}</b>",
                "",
            ]

            # ── Metadata block ────────────────────────────────────
            meta_lines = []

            cats = movie.categories.all()
            if cats:
                meta_lines.append(f"🏷  <b>Genre:</b>  {', '.join(c.name for c in cats[:4])}")

            if getattr(movie, 'vi_year', ''):
                meta_lines.append(f"📅  <b>Year:</b>   {movie.vi_year}")

            if getattr(movie, 'vi_country', ''):
                meta_lines.append(f"🌍  <b>Country:</b> {movie.vi_country}")

            if getattr(movie, 'vi_language', ''):
                meta_lines.append(f"🗣  <b>Language:</b> {movie.vi_language}")

            if getattr(movie, 'vi_subtitle', ''):
                meta_lines.append(f"📝  <b>Subtitle:</b> {movie.vi_subtitle}")

            if getattr(movie, 'vi_runtime', ''):
                meta_lines.append(f"⏱  <b>Runtime:</b> {movie.vi_runtime}")

            if getattr(movie, 'vi_filesize', ''):
                meta_lines.append(f"💾  <b>File Size:</b> {movie.vi_filesize}")

            if getattr(movie, 'vi_quality', '') or getattr(movie, 'vi_episodes', ''):
                q = getattr(movie, 'vi_quality', '') or ''
                ep = getattr(movie, 'vi_episodes', '') or ''
                if ep:
                    meta_lines.append(f"🎞  <b>Episodes:</b> {ep}")
                if q:
                    meta_lines.append(f"🎥  <b>Quality:</b>  {q}")

            if movie.is_series:
                status = "✅ Completed" if movie.completed else "🔄 Ongoing"
                meta_lines.append(f"📡  <b>Status:</b>  {status}")

            if meta_lines:
                lines += meta_lines + [""]

            # ── Description ───────────────────────────────────────
            if movie.description:
                desc = movie.description[:280].rstrip()
                if len(movie.description) > 280:
                    desc += "…"
                lines += [
                    f"📖  <i>{desc}</i>",
                    "",
                ]

            # ── CTA — the most prominent part ─────────────────────
            lines += [
                f"{'▬' * 22}",
                "",
                "⬇️  <b>Tap the Download Link button below</b> 👇",
                "⚠️  <i>Open it in Chrome — not Telegram's built-in browser.</i>",
                "",
                f"{'▬' * 22}",
                "",
            ]

            # ── Hashtags ──────────────────────────────────────────
            lines += [TELEGRAM_FOOTER]
            # lines += [tg_tags, TELEGRAM_FOOTER]

            from automation.models import TelegramPost
            _, created = TelegramPost.objects.get_or_create(
                content_type='movie',
                content_id=movie.id,
                defaults={'content_title': movie.title, 'success': True},
            )

        else:
            # ── Episode update post ───────────────────────────────
            episode_label = movie.title_b or "New Episode"
            lines = [
                # f"{'━' * 22}",
                # f"🆕  <b>NEW EPISODE DROPPED!</b>",
                # f"{'━' * 22}",
                # "",
                f"📺  <b>{movie.title}</b>",
                f"🎬  <b>Episode:</b>  {episode_label}",
                "",
                f"{'▬' * 22}",
                "",
                "⬇️  <b>Tap the Download Link button below</b> 👇",
                "⚠️  <i>Open it in Chrome — not Telegram's built-in browser.</i>",
                "",
                # f"{'▬' * 22}",
                # "",
                # tg_tags,
                TELEGRAM_FOOTER,
            ]

            from automation.models import TelegramUpdate
            _, created = TelegramUpdate.objects.get_or_create(
                content_type='movie',
                content_id=movie.id,
                update_key=episode_label.strip(),
                defaults={'content_title': movie.title, 'success': True},
            )

        # Already posted this movie / this exact episode → don't repost.
        if not created:
            return

        caption = "\n".join(lines)
        markup = {'inline_keyboard': [
            [{'text': '⬇️ Download Link', 'url': url}],
            [{'text': '📲 Get the Watch2D App', 'url': PLATFORM_LINKS['app']}],
        ]}
        if movie.image_url:
            send_photo(channel, movie.image_url, caption, reply_markup=markup)
        else:
            send_message(channel, caption, reply_markup=markup)

        _limiter.record('telegram')
        print(f"📢 Telegram: {'NEW' if is_new else 'UPDATE'} posted — {movie.title}")

    except Exception as e:
        print(f"⚠️ Telegram post failed (non-critical): {e}")


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
            kind  = "SERIES" if movie.is_series else "MOVIE"

            lines = [
                f"{'━' * 22}",
                f"{emoji}  NEW {kind} — {movie.title}",
                f"{'━' * 22}",
                "",
            ]

            # ── Metadata ──────────────────────────────────────────
            cats = movie.categories.all()
            if cats:
                lines.append(f"🏷  Genre:    {', '.join(c.name for c in cats[:4])}")

            if getattr(movie, 'vi_year', ''):
                lines.append(f"📅  Year:     {movie.vi_year}")

            if getattr(movie, 'vi_country', ''):
                lines.append(f"🌍  Country:  {movie.vi_country}")

            if getattr(movie, 'vi_language', ''):
                lines.append(f"🗣  Language: {movie.vi_language}")

            if getattr(movie, 'vi_subtitle', ''):
                lines.append(f"📝  Subtitle: {movie.vi_subtitle}")

            if getattr(movie, 'vi_runtime', ''):
                lines.append(f"⏱  Runtime:  {movie.vi_runtime}")

            if getattr(movie, 'vi_filesize', ''):
                lines.append(f"💾  Size:     {movie.vi_filesize}")

            if getattr(movie, 'vi_episodes', ''):
                lines.append(f"🎞  Episodes: {movie.vi_episodes}")

            if movie.is_series:
                status = "✅ Completed" if movie.completed else "🔄 Ongoing"
                lines.append(f"📡  Status:   {status}")

            lines.append("")

            # ── Description ───────────────────────────────────────
            if movie.description:
                desc = movie.description[:350].rstrip()
                if len(movie.description) > 350:
                    desc += "…"
                lines += [f"📖  {desc}", ""]

            # ── CTA ───────────────────────────────────────────────
            lines += [
                f"{'▬' * 22}",
                f"⬇️  DOWNLOAD / WATCH FOR FREE",
                f"👉  {url}",
                f"{'▬' * 22}",
                "",
                fb_tags,
                FACEBOOK_FOOTER,
            ]

        else:
            episode_label = movie.title_b or "New Episode"
            lines = [
                f"{'━' * 22}",
                f"🆕  NEW EPISODE — {movie.title}",
                f"{'━' * 22}",
                "",
                f"📺  Show:    {movie.title}",
                f"🎬  Episode: {episode_label}",
                "",
                f"{'▬' * 22}",
                f"⬇️  DOWNLOAD / WATCH FOR FREE",
                f"👉  {url}",
                f"{'▬' * 22}",
                "",
                fb_tags,
                FACEBOOK_FOOTER,
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

def _post_to_all_platforms(movie, is_new: bool):
    _post_movie_to_telegram(movie, is_new=is_new)
    _post_movie_to_facebook(movie, is_new=is_new)
    # _post_movie_to_twitter(movie,  is_new=is_new)
    # pass


# ══════════════════════════════════════════════════════════════
# PRIVATE TELEGRAM UPLOADER  (Telethon — supports up to 2 GB)
#
# How it works:
#   1. resolve_direct_link()  — follows the downloadwella.com landing
#      page (same logic as movie_detail.html's resolveUrl) to find
#      the real ?pt= download URL before it expires.
#   2. download_to_temp()     — streams the file to a temp dir on the
#      server disk.  Skips if file > MAX_UPLOAD_BYTES.
#   3. upload_to_private_channel() — sends the file to your private
#      Telegram channel via Telethon with a rich caption.
#   4. Temp file is always deleted in a finally block.
#
# Setup (one-time, on the server):
#   pip install telethon
#   Then run:  python manage.py scrape_thenkiri --telethon-login
#   This saves a .session file so future runs are fully automatic.
#
# Required Django settings (add to settings.py or .env):
#   TELETHON_API_ID        = 12345678          # from my.telegram.org
#   TELETHON_API_HASH      = "abc123..."       # from my.telegram.org
#   TELETHON_SESSION_NAME  = "uploader"        # any name you like
#   TELETHON_PRIVATE_CHANNEL = -1001234567890  # your private channel ID
#
# ══════════════════════════════════════════════════════════════

# Max file size to download+upload (2 GB — Telegram's hard limit)
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024   # 2 GB
# Min free disk space required before attempting a download
MIN_FREE_BYTES   = 5 * 1024 * 1024 * 1024   # 5 GB

# Temp directory for downloaded files (auto-cleaned after upload)
TEMP_DIR = os.path.join(tempfile.gettempdir(), "tg_uploads")


def _ensure_temp_dir():
    os.makedirs(TEMP_DIR, exist_ok=True)


def _cleanup_stale_temp_files():
    """
    Delete any leftover files from a previous crashed run.
    Called once at scraper startup when --upload-files is active.
    """
    if not os.path.isdir(TEMP_DIR):
        return
    count = 0
    for fname in os.listdir(TEMP_DIR):
        fpath = os.path.join(TEMP_DIR, fname)
        try:
            os.remove(fpath)
            count += 1
        except Exception:
            pass
    if count:
        print(f"🧹  Cleaned up {count} stale temp file(s) from {TEMP_DIR}")


def _free_disk_bytes() -> int:
    """Return free disk space in bytes for the TEMP_DIR partition."""
    stat = shutil.disk_usage(TEMP_DIR if os.path.isdir(TEMP_DIR) else tempfile.gettempdir())
    return stat.free


# ── Step 1: Resolve the landing page to a direct download URL ──
#
# downloadwella.com (and similar hosts used by thenkiri) serve a
# landing page.  The real ?pt= download link is embedded in the
# page HTML.  We replicate the exact same extraction logic that
# movie_detail.html uses on the client side, but here in Python
# so the server can start the download immediately.

def resolve_direct_link(landing_url: str, session: requests.Session) -> str | None:
    """
    Follow a downloadwella.com (or similar) landing page and return
    the actual direct download URL.

    Strategy:
      1. Return immediately if already a direct/resolved link
      2. Fetch landing page with cloudscraper (bypasses bot detection)
         and parse the HTML for the ?pt= token link using multiple patterns
      3. Follow redirects — downloadwella sometimes redirects directly
         to the file after a short delay

    NOTE: We intentionally skip calling the Django website resolver
    (watch2d.org/movies/resolve-download/) because the Render free tier
    may be sleeping and cause a 15-second timeout every single movie.
    Cloudscraper on the raw landing page is faster and more reliable
    when running on a GitHub Actions / VPS server.

    Returns the resolved URL string, or None if resolution fails.
    """
    url_lower = landing_url.lower()
    direct_exts = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.zip', '.rar')

    # ── Already resolved ───────────────────────────────────────
    if '?pt=' in url_lower:
        return landing_url

    # sabishares.com/file/?preview → strip query = direct link
    try:
        u = urlparse(landing_url)
        if u.hostname and 'sabishares.com' in u.hostname and '/file/' in u.path and 'preview' in u.query:
            return f"{u.scheme}://{u.netloc}{u.path}"
    except Exception:
        pass

    # Plain direct file extension (not a landing page host)
    is_ext = any(url_lower.endswith(e) or (e + '?') in url_lower for e in direct_exts)
    if is_ext and 'downloadwella.com' not in url_lower and 'sabishares.com/file/' not in url_lower:
        return landing_url

    print(f"      🔗 Resolving landing page: {landing_url[:80]}…")

    # ── Fetch with cloudscraper (handles Cloudflare / bot checks) ──
    for attempt in range(2):
        try:
            scraper = cloudscraper.create_scraper(
                browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
            )
            resp = scraper.get(landing_url, timeout=25, allow_redirects=True)
            html  = resp.text
            final = resp.url

            # ── Check if the redirect chain led to a direct file ──
            if '?pt=' in final or any(final.lower().endswith(e) for e in direct_exts):
                print(f"      ✅ Resolved via redirect chain")
                return final

            # ── Pattern 1: href with ?pt= token ───────────────────
            # e.g. <a href="https://cdn.downloadwella.com/dl/file.mkv?pt=TOKEN">
            m = re.search(
                r'href=[\'"]?(https?://[^\'">\s]+\?pt=[^\'">\s]+)[\'"]?',
                html
            )
            if m:
                print(f"      ✅ Resolved via href ?pt= pattern")
                return m[1]

            # ── Pattern 2: jQuery .html() injection ───────────────
            # e.g. $(...).html('...<a href="URL?pt=TOKEN">...')
            m = re.search(
                r'\.html\s*\(\s*["\'].*?href=[\'"](https?://[^\'">\s]+\?pt=[^\'">\s]+)[\'"]',
                html, re.DOTALL
            )
            if m:
                print(f"      ✅ Resolved via jQuery html() pattern")
                return m[1]

            # ── Pattern 3: JS string assignment with ?pt= ─────────
            # e.g. var url = "https://...?pt=TOKEN";
            m = re.search(
                r'[=:(,\s][\'\"](https?://[^\'">\s]+\?pt=[^\'">\s]+)[\'"]',
                html
            )
            if m:
                print(f"      ✅ Resolved via JS variable pattern")
                return m[1]

            # ── Pattern 4: location.href = download path ──────────
            m = re.search(
                r'location\.href\s*=\s*[\'"]((https?://)[^\'"]{20,})[\'"]',
                html
            )
            if m and re.search(r'/dl/|\.mkv|\.mp4|\.avi|\.zip', m[1], re.IGNORECASE):
                print(f"      ✅ Resolved via location.href pattern")
                return m[1]

            # ── Pattern 5: data-url or data-link attributes ───────
            m = re.search(
                r'data-(?:url|link|href|src)=[\'"]?(https?://[^\'">\s]+\?pt=[^\'">\s]+)[\'"]?',
                html
            )
            if m:
                print(f"      ✅ Resolved via data-attribute pattern")
                return m[1]

            # ── Pattern 6: raw direct file URL anywhere in page ───
            m = re.search(
                r'[\'\"](https?://[^\'"?\s]{10,}\.(?:mp4|mkv|webm|avi|zip|rar))[\'"]',
                html, re.IGNORECASE
            )
            if m:
                print(f"      ✅ Resolved via raw file URL pattern")
                return m[1]

            # ── Pattern 7: base64-encoded URL (some hosts encode it) ─
            b64_matches = re.findall(r'[\'\""]([A-Za-z0-9+/]{40,}={0,2})[\'\""]', html)
            import base64
            for b64 in b64_matches:
                try:
                    decoded = base64.b64decode(b64 + '==').decode('utf-8', errors='ignore')
                    if decoded.startswith('http') and ('?pt=' in decoded or
                            any(decoded.lower().endswith(e) for e in direct_exts)):
                        print(f"      ✅ Resolved via base64 decode")
                        return decoded
                except Exception:
                    continue

            # If first attempt got nothing useful, wait briefly and retry
            # (some pages load the link after a JS timer)
            if attempt == 0:
                print(f"      ⏳ No link found yet — retrying after 3s…")
                time.sleep(3)
                continue
            break

        except Exception as e:
            print(f"      ⚠️  Cloudscraper fetch failed (attempt {attempt+1}): {e}")
            if attempt == 0:
                time.sleep(2)
                continue
            break

    print(f"      ❌ Could not resolve direct link from: {landing_url[:80]}")
    return None


# ── Step 2: Check file size via HEAD request ────────────────────

def _get_file_size(direct_url: str, session: requests.Session) -> int | None:
    """
    Return Content-Length in bytes, or None if unknown.
    Uses a HEAD request so no data is downloaded.
    """
    try:
        resp = session.head(direct_url, timeout=10, allow_redirects=True)
        cl = resp.headers.get('Content-Length') or resp.headers.get('content-length')
        if cl:
            return int(cl)
    except Exception as e:
        print(f"      ⚠️  HEAD request failed: {e}")
    return None


def _human_bytes(n: int) -> str:
    if n >= 1_073_741_824: return f"{n/1_073_741_824:.1f} GB"
    if n >= 1_048_576:     return f"{n/1_048_576:.1f} MB"
    if n >= 1024:          return f"{n/1024:.1f} KB"
    return f"{n} B"


# ── Step 3: Stream download to temp file ───────────────────────

def download_to_temp(direct_url: str, filename: str, session: requests.Session) -> str | None:
    """
    Stream-download direct_url to TEMP_DIR/filename.
    Shows a simple progress indicator.
    Returns the local file path on success, or None on failure.
    """
    _ensure_temp_dir()
    dest = os.path.join(TEMP_DIR, filename)

    # Safety: check free disk space
    free = _free_disk_bytes()
    if free < MIN_FREE_BYTES:
        print(f"      ⚠️  Only {_human_bytes(free)} free disk space — skipping download")
        return None

    print(f"      ⬇️  Downloading → {filename}")
    try:
        with session.get(direct_url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            total     = int(resp.headers.get('Content-Length', 0))
            received  = 0
            last_pct  = -1

            with open(dest, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):  # 1 MB chunks
                    if chunk:
                        f.write(chunk)
                        received += len(chunk)
                        if total:
                            pct = int(received / total * 100)
                            if pct != last_pct and pct % 10 == 0:
                                print(f"      📥  {pct}%  ({_human_bytes(received)} / {_human_bytes(total)})")
                                last_pct = pct

        actual_size = os.path.getsize(dest)
        print(f"      ✅ Download complete: {_human_bytes(actual_size)}")
        return dest

    except Exception as e:
        print(f"      ❌ Download failed: {e}")
        # Clean up partial file
        if os.path.exists(dest):
            os.remove(dest)
        return None


# ── Step 4: Build caption for private channel ──────────────────

def _build_upload_caption(movie) -> str:
    """
    Build the caption that appears with the uploaded file
    in the private Telegram channel.
    """
    from django.conf import settings as _s
    site_url  = getattr(_s, 'SITE_URL', 'https://watch2d.org').rstrip('/')
    movie_url = f"{site_url}/movies/movie/{movie.pk}/"

    lines = [
        f"🎬  <b>{movie.title}</b>",
        "",
    ]

    if getattr(movie, 'vi_year', ''):
        lines.append(f"📅  <b>Year:</b> {movie.vi_year}")
    if getattr(movie, 'vi_language', ''):
        lines.append(f"🗣  <b>Language:</b> {movie.vi_language}")
    if getattr(movie, 'vi_runtime', ''):
        lines.append(f"⏱  <b>Runtime:</b> {movie.vi_runtime}")
    if getattr(movie, 'vi_filesize', ''):
        lines.append(f"💾  <b>Size:</b> {movie.vi_filesize}")

    try:
        cats = movie.categories.all()
        if cats:
            lines.append(f"🏷  <b>Genre:</b> {', '.join(c.name for c in cats[:3])}")
    except Exception:
        pass

    lines += [
        "",
        f"🌍  <a href='{movie_url}'>Watch2D.org</a>",
    ]

    return "\n".join(lines)


# ── Step 5: Upload via Telethon ────────────────────────────────

def upload_file_to_private_channel(movie, file_path: str) -> bool:
    """
    Upload file_path to the private Telegram channel using Telethon.
    Returns True on success, False on failure.

    Telethon is used (not aiogram) because:
      • Bot API hard-limits uploads to 50 MB
      • Telethon (userbot) supports up to 2 GB via MTProto
    """
    try:
        from django.conf import settings as _s
        from telethon.sync import TelegramClient
        from telethon.tl.types import DocumentAttributeFilename

        api_id       = getattr(_s, 'TELETHON_API_ID', None)
        api_hash     = getattr(_s, 'TELETHON_API_HASH', None)
        session_name = getattr(_s, 'TELETHON_SESSION_NAME', 'uploader')
        channel      = getattr(_s, 'TELETHON_PRIVATE_CHANNEL', None)

        if not all([api_id, api_hash, channel]):
            print("      ⚠️  Telethon credentials not configured in settings — skipping upload")
            return False

        caption  = _build_upload_caption(movie)
        filename = os.path.basename(file_path)
        filesize = os.path.getsize(file_path)

        print(f"      📤  Uploading to private channel: {filename} ({_human_bytes(filesize)})")

        # Progress callback shown every 10%
        _last_pct = [-1]
        def _progress(current, total):
            pct = int(current / total * 100)
            if pct != _last_pct[0] and pct % 10 == 0:
                print(f"      📤  {pct}%  ({_human_bytes(current)} / {_human_bytes(total)})")
                _last_pct[0] = pct

        with TelegramClient(session_name, api_id, api_hash) as client:
            client.send_file(
                channel,
                file_path,
                caption          = caption,
                parse_mode       = 'html',
                progress_callback= _progress,
                attributes       = [DocumentAttributeFilename(file_name=filename)],
                # Force sending as a document (not compressed video)
                # so the receiver gets the original file quality
                force_document   = True,
            )

        print(f"      ✅  Uploaded to private channel!")
        return True

    except ImportError:
        print("      ❌  Telethon not installed.  Run:  pip install telethon")
        return False
    except Exception as e:
        print(f"      ❌  Telethon upload failed: {e}")
        return False


# ── Master upload function — called from the scraper loop ──────

def upload_movie_file(movie, landing_url: str, http_session: requests.Session) -> bool:
    """
    Full pipeline:
      resolve link → check size → download → upload → delete temp file

    Returns True if the file was successfully uploaded to the private channel.
    Called only for newly created movies (created=True in the scraper loop).
    """
    temp_file = None
    try:
        # Step 1: Resolve landing page → direct download URL
        direct_url = resolve_direct_link(landing_url, http_session)
        if not direct_url:
            print(f"      ⛔ Upload skipped — could not resolve direct link")
            return False

        # Step 2: Check file size (HEAD request — no data used)
        size = _get_file_size(direct_url, http_session)
        if size is not None:
            print(f"      📏  File size: {_human_bytes(size)}")
            if size > MAX_UPLOAD_BYTES:
                print(f"      ⛔ Upload skipped — file too large "
                      f"({_human_bytes(size)} > {_human_bytes(MAX_UPLOAD_BYTES)})")
                return False
        else:
            print(f"      📏  File size unknown — will attempt download")

        # Step 3: Build a safe filename from the URL
        raw_name = direct_url.split('/')[-1].split('?')[0]
        # Strip any THENKIRI.COM / naijadeleyss watermarks from filename
        safe_name = re.sub(r'\.(THENKIRI\.COM|DOWNLOADED\.FROM\.[^.]+)\b', '',
                           raw_name, flags=re.IGNORECASE)
        safe_name = safe_name.strip('.') or f"movie_{movie.pk}.mkv"

        # Step 4: Download to server temp folder
        temp_file = download_to_temp(direct_url, safe_name, http_session)
        if not temp_file:
            return False

        # Step 5: Upload to private Telegram channel
        return upload_file_to_private_channel(movie, temp_file)

    finally:
        # Always delete the temp file — even if upload fails
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
                print(f"      🗑️  Temp file deleted")
            except Exception as e:
                print(f"      ⚠️  Could not delete temp file: {e}")


# ══════════════════════════════════════════════════════════════
# HTML PARSERS  (based on confirmed live page structure)
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
    """
    Extract all post URLs from a thenkiri.com category listing page.

    thenkiri uses a standard WordPress theme with <article> tags.
    Each article has a permalink that looks like:
      https://thenkiri.com/some-movie-title-download/
    We collect all links that:
      - belong to SITE_URL
      - are NOT category/tag/page/admin/feed links
      - look like a single post (no /page/, /category/, /tag/, etc.)
    """
    soup  = BeautifulSoup(html, 'html.parser')
    links = set()

    # Strategy 1: grab links from <article> elements (standard WP loop)
    for article in soup.find_all('article'):
        for a in article.find_all('a', href=True):
            href = a['href'].strip().rstrip('/')
            if _is_post_url(href):
                links.add(href)

    # Strategy 2: any link on the page that looks like a post permalink
    # (catches Elementor post-grids and other widget layouts)
    if not links:
        for a in soup.find_all('a', href=True):
            href = a['href'].strip().rstrip('/')
            if _is_post_url(href):
                links.add(href)

    return list(links)


def _is_post_url(href: str) -> bool:
    """
    Return True if href looks like a single post on thenkiri.com.
    Excludes navigation, category, tag, feed, admin, and static asset URLs.
    """
    if not href.startswith(SITE_URL):
        return False
    path = href[len(SITE_URL):]  # e.g. "/some-movie-title-download/"
    # must have at least one path segment
    if not path or path == '/':
        return False
    # skip known non-post paths
    skip = (
        '/category/', '/tag/', '/page/', '/wp-', '/feed', '/author/',
        '/search/', '/how-to-download', '?', '#', '/movies-menu/',
        '/korean-drama-menu/', '/tv-series-menu/', '/comments/',
        '/sitemap', '.xml', '.php',
    )
    if any(s in path for s in skip):
        return False
    # must look like a slug (contains at least one hyphen, no double slashes)
    if '//' in path:
        return False
    # path should have exactly one segment (no sub-paths like /category/slug/post)
    segments = [s for s in path.strip('/').split('/') if s]
    if len(segments) != 1:
        return False
    return True


def has_next_page(html: str) -> bool:
    """
    Check whether a 'Next' pagination link exists.
    thenkiri uses standard WordPress pagination:
      <a class='next page-numbers' href='...'>Next</a>
    or
      <a class='nextpostslink' href='...'>»</a>
    """
    soup = BeautifulSoup(html, 'html.parser')
    for a in soup.find_all('a', href=True):
        text = a.get_text(strip=True).lower()
        cls  = ' '.join(a.get('class', []))
        if (
            text in ('next', '»', 'next page', '›') or
            'next' in cls or
            'nextpostslink' in cls or
            'page-numbers next' in cls or
            'next page-numbers' in cls
        ):
            return True
    return False


def parse_post_page(html: str, url: str) -> dict | None:
    """
    Parse a single thenkiri.com post page.

    thenkiri post structure (confirmed from live page source):
      • Title       : <h1 class="entry-title"> or first <h1> in main content
      • Categories  : <a rel="category tag"> links, or breadcrumb
      • Image       : og:image meta tag (most reliable), or first <img> in .entry-content
      • Video       : <iframe> inside .entry-content
      • Description : og:description or first long <p> in content
      • Metadata    : key:value lines or a <table> in .entry-content
      • Downloads   : <a> tags whose href/text matches download patterns

    Returns a dict or None if the page looks like a non-movie page.
    """
    soup = BeautifulSoup(html, 'html.parser')

    # ── Guard: skip empty / error pages ──────────────────────────
    body_text = soup.get_text(' ', strip=True)
    if len(body_text) < 200:
        print(f"      [parse] page too short ({len(body_text)} chars) — likely blocked/empty")
        return None

    # ── Title ────────────────────────────────────────────────────
    # Priority: og:title  >  h1.single-post-title  >  any h1  >  <title> tag
    title_raw = ''

    og_title = soup.find('meta', property='og:title')
    if og_title:
        title_raw = og_title.get('content', '').strip()
        # Strip " | Download …" or " - TheNkiri" suffixes
        import re as _re
        title_raw = _re.sub(
            r'\s*[|\-–]\s*(Download\s+\w.*|TheNkiri.*|Nkiri.*)$',
            '', title_raw, flags=_re.IGNORECASE
        ).strip()
        title_raw = _re.sub(r'^DOWNLOAD\s+', '', title_raw, flags=_re.IGNORECASE).strip()

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
            import re as _re
            title_raw = _re.sub(
                r'\s*[|\-–]\s*(TheNkiri|Nkiri|NKIRI DOWNLOAD).*$',
                '', title_tag.get_text(strip=True), flags=_re.IGNORECASE
            ).strip()
            title_raw = _re.sub(r'^DOWNLOAD\s+', '', title_raw, flags=_re.IGNORECASE).strip()

    if not title_raw or len(title_raw) < 4:
        print(f"      [parse] no title found")
        return None

    # ── Categories ───────────────────────────────────────────────
    categories = []
    for a in soup.find_all('a', rel=True):
        rels = a.get('rel', [])
        if isinstance(rels, str):
            rels = rels.split()
        if 'category' in rels or 'tag' in rels:
            name = a.get_text(strip=True)
            if name and name.lower() not in ('uncategorized',):
                categories.append(name)
    if not categories:
        for sel in ['.breadcrumb', '.breadcrumbs', '#breadcrumb', '.entry-meta',
                    '.site-breadcrumbs', '.ocean-breadcrumbs']:
            bc = soup.select_one(sel)
            if bc:
                for a in bc.find_all('a', href=True):
                    name = a.get_text(strip=True)
                    skip_bc = {'home', '', 'movies', 'drama', 'series'}
                    if name.lower() not in skip_bc:
                        categories.append(name)
                break

    # ── Content div ──────────────────────────────────────────────
    # OceanWP (thenkiri's theme) uses <div class="entry clr"> NOT "entry-content".
    # We try multiple selectors, widest last, never return None.
    content_div = (
        soup.find('div', class_='entry-content') or
        soup.find('div', class_='post-content') or
        soup.find('div', class_='the-content') or
        soup.find('div', class_='entry') or          # OceanWP uses this
        soup.find('article') or
        soup.find('div', id='content') or
        soup.find('main', id='main') or
        soup.find('body') or
        soup
    )
    # Log which selector matched for debugging
    if content_div is not None:
        cls = getattr(content_div, 'attrs', {}).get('class', [])
        print(f"      [parse] content_div: <{content_div.name} class=\'{' '.join(cls) if cls else '(none)'}\'>")

    # ── Image ────────────────────────────────────────────────────
    image_url = ''
    # 1. og:image is most reliable on thenkiri
    og_img = soup.find('meta', property='og:image')
    if og_img:
        image_url = og_img.get('content', '').strip()
    # 2. First non-tiny <img> in content
    if not image_url:
        for img in content_div.find_all('img'):
            src = img.get('src') or img.get('data-src') or img.get('data-lazy-src') or ''
            src = src.strip()
            if src and not src.endswith('.gif'):
                w = img.get('width', '0')
                try:
                    if int(str(w).replace('px','')) < 80:
                        continue
                except ValueError:
                    pass
                image_url = src
                break

    # ── Video / Trailer ──────────────────────────────────────────
    # thenkiri uses Elementor's video widget which stores the YouTube URL
    # in data-settings JSON on the widget element — NOT in <iframe src>.
    # We must parse data-settings first, then fall back to actual iframes.
    video_url = ''
    import json as _json

    # Strategy 1: Elementor video widget data-settings (primary method)
    for _widget in soup.find_all(attrs={'data-widget_type': 'video.default'}):
        _settings_raw = _widget.get('data-settings', '')
        if _settings_raw:
            try:
                _settings = _json.loads(_settings_raw)
                _yt = _settings.get('youtube_url', '')
                if _yt:
                    video_url = _yt
                    break
            except Exception:
                pass

    # Strategy 2: <iframe src> with YouTube URL (some older posts use this)
    if not video_url:
        _TRAILER_HOSTS = ['youtube.com/embed', 'youtu.be', 'youtube-nocookie.com']
        for _iframe in soup.find_all('iframe', src=True):
            _src = _iframe['src'].strip()
            if any(d in _src for d in _TRAILER_HOSTS):
                video_url = _src
                break

    # Strategy 3: Any iframe in content area
    if not video_url:
        _iframe = content_div.find('iframe', src=True)
        if _iframe:
            video_url = _iframe['src'].strip()

    # ── Description ──────────────────────────────────────────────
    description = ''
    og_desc = soup.find('meta', property='og:description')

    if og_desc:
        description = og_desc.get('content', '').strip()

        # Replace source branding
        description = re.sub(r'https://thenkiri\.com', '', description, flags=re.IGNORECASE)
        description = re.sub(r'http://thenkiri\.com', '', description, flags=re.IGNORECASE)
        description = re.sub(r'www\.nkiri\.com', '', description, flags=re.IGNORECASE)
        description = re.sub(r'nkiri\.com', '', description, flags=re.IGNORECASE)
        description = re.sub(r'thenkiri\.com', '', description, flags=re.IGNORECASE)

    # Clean up description: some sites put the whole post in og:description
    if description and len(description) > 600:
        description = description[:600].rsplit(' ', 1)[0] + '...'

    if not description:
        for p in content_div.find_all('p'):
            text = p.get_text(strip=True)
            if text and len(text) > 40 and not re.search(r'https?://', text):
                description = text[:600]

                # Replace source branding
                description = re.sub(r'https://thenkiri\.com', '', description, flags=re.IGNORECASE)
                description = re.sub(r'http://thenkiri\.com', '', description, flags=re.IGNORECASE)
                description = re.sub(r'www\.nkiri\.com', '', description, flags=re.IGNORECASE)
                description = re.sub(r'nkiri\.com', '', description, flags=re.IGNORECASE)
                description = re.sub(r'thenkiri\.com', '', description, flags=re.IGNORECASE)

                break

    # ── Metadata table / key:value lines ─────────────────────────
    meta = {}
    # Try <table> rows (thenkiri often uses a table for movie info)
    table = content_div.find('table')
    if table:
        for row in table.find_all('tr'):
            cells = row.find_all(['td', 'th'])
            if len(cells) >= 2:
                key = cells[0].get_text(strip=True).lower().rstrip(':')
                val = cells[1].get_text(strip=True)
                if key and val:
                    meta[key] = val
    # Try <p> lines with "Key : Value" pattern
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
    # Also try <li> lines (some thenkiri posts use lists)
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
    # Button texts that are NEVER download links (must be skipped).
    _SKIP_BTN = {
        "can't download?", "cant download?", "cant download",
        "how to download", "how to download?",
        "report broken link", "report link", "report broken",
        "request movie", "request a movie",
        "subscribe", "follow us", "join us",
        "leave a comment", "share", "recommended",
        "notify me", "get notified",
    }
    # href fragments that indicate a helper/info page (not a download)
    _SKIP_HREF_FRAGS = [
        'how-to-download', 'how_to_download', '/faq', '/help',
        'report-broken', 'request-movie', 'cant-download',
    ]

    download_links = []
    seen_urls = set()

    def _get_section_season(section_el):
        """
        Look backwards from this Elementor section for the nearest
        'Season N' heading that precedes it (same parent level).
        Returns a string like 'Season 2' or '' if none found.
        """
        if section_el is None:
            return ''
        parent = section_el.parent
        if parent is None:
            return ''
        season_label = ''
        for sib in section_el.previous_siblings:
            if not hasattr(sib, 'find_all'):
                continue
            # Look for an h2 whose text matches Season N
            for h2 in sib.find_all(['h2', 'h3', 'h4']):
                txt = h2.get_text(strip=True)
                if re.search(r'\bSeason\s*\d+\b', txt, re.IGNORECASE):
                    season_label = txt.strip()
                    break
            if season_label:
                break
        return season_label

    def _episode_prefix(anchor):
        """
        Find an episode/part label associated with this download <a> tag.

        thenkiri uses an Elementor 3-column section layout:
          [Column 1: Episode N heading] [Column 2: Download button] [Column 3: empty]

        So the episode label is NOT a sibling of the <a> — it's in a sibling
        COLUMN inside the same Elementor section row. We walk up to find the
        section, then look in the first column for a heading.

        Also prepend the Season label if one precedes this section.
        """
        # ── Strategy 1: Elementor 3-column layout ──────────────
        # Walk up from anchor to find the elementor-section containing it
        el = anchor
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
            # Find all columns in this section
            columns = section.find_all(
                'div',
                class_=lambda c: c and 'elementor-column' in c
            )
            if columns:
                # The episode label is typically in the first column
                first_col = columns[0]
                for heading in first_col.find_all(['h2', 'h3', 'h4', 'h5', 'h6']):
                    txt = heading.get_text(strip=True)
                    if txt and re.search(
                        r'episode|ep\.?\s*\d|part\s*\d|s\d{1,2}e\d',
                        txt, re.IGNORECASE
                    ):
                        ep_label = txt
                        # Also find the Season label from a preceding section
                        season_label = _get_section_season(section)
                        if season_label and season_label.lower() not in ep_label.lower():
                            return f"{ep_label} ({season_label})"
                        return ep_label

        # ── Strategy 2: traditional sibling text (older post layouts) ──
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
            if hasattr(sibling, 'get_text'):
                txt = sibling.get_text(' ', strip=True)
            else:
                txt = str(sibling).strip()
            if txt:
                parts.append(txt)
        prefix = ' '.join(parts).strip()
        if prefix and re.search(
            r'episode|ep\.?\s*\d|part\s*\d|zip|s\d{1,2}e\d|batch',
            prefix, re.IGNORECASE
        ):
            return prefix
        return ''

    # thenkiri wraps download links in <a> tags with button-like text
    # or they point to known download domains / contain download keywords.
    for a in content_div.find_all('a', href=True):
        href       = a.get('href', '').strip()
        btn_text   = a.get_text(strip=True) or 'Download'
        href_lower = href.lower()
        btn_lower  = btn_text.lower().strip().rstrip('?')

        if not href or href.startswith('#') or 'javascript' in href_lower:
            continue

        # ── Skip helper/info buttons (not real download links) ───
        if btn_lower in _SKIP_BTN:
            continue
        if any(frag in href_lower for frag in _SKIP_HREF_FRAGS):
            continue

        if any(ad in href_lower for ad in AD_DOMAINS):
            print(f"   🚫 [ad skipped] {btn_text} → {href[:80]}")
            continue
        # skip site-internal navigation
        if any(skip in href_lower for skip in [
            'facebook.com', 'twitter.com', 't.me/official', 'youtube.com/watch?',
            'imdb.com', 'wp-admin', '#respond', 'mailto:',
            'thenkiri.com/category/', 'thenkiri.com/tag/',
            'thenkiri.com/how-to', 'thenkiri.com/page/',
            'dramakey.com', 'nkiri.ink', 'tiktok.com', 'x.com/official',
        ]):
            continue
        # skip same-site post links (links back to thenkiri posts)
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

    is_series = bool(
        re.search(
            r'\bS\d{1,2}\b|\bSeason\s?\d{1,2}\b|\bEpisode\b|\bEp\.?\s?\d+\b|Series\b',
            title_raw, re.IGNORECASE
        )
    )
    is_complete = bool(re.search(r'\bcomplete(d)?\b', title_raw, re.IGNORECASE))

    # ── Extract vi_ fields from the blockquote meta dict ────────────────────
    # thenkiri post metadata lines look like:
    #   Movie Name    : Ijo (2022)
    #   Director      : Tope Adebayo
    #   Stars         : Toyin Abraham, ...
    #   Genre         : Crime, Action
    #   Country       : Nigeria
    #   Language      : Yoruba
    #   Subtitle      : English
    #   Running Time  : 01:42:00
    #   File size     : 540 MB
    #   Year          : 2022
    #   Episodes      : 12
    #   Status        : Completed / On Going
    #
    # Keys vary slightly per post so we normalise them below.

    def _mv(keys):
        """Return first non-empty value from meta matching any of the keys."""
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
        'vi_episodes': _mv(['episodes', 'episode', 'total episodes', 'no of episodes']),
        'vi_status':   _mv(['status', 'series status']),
        'vi_runtime':  _mv(['running time', 'runtime', 'duration', 'run time']),
        'vi_filesize': _mv(['file size', 'filesize', 'size', 'file']),
    }

    # If year not in blockquote, try to pull from title e.g. "Ijo (2022)"
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
        'meta':           meta,
        **vi,
    }


# ══════════════════════════════════════════════════════════════
# TITLE CLEANING
# ══════════════════════════════════════════════════════════════

# Canonical season format: "Season N" (title-case, no zero-padding)
# Covers all these raw inputs:
#   S04  S4  Season 4  Season 04  SEASON 4  season4  Season4
_SEASON_RE = re.compile(
    r'\b(?:S(?:eason\s*)?|Season\s*)0*(\d{1,2})\b',
    re.IGNORECASE,
)

def _canonicalize_season(text: str) -> str:
    """
    Replace every season token in *text* with the canonical form "Season N".
    Examples:
        "S04"       → "Season 4"
        "Season 04" → "Season 4"
        "SEASON 4"  → "Season 4"
        "Season4"   → "Season 4"
        "S4"        → "Season 4"
    Non-season text is left untouched.
    """
    return _SEASON_RE.sub(lambda m: f"Season {int(m.group(1))}", text)


def clean_title_parts(raw: str) -> tuple[str, str]:
    """Returns (main_title, episode_label)."""
    title      = re.sub(r'\s+', ' ', raw).strip()
    title_lower = title.lower()
    is_complete = bool(re.search(r'\bcomplete(d)?\b', title_lower))

    series_re = re.compile(
        r'(?i)(.*?\b(?:S\d{1,2}|Season\s?\d{1,2}))[\s\-–|:]*\s*(.*)'
    )
    m = series_re.match(title)
    if m:
        base   = _canonicalize_season(m.group(1).strip())
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


def _season_variants(title: str) -> list[str]:
    """
    Given a title that may contain a season token in ANY format, return every
    plausible spelling of that token so we can match old DB records regardless
    of which format was used when they were first scraped.

    Example — "My Show Season 4":
        "My Show Season 4"   (canonical, already in list)
        "My Show Season 04"
        "My Show S4"
        "My Show S04"
        "My Show SEASON 4"
        "My Show season 4"
    """
    m = _SEASON_RE.search(title)
    if not m:
        return [title]

    n      = int(m.group(1))          # the season number, e.g. 4
    prefix = title[:m.start()]        # everything before the season token
    suffix = title[m.end():]          # everything after  the season token

    forms = [
        f"Season {n}", f"Season {n:02d}",
        f"S{n}",       f"S{n:02d}",
        f"SEASON {n}", f"season {n}",
    ]
    return list(dict.fromkeys(f"{prefix}{f}{suffix}" for f in forms))


def find_existing_movie(title: str, max_retries: int = 3):
    from django.db import connection

    base_title = re.sub(r'\s*\((complete|completed)\)\s*$', '', title, flags=re.IGNORECASE).strip()

    # Build every combination of (complete suffix) × (season spelling)
    title_bases = list(dict.fromkeys([
        title, base_title,
        f"{base_title} (Complete)", f"{base_title} (Completed)",
    ]))
    variants: list[str] = []
    for t in title_bases:
        for v in _season_variants(t):
            if v not in variants:
                variants.append(v)

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


def assign_db_categories(movie, scraped_cats: list[str], forced_db_cats: list[str]):
    """
    Assign categories to a movie.

    Rule: use ONLY forced_db_cats — the exact DB names derived from the
    thenkiri.com category slug we are currently crawling.

    scraped_cats (tags scraped from the post page itself) are intentionally
    IGNORED. A Nollywood post page often carries tags like 'Series',
    'Animation', 'Hollywood movies', 'Chinese drama', etc. that would
    pollute the movie's category assignment. The slug we crawled already
    tells us exactly what category it belongs to — that is the single
    source of truth.

    DB category names match exactly what is in views.py get_sidebar_categories():
      'Nollywood movies', 'Korean drama', 'Hollywood movies', 'Bollywood movies',
      'Anime', 'Chinese drama', 'Thai drama', 'Series', 'Animation'

    IMPORTANT: We use .set() to REPLACE all existing categories, not .add().
    This prevents stale categories from previous scrapes (e.g. Anime, Nollywood)
    from accumulating on unrelated movies.
    """
    if not forced_db_cats:
        return

    # Resolve the target Category objects
    target_cats = []
    for name in forced_db_cats:
        cat_obj = get_or_create_category(name.strip())
        if cat_obj:
            target_cats.append(cat_obj)

    # Replace all existing categories with exactly the target set
    movie.categories.set(target_cats)
    for cat in target_cats:
        print(f"      🏷  Assigned category: '{cat.name}'")


# ══════════════════════════════════════════════════════════════
# MANAGEMENT COMMAND
# ══════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = (
        'Scrape thenkiri.com category pages → save to DB → '
        'optionally post to Twitter + Facebook'
    )

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
                'Which category to scrape. Use a friendly alias:\n'
                '  hollywood, kdrama, korean, chinese, cdrama,\n'
                '  bollywood, philippine, k_variety, series, all  (default: all)\n'
                'Or a full thenkiri slug like "download-k-drama".'
            ),
        )
        parser.add_argument(
            '--no-social', action='store_true', default=False,
            help='Save to DB only — skip all social posts',
        )
        parser.add_argument(
            '--delay', type=float, default=0.3,
            help='Seconds to wait between individual post requests (default: 0.3)',
        )
        parser.add_argument(
            '--list-categories', action='store_true', default=False,
            help='Print all available category aliases and exit',
        )
        parser.add_argument(
            '--upload-files', action='store_true', default=False,
            help=(
                'After saving a NEW movie, resolve the download link, '
                'download the file to the server, and upload it to the '
                'private Telegram channel configured in TELETHON_PRIVATE_CHANNEL.'
            ),
        )
        parser.add_argument(
            '--telethon-login', action='store_true', default=False,
            help=(
                'Run the one-time interactive Telethon login (phone number + code). '
                'Saves a .session file so all future --upload-files runs are automatic.'
            ),
        )

    def handle(self, *args, **options):
        from django.db import connection

        # ── --telethon-login (one-time setup) ──────────────────
        if options.get('telethon_login'):
            self._run_telethon_login()
            return

        # ── --list-categories ──────────────────────────────────
        if options['list_categories']:
            self._print_category_list()
            return

        start_page   = options['startpage']
        end_page     = options['endpage']
        max_pages    = options['max_pages']
        no_social    = options['no_social']
        delay        = options['delay']
        cat_arg      = (options.get('category') or 'all').strip().lower()
        upload_files = options.get('upload_files', False)

        if upload_files:
            _cleanup_stale_temp_files()
            _ensure_temp_dir()

        # ── Resolve --category to a list of CATEGORY_DEFINITIONS ─
        cats_to_crawl = self._resolve_category_arg(cat_arg)
        if not cats_to_crawl:
            self.stderr.write(
                f"❌  Unknown category '{cat_arg}'.\n"
                f"    Run with --list-categories to see all options."
            )
            return

        print("=" * 60)
        print("🚀  thenkiri.com scraper starting")
        print(f"    Method  : Category page HTML scraping")
        print(f"    Cats    : {', '.join(d['label'] for d in cats_to_crawl)}")
        print(f"    Pages   : {start_page} → {end_page or '∞'}"
              + (f"  (max {max_pages})" if max_pages else ""))
        if no_social:
            print("    Social  : DISABLED (--no-social)")
        else:
            print("    Social  : Twitter + Facebook  (Telegram is OFF)")
        if upload_files:
            print("    Upload  : ✅ ENABLED — new movies will be uploaded to private TG channel")
        else:
            print("    Upload  : DISABLED (use --upload-files to enable)")
        print("=" * 60)

        scraper      = _make_scraper()
        http_session = requests.Session()
        http_session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        })

        total_posts_scraped = 0
        total_new           = 0
        total_updated       = 0

        for cat_def in cats_to_crawl:
            cat_slug_full  = cat_def['slug']
            forced_db_cats = cat_def['db_cats']   # ← exact DB category names for this slug
            cat_base_url   = f"{SITE_URL}/category/{cat_slug_full}"

            print(f"\n\n{'═'*60}")
            print(f"📂  Category : {cat_def['label']}")
            print(f"    Slug     : {cat_slug_full}")
            print(f"    DB cats  : {', '.join(forced_db_cats)}")
            print(f"    URL      : {cat_base_url}")
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
                        print(f"   ✅ No more pages (404). Moving to next category.")
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
                    print("   ⚠️ No posts found — may be at the end. Stopping category.")
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
                        print(f"      ⚠️ Could not parse post — skipping")
                        continue

                    if not parsed['download_links']:
                        print(f"      ⛔ No download links — skipping '{parsed['title_raw']}'")
                        continue

                    title, title_b = clean_title_parts(parsed['title_raw'])
                    print(f"      📝 Title: {title}")
                    if title_b:
                        print(f"      📝 Episode: {title_b}")

                    # ── DB write ──────────────────────────────
                    try:
                        movie   = find_existing_movie(title)
                        created = False
                        updated = False

                        if not movie:
                            movie = Movie.objects.create(
                                title       = title[:200],
                                title_b     = (title_b or '')[:200] or None,
                                title_b_updated_at = timezone.now() if title_b else None,
                                description = parsed['description'],
                                video_url   = parsed['video_url'][:500] if parsed['video_url'] else '',
                                download_url= parsed['download_links'][0]['url'][:500],
                                image_url   = parsed['image_url'][:500] if parsed['image_url'] else '',
                                completed   = parsed['is_complete'],
                                is_series   = parsed['is_series'],
                                scraped     = True,
                                # ── Video info fields ──
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
                            created = True
                            total_new += 1
                            print(f"      ✅ Created: {title}")

                            if not no_social:
                                _post_to_all_platforms(movie, is_new=True)

                            # ── Upload file to private TG channel ──────
                            if upload_files and parsed['download_links']:
                                landing_url = parsed['download_links'][0]['url']
                                print(f"      📦 Starting file upload pipeline…")
                                upload_movie_file(movie, landing_url, http_session)

                        else:
                            if movie.title != title:
                                movie.title = title
                                updated = True

                            if title_b and movie.title_b != title_b:
                                movie.title_b            = title_b
                                movie.title_b_updated_at = timezone.now()
                                updated                  = True
                                print(f"      🆕 Episode updated → {title_b}")
                                if not no_social:
                                    _post_to_all_platforms(movie, is_new=False)

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

                            # ── Backfill vi_ fields if not yet set ────────
                            vi_map = {
                                'vi_year':     parsed.get('vi_year', '')[:10],
                                'vi_country':  parsed.get('vi_country', '')[:120],
                                'vi_language': parsed.get('vi_language', '')[:120],
                                'vi_subtitle': parsed.get('vi_subtitle', '')[:60],
                                'vi_genre':    parsed.get('vi_genre', '')[:200],
                                'vi_cast':     parsed.get('vi_cast', ''),
                                'vi_episodes': parsed.get('vi_episodes', '')[:20],
                                'vi_status':   parsed.get('vi_status', '')[:60],
                                'vi_runtime':  parsed.get('vi_runtime', '')[:30],
                                'vi_filesize': parsed.get('vi_filesize', '')[:30],
                            }
                            for field, value in vi_map.items():
                                if value and not getattr(movie, field, ''):
                                    setattr(movie, field, value)
                                    updated = True

                            if updated:
                                movie.save()
                                total_updated += 1

                        # ── Assign categories (both forced + scraped) ──
                        assign_db_categories(
                            movie,
                            scraped_cats   = parsed['categories'],
                            forced_db_cats = forced_db_cats,
                        )

                        # ── Download link sync ─────────────────────────
                        existing = {normalize_url(dl.url): dl for dl in movie.download_links.all()}
                        current  = {normalize_url(dl['url']): dl for dl in parsed['download_links'] if is_valid_download_url(dl['url'])}
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
                        print(f"      📋 {status} | links: {len(parsed['download_links'])} (+{added} new)")

                    except Exception as db_err:
                        print(f"      💥 DB error: {db_err}")
                        import traceback; traceback.print_exc()
                        connection.close()
                        continue

                if not has_next_page(html):
                    print(f"\n   ✅ No next page — end of category '{cat_def['label']}'.")
                    break

                page += 1

        # ── Final summary ──────────────────────────────────────
        print(f"\n\n{'=' * 60}")
        print(f"🎉  Scraping complete!")
        print(f"    Posts processed : {total_posts_scraped}")
        print(f"    New entries     : {total_new}")
        print(f"    Updated entries : {total_updated}")
        print(f"    {_limiter.stats()}")
        print("=" * 60)

    def _run_telethon_login(self):
        """
        One-time interactive login.  Run this ONCE on the server:
            python manage.py scrape_thenkiri --telethon-login

        It will ask for your phone number and the code Telegram sends you.
        After that, a .session file is saved and all future runs are
        fully automatic — no phone needed again.
        """
        try:
            from telethon.sync import TelegramClient
            from django.conf import settings as _s

            api_id       = getattr(_s, 'TELETHON_API_ID', None)
            api_hash     = getattr(_s, 'TELETHON_API_HASH', None)
            session_name = getattr(_s, 'TELETHON_SESSION_NAME', 'uploader')

            if not all([api_id, api_hash]):
                print("\n❌  TELETHON_API_ID and TELETHON_API_HASH must be set in settings.py")
                print("    Get them from: https://my.telegram.org → API development tools")
                return

            print("\n📱  Telethon one-time login")
            print("    You will be asked for your phone number and the code Telegram sends you.")
            print(f"    Session will be saved as: {session_name}.session\n")

            with TelegramClient(session_name, api_id, api_hash) as client:
                me = client.get_me()
                print(f"\n✅  Logged in as: {me.first_name} (@{me.username})")
                print("    You can now run scrape_thenkiri with --upload-files")

        except ImportError:
            print("❌  Telethon not installed.  Run:  pip install telethon")
        except Exception as e:
            print(f"❌  Login failed: {e}")

    def _resolve_category_arg(self, cat_arg: str) -> list[dict]:
        """
        Turn the --category value into a list of CATEGORY_DEFINITIONS dicts.

        Accepts:
          • A friendly alias key (e.g. "nollywood", "kdrama", "all")
          • A full thenkiri slug (e.g. "download-k-drama")
          • A bare slug segment (e.g. "korean-drama")
        """
        # 1. Friendly alias
        if cat_arg in CATEGORY_ALIASES:
            keys = CATEGORY_ALIASES[cat_arg]
            return [_KEY_TO_DEF[k] for k in keys if k in _KEY_TO_DEF]

        # 2. Full slug match
        if cat_arg in _SLUG_TO_DEF:
            return [_SLUG_TO_DEF[cat_arg]]

        # 3. Bare slug — thenkiri slugs have no prefix, try as-is with hyphen normalisation
        normalised_slug = cat_arg.replace('_', '-')
        if normalised_slug in _SLUG_TO_DEF:
            return [_SLUG_TO_DEF[normalised_slug]]

        # 4. Partial key match (e.g. "nollywood_series" or "k-drama")
        normalized = cat_arg.replace('-', '_')
        if normalized in _KEY_TO_DEF:
            return [_KEY_TO_DEF[normalized]]

        return []

    def _print_category_list(self):
        print("\n📋  Available --category aliases (thenkiri.com)\n")
        print(f"  {'Alias':<18} {'DB categories assigned'}")
        print("  " + "─" * 58)
        for alias, keys in CATEGORY_ALIASES.items():
            if not keys:
                db_cats_str = "(no slug — skipped)"
            else:
                db_cats = []
                for k in keys:
                    if k in _KEY_TO_DEF:
                        db_cats.extend(_KEY_TO_DEF[k]['db_cats'])
                db_cats_str = ', '.join(sorted(set(db_cats)))
            print(f"  {alias:<18} {db_cats_str}")
        print()
        print("  You can also pass a full slug, e.g.:")
        print("    --category videodownload/korean-drama")
        print()








# # ── Basic usage ─────────────────────────────────────────────
# python manage.py scrape_thenkiri                          # scrape everything
# python manage.py scrape_thenkiri --list-categories        # print all aliases and exit

# # ── Category aliases ────────────────────────────────────────
# python manage.py scrape_thenkiri --category hollywood --startpage 111     # International / Hollywood movies
# python manage.py scrape_thenkiri --category kdrama       # Korean dramas (alias: korean)
# python manage.py scrape_thenkiri --category korean_movie  # Korean movies
# python manage.py scrape_thenkiri --category chinese       # Chinese movies + dramas
# python manage.py scrape_thenkiri --category cdrama        # Chinese dramas only
# python manage.py scrape_thenkiri --category bollywood     # Bollywood
# python manage.py scrape_thenkiri --category philippine    # Philippine movies (alias: filipino)
# python manage.py scrape_thenkiri --category k_variety     # K-Variety / reality shows
# python manage.py scrape_thenkiri --category series     # TV Series
# python manage.py scrape_thenkiri --category all           # everything (default)

# # ── Page control ────────────────────────────────────────────
# python manage.py scrape_thenkiri --startpage 5
# python manage.py scrape_thenkiri --startpage 1 --endpage 10
# python manage.py scrape_thenkiri --category kdrama --max-pages 3

# # ── Speed / social ──────────────────────────────────────────
# python manage.py scrape_thenkiri --no-social              # DB only, skip Twitter/Facebook
# python manage.py scrape_thenkiri --delay 1.0              # 1s between post requests (default 0.3)

# # ── Full slug (advanced) ────────────────────────────────────
# python manage.py scrape_thenkiri --category download-k-drama
# python manage.py scrape_thenkiri --category asian-movies/download-korean-movies

# # ── Combined examples ───────────────────────────────────────
# python manage.py scrape_thenkiri --category kdrama --startpage 1 --endpage 5 --no-social
# python manage.py scrape_thenkiri --category hollywood --delay 0.5 --max-pages 10