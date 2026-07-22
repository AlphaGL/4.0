"""
Management command: scrape_9jarocks
Scrapes my9jarocks.bz by crawling category listing pages, then visiting
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
python manage.py scrape_9jarocks
python manage.py scrape_9jarocks --startpage 5
python manage.py scrape_9jarocks --startpage 1 --endpage 10
python manage.py scrape_9jarocks --no-social
python manage.py scrape_9jarocks --category nollywood
python manage.py scrape_9jarocks --category hollywood
python manage.py scrape_9jarocks --category kdrama
python manage.py scrape_9jarocks --category anime
python manage.py scrape_9jarocks --category series      # all series categories
python manage.py scrape_9jarocks --category all         # everything (default)

Available friendly --category values:
  nollywood, hollywood, kdrama, chinese, thai, japanese,
  filipino, anime, bollywood, foreign, series, wrestling, ongoing, 18plus, all
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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ══════════════════════════════════════════════════════════════
# SITE CONSTANTS
# ══════════════════════════════════════════════════════════════

SITE_URL = 'https://www.my9jarocks.bz'   # site 301-redirects the bare domain to www

# ── Category definitions ──────────────────────────────────────
# Each entry:
#   'slug'     : 9jarocks URL path under /category/
#   'db_cats'  : exact DB Category names to assign on your site
#                (must match what Category.objects.get_or_create uses)
#
# DB category names are taken from your views.py sidebar:
#   'Nollywood movies', 'Korean drama', 'Hollywood movies', 'Bollywood movies'
# Plus extended names for series, anime, etc.
#
# Rule: if your DB already has a category with a given name it will be
# reused; if not, get_or_create will create it.  Keep the names consistent
# with whatever you've already seeded in the DB.

CATEGORY_DEFINITIONS = [
    # ── DB category names MUST match exactly what is in views.py get_sidebar_categories()
    # Sidebar cats: 'Nollywood movies', 'Korean drama', 'Hollywood movies',
    #               'Bollywood movies', 'Anime', 'Chinese drama', 'Thai drama',
    #               'Series', 'Animation'
    {
        'key':     'nollywood',
        'slug':    'videodownload/nollywood-movie',
        'label':   'Nollywood Movies',
        'db_cats': ['Nollywood movies'],
    },
    {
        'key':     'nollywood_series',
        'slug':    'videodownload/nollywood-tv-series',
        'label':   'Nollywood TV Series',
        'db_cats': ['Nollywood movies', 'Series'],
    },
    {
        'key':     'hollywood',
        'slug':    'videodownload/hollywood-movie',
        'label':   'Hollywood Movies',
        'db_cats': ['Hollywood movies'],
    },
    {
        'key':     'hollywood_series',
        'slug':    'videodownload/hollywood-tv-series',
        'label':   'Hollywood TV Series',
        'db_cats': ['Hollywood movies', 'Series'],
    },
    {
        'key':     'foreign',
        'slug':    'videodownload/foreign-movies',
        'label':   'Foreign Movies',
        'db_cats': ['Hollywood movies'],   # foreign movies grouped under Hollywood in sidebar
    },
    {
        'key':     'foreign_series',
        'slug':    'videodownload/other-foreign-series',
        'label':   'Foreign Series',
        'db_cats': ['Hollywood movies', 'Series'],
    },
    {
        'key':     'kdrama',
        'slug':    'videodownload/korean-drama',
        'label':   'Korean Drama',
        'db_cats': ['Korean drama'],
    },
    {
        'key':     'chinese',
        'slug':    'videodownload/chinese-drama',
        'label':   'Chinese Drama',
        'db_cats': ['Chinese drama'],
    },
    {
        'key':     'thai',
        'slug':    'videodownload/thai-drama',
        'label':   'Thai Drama',
        'db_cats': ['Thai drama'],
    },
    {
        'key':     'japanese',
        'slug':    'videodownload/japanese-drama',
        'label':   'Japanese Drama',
        'db_cats': ['Series'],   # no dedicated sidebar cat — goes under Series
    },
    {
        'key':     'filipino',
        'slug':    'videodownload/filipino-drama',
        'label':   'Filipino Drama',
        'db_cats': ['Series'],   # no dedicated sidebar cat — goes under Series
    },
    {
        'key':     'bollywood',
        'slug':    'videodownload/bollywood',
        'label':   'Bollywood Movies',
        'db_cats': ['Bollywood movies'],
    },
    {
        'key':     'anime',
        'slug':    'videodownload/anime',
        'label':   'Anime',
        'db_cats': ['Anime'],
    },
    {
        'key':     'wrestling',
        'slug':    'videodownload/pro-wrestling-fighting-sports',
        'label':   'Pro Wrestling / Sports',
        'db_cats': ['wrestling'],   # no dedicated sidebar cat — goes under Series
    },
    {
        'key':     'ongoing',
        'slug':    'videodownload/ongoing',
        'label':   'Ongoing Series',
        'db_cats': ['Series'],
    },
    {
        'key':     '18plus',
        'slug':    '18-section',
        'label':   '18+ Section',
        'db_cats': ['18plus'],   # no dedicated sidebar cat — goes under Series
    },
]

# ── Friendly alias groups (for --category flag) ────────────────
# Maps the value the user passes on the CLI to a list of definition keys.
# "all" (or no --category) runs every definition.
CATEGORY_ALIASES = {
    'nollywood':  ['nollywood', 'nollywood_series'],
    'hollywood':  ['hollywood', 'hollywood_series'],
    'kdrama':     ['kdrama'],
    'korean':     ['kdrama'],
    'chinese':    ['chinese'],
    'cdrama':     ['chinese'],
    'thai':       ['thai'],
    'japanese':   ['japanese'],
    'filipino':   ['filipino'],
    'anime':      ['anime'],
    'bollywood':  ['bollywood'],
    'foreign':    ['foreign', 'foreign_series'],
    'series':     ['nollywood_series', 'hollywood_series', 'foreign_series', 'ongoing'],
    'wrestling':  ['wrestling'],
    'sports':     ['wrestling'],
    'ongoing':    ['ongoing'],
    '18plus':     ['18plus'],
    '18':         ['18plus'],
    'all':        [d['key'] for d in CATEGORY_DEFINITIONS],  # auto-includes all keys
    # Everything EXCEPT 18+ — Adult is hidden site-wide (ad-network + SEO safety),
    # so the scheduled scrape uses this instead of 'all'.
    'all_sfw':    [d['key'] for d in CATEGORY_DEFINITIONS if d['key'] != '18plus'],
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
    'loadedfiles.org',
    'loadedfiles.net',
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
    'netnaijafiles.xyz',
    'sabishares.com',
    'meetdownload.com',
    'webloaded.com.ng',
    'wideshares.org',
    'downloadwella.com',
    'netnaija.com',
    'fzmovies.net',
    'files.my9jarocks.bz',
    'cdn.my9jarocks.bz',
    'download.my9jarocks.bz',
    'my9jarocks.bz/download',
    'wildshare.net',
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
    # Where "Get the App" button points — the direct APK download.
    'app':      'https://dl.watch2d.org/watch2d-latest.apk',
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

TELEGRAM_FOOTER = (
    "\n\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
    "📣 <b>Stay Connected:</b>\n"
    f"📱 <a href='{PLATFORM_LINKS['telegram']}'>Join our Telegram Channel</a>\n"
    f"📘 <a href='{PLATFORM_LINKS['facebook']}'>Like us on Facebook</a>\n"
    f"🐦 <a href='{PLATFORM_LINKS['twitter']}'>Follow us on X/Twitter</a>\n"
    f"🌍 <a href='{PLATFORM_LINKS['website']}'>Visit Watch2D.org</a>\n"
    "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"
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
        tg =""
        # tg = "#Watch2D #KDrama #KoreanDrama #KoreanSeries #AsianDrama #FreeDownload #HDDownload #NowStreaming #MustWatch #BingeWatch #KDramaEnglishSub #WatchFree #Trending"
        tw = "#Watch2D #KDrama #KoreanDrama #AsianDrama #FreeDownload"
    elif any(kw in combined for kw in ['nigerian', 'nollywood', 'naija', 'nigeria']):
        tg = ""
        # tg = "#Watch2D #Nollywood #NigerianMovies #NaijaMovies #AfricanMovies #FreeDownload #HDDownload #NowStreaming #MustWatch #BingeWatch #AfricanCinema #WatchFree #Trending"
        tw = "#Watch2D #Nollywood #NaijaMovies #AfricanMovies #FreeDownload"
    elif any(kw in combined for kw in ['turkish', 'turkey', 'dizi']):
        tg = ""
        # tg = "#Watch2D #TurkishSeries #TurkishDrama #Dizi #FreeDownload #HDDownload #NowStreaming #MustWatch #BingeWatch #EnglishSubtitles #WatchFree #Trending"
        tw = "#Watch2D #TurkishDrama #Dizi #TurkishSeries #FreeDownload"
    elif any(kw in combined for kw in ['indian', 'bollywood', 'hindi', 'telugu', 'tamil']):
        tg = ""
        # tg = "#Watch2D #Bollywood #IndianSeries #HindiSeries #FreeDownload #HDDownload #NowStreaming #MustWatch #IndianCinema #WatchFree #Trending"
        tw = "#Watch2D #Bollywood #IndianSeries #HindiSeries #FreeDownload"
    elif any(kw in combined for kw in ['chinese', 'china', 'cdrama']):
        tg = ""
        # tg = "#Watch2D #CDrama #ChineseDrama #ChineseSeries #AsianDrama #FreeDownload #HDDownload #NowStreaming #MustWatch #BingeWatch #WatchFree #Trending"
        tw = "#Watch2D #CDrama #ChineseDrama #AsianDrama #FreeDownload"
    elif any(kw in combined for kw in ['anime']):
        tg = ""
        # tg = "#Watch2D #Anime #AnimeDownload #AnimeSeries #FreeDownload #HDDownload #NowStreaming #MustWatch #BingeWatch #WatchFree #Trending"
        tw = "#Watch2D #Anime #AnimeDownload #FreeDownload"
    elif movie.is_series:
        tg = ""
        # tg = "#Watch2D #NewSeries #TVSeries #Series #NowStreaming #FreeDownload #HDDownload #MustWatch #BingeWatch #WatchFree #Trending"
        tw = "#Watch2D #TVSeries #NowStreaming #FreeDownload #BingeWatch"
    else:
        tg = ""
        # tg = "#Watch2D #NewMovie #Hollywood #FullMovie #FreeDownload #HDMovie #NowStreaming #MustWatch #MovieLovers #WatchFree #Trending"
        tw = "#Watch2D #NewMovie #Hollywood #FreeDownload #MustWatch"
    return tg, tw, tg



# ══════════════════════════════════════════════════════════════
# TELEGRAM POSTER
# ══════════════════════════════════════════════════════════════

def _post_movie_to_telegram(movie, is_new: bool):
    try:
        from django.conf import settings
        from automation.telegram import send_photo, send_message

        channel  = getattr(settings, 'TELEGRAM_MOVIES_CHANNEL', '')
        site_url = getattr(settings, 'SITE_URL', 'https://watch2d.org')
        if not channel:
            return

        # 'Download Link': route through the Telegram Mini App ONLY when
        # TELEGRAM_MINIAPP_URL is set (do that on the account whose DB the Mini
        # App/site serves — the web DB). Otherwise use the normal site link, so
        # the other account's posts (different DB, different ids) don't 404.
        # Twitter/Facebook below always keep the plain site URL.
        miniapp = getattr(settings, 'TELEGRAM_MINIAPP_URL', '')
        if miniapp:
            url = f"{miniapp}?startapp=movie{movie.pk}"
        else:
            url = f"{site_url}/movie/{movie.pk}/{movie.slug}/"
        tg_tags, _, _ = _detect_hashtags(movie)

        if is_new:
            emoji = "🎬" if not movie.is_series else "📺"
            lines = [f"{emoji} <b>{movie.title}</b>", ""]

            if movie.description:
                lines += [f"{movie.description[:250]}...", ""]

            cats = movie.categories.all()
            if cats:
                lines.append(f"🏷 <b>Genre:</b> {', '.join(c.name for c in cats[:4])}")

            if movie.is_series:
                status = "✅ Completed" if movie.completed else "🔄 Ongoing Series"
                lines.append(f"📡 <b>Status:</b> {status}")

            lines += [
                "",
                "⬇️ <b>Tap the Download Link button below</b> 👇",
                "⚠️ <i>Open it in Chrome — not Telegram's built-in browser.</i>",
                "",
                tg_tags,
                TELEGRAM_FOOTER,
            ]

            from automation.models import TelegramPost
            _, created = TelegramPost.objects.get_or_create(
                content_type='movie',
                content_id=movie.id,
                defaults={'content_title': movie.title, 'success': True},
            )

        else:
            episode_label = movie.title_b or "New Episode"
            lines = [
                "🆕 <b>New Episode Available!</b>", "",
                f"📺 <b>{movie.title}</b>",
                f"🎬 <b>Episode:</b> {episode_label}",
                "",
                "⬇️ <b>Tap the Download Link button below</b> 👇",
                "⚠️ <i>Open it in Chrome — not Telegram's built-in browser.</i>",
                "",
                tg_tags,
                TELEGRAM_FOOTER,
            ]

            from automation.models import TelegramUpdate
            from django.utils import timezone as _tz_now
            from datetime import timedelta as _tz_td
            # Cooldown: the scraped episode label (title_b) is volatile — minor
            # formatting changes looked like new episodes and re-posted the same
            # series repeatedly. Cap episode-update posts to once per 12h / movie.
            if TelegramUpdate.objects.filter(
                content_type='movie', content_id=movie.id,
                posted_at__gte=_tz_now.now() - _tz_td(hours=12),
            ).exists():
                return
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
        url           = f"{site_url}/movie/{movie.pk}/{movie.slug}/"
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
        url           = f"{site_url}/movie/{movie.pk}/{movie.slug}/"
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
# MASTER POSTER  (Telegram is DISABLED)
# ══════════════════════════════════════════════════════════════

def _post_to_all_platforms(movie, is_new: bool):
    # ⚠️  Telegram is DISABLED — uncomment below line when ready
    # _post_movie_to_twitter(movie,  is_new=is_new)
    _post_movie_to_telegram(movie, is_new=is_new)
    _post_movie_to_facebook(movie, is_new=is_new)


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
    Extract all post URLs from a category listing page.
    9jarocks uses standard Jannah theme article links.
    """
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
    """Check whether a 'Next' pagination link exists."""
    soup = BeautifulSoup(html, 'html.parser')
    for a in soup.find_all('a', href=True):
        text = a.get_text(strip=True).lower()
        cls  = ' '.join(a.get('class', []))
        if text in ('next', '»', 'next page') or 'next' in cls or 'nextpostslink' in cls:
            return True
    return False


def parse_post_page(html: str, url: str) -> dict | None:
    """
    Parse a single 9jarocks post page.
    Returns a dict with: title, description, image_url, video_url,
                         download_links ([{url, label}]), categories,
                         is_series, is_complete
    or None if it looks like a non-movie page.
    """
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
    # Scope to .entry-header or .entry-header-outer ONLY — the sidebar also uses
    # class="post-cat" on genre/alphabet links which would pollute the list.
    _cat_scope = (
        soup.find(class_='entry-header') or
        soup.find(class_='entry-header-outer') or
        soup.find(class_='post-header') or
        soup  # last resort: full page (old behaviour, but sidebar will be filtered below)
    )
    for a in _cat_scope.select('a.post-cat'):
        name = a.get_text(strip=True)
        # Skip generic/sidebar labels
        if name and name.lower() not in ('video', 'uncategorized') and len(name) > 1:
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

    def _episode_prefix(anchor):
        """
        Try to find an episode/part label that sits just before this <a> tag.

        9jarocks uses two patterns:
          Pattern A – label in a <p> that contains only the <em> + <br> + <a>:
            <p><em>EPISODE 5 </em><br/><a class="fa-fa-download" href="...">DOWNLOAD</a></p>

          Pattern B – label in a plain <em> or text node inside the same <p>:
            <p><em>EPISODE 5</em><br><a ...>DOWNLOAD</a></p>

        We walk up to the closest <p> ancestor and collect any <em> or leading
        text found before the <a>.  If nothing useful is there we return ''.
        """
        # Walk up to the enclosing <p> (or <div> as a last resort)
        parent = anchor.parent
        for _ in range(4):  # max 4 levels up
            if parent is None:
                break
            if parent.name in ('p', 'div', 'li'):
                break
            parent = parent.parent

        if parent is None:
            return ''

        # Collect text from <em> or bare text nodes that come BEFORE the <a>
        parts = []
        for sibling in parent.children:
            if sibling is anchor:
                break  # stop once we hit the link itself
            if hasattr(sibling, 'get_text'):
                txt = sibling.get_text(' ', strip=True)
            else:
                txt = str(sibling).strip()
            if txt:
                parts.append(txt)

        prefix = ' '.join(parts).strip()
        # Only keep it when it looks like an episode/part marker
        # (avoids accidentally picking up ZIP labels, ads, etc.)
        if prefix and re.search(
            r'episode|ep\.?\s*\d|part\s*\d|zip|s\d{1,2}e\d|batch',
            prefix, re.IGNORECASE
        ):
            return prefix
        return ''

    for a in content_div.find_all('a', class_='fa-fa-download'):
        href     = a.get('href', '').strip()
        btn_text = a.get_text(strip=True) or href   # e.g. "DOWNLOAD" or "[SERVER 1]"
        if not href or href in seen_urls:
            continue
        if any(ad in href.lower() for ad in AD_DOMAINS):
            print(f"   🚫 [ad skipped] {btn_text} → {href[:80]}")
            continue
        seen_urls.add(href)

        # Build a rich label: "EPISODE 5 – DOWNLOAD" when a prefix exists,
        # otherwise just use the button text (works fine for plain movies).
        prefix = _episode_prefix(a)
        label  = f"{prefix} – {btn_text}" if prefix else btn_text

        download_links.append({'url': href, 'label': label})
        print(f"   🔗 [fa-fa-download] {label} → {href}")

    if not download_links:
        for a in content_div.find_all('a', href=True):
            href       = a.get('href', '').strip()
            btn_text   = a.get_text(strip=True) or href
            href_lower = href.lower()

            if not href or href.startswith('#') or 'javascript' in href_lower:
                continue
            if any(ad in href_lower for ad in AD_DOMAINS):
                continue
            if any(skip in href_lower for skip in [
                'facebook.com', 'twitter.com', 't.me', 'youtube.com/watch?',
                'imdb.com', 'wp-admin', '#respond', 'mailto:', 'my9jarocks.bz/category',
                'my9jarocks.bz/tag',
            ]):
                continue

            is_dl = (
                any(d in href_lower for d in KNOWN_DOWNLOAD_DOMAINS)
                or any(href_lower.endswith(ext) for ext in FILE_EXTENSIONS)
                or any(kw in btn_text.lower() for kw in DOWNLOAD_KEYWORDS)
                or any(kw in href_lower  for kw in ['/dl/', '/get/', '/file/', 'download'])
            )

            if is_dl and href not in seen_urls:
                seen_urls.add(href)
                prefix = _episode_prefix(a)
                label  = f"{prefix} – {btn_text}" if prefix else btn_text
                download_links.append({'url': href, 'label': label})
                print(f"   🔗 [fallback] {label} → {href}")

    is_series = bool(
        re.search(
            r'\bS\d{1,2}\b|\bSeason\s?\d{1,2}\b|\bEpisode\b|\bEp\.?\s?\d+\b|Series\b',
            title_raw, re.IGNORECASE
        )
    )
    is_complete = bool(re.search(r'\bcomplete(d)?\b', title_raw, re.IGNORECASE))

    # ── Extract vi_ fields from the blockquote meta dict ────────────────────
    # 9jarocks blockquote lines look like:
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
            if movie is None:
                # Fallback: match on normalized title so a source re-titling
                # "Korea No.1" as "Korea No. 1" doesn't create a duplicate
                # (which would get posted, then deleted by cleanse_db → 404).
                from movies.scraper_utils import find_movie_by_normalized_title
                movie = find_movie_by_normalized_title(title)
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
    9jarocks category slug we are currently crawling.

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
        'Scrape my9jarocks.bz category pages → save to DB → '
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
                '  nollywood, hollywood, kdrama, chinese, thai,\n'
                '  japanese, filipino, anime, foreign, series,\n'
                '  wrestling, ongoing, 18plus, all  (default: all)\n'
                'Or a full 9jarocks slug like "videodownload/korean-drama".'
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

    def handle(self, *args, **options):
        from django.db import connection

        # ── --list-categories ──────────────────────────────────
        if options['list_categories']:
            self._print_category_list()
            return

        start_page = options['startpage']
        end_page   = options['endpage']
        max_pages  = options['max_pages']
        no_social  = options['no_social']
        delay      = options['delay']
        cat_arg    = (options.get('category') or 'all').strip().lower()

        # ── Resolve --category to a list of CATEGORY_DEFINITIONS ─
        cats_to_crawl = self._resolve_category_arg(cat_arg)
        if not cats_to_crawl:
            self.stderr.write(
                f"❌  Unknown category '{cat_arg}'.\n"
                f"    Run with --list-categories to see all options."
            )
            return

        print("=" * 60)
        print("🚀  my9jarocks.bz scraper starting")
        print(f"    Method  : Category page HTML scraping")
        print(f"    Cats    : {', '.join(d['label'] for d in cats_to_crawl)}")
        print(f"    Pages   : {start_page} → {end_page or '∞'}"
              + (f"  (max {max_pages})" if max_pages else ""))
        if no_social:
            print("    Social  : DISABLED (--no-social)")
        else:
            print("    Social  : Twitter + Facebook  (Telegram is OFF)")
        print("=" * 60)

        scraper = _make_scraper()

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

    # ──────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────

    def _resolve_category_arg(self, cat_arg: str) -> list[dict]:
        """
        Turn the --category value into a list of CATEGORY_DEFINITIONS dicts.

        Accepts:
          • A friendly alias key (e.g. "nollywood", "kdrama", "all")
          • A full 9jarocks slug (e.g. "videodownload/korean-drama")
          • A bare slug segment (e.g. "korean-drama")
        """
        # 1. Friendly alias
        if cat_arg in CATEGORY_ALIASES:
            keys = CATEGORY_ALIASES[cat_arg]
            return [_KEY_TO_DEF[k] for k in keys if k in _KEY_TO_DEF]

        # 2. Full slug match
        if cat_arg in _SLUG_TO_DEF:
            return [_SLUG_TO_DEF[cat_arg]]

        # 3. Bare slug → try with prefix
        full_slug = f"videodownload/{cat_arg}"
        if full_slug in _SLUG_TO_DEF:
            return [_SLUG_TO_DEF[full_slug]]

        # 4. Partial key match (e.g. "nollywood_series" or "k-drama")
        normalized = cat_arg.replace('-', '_')
        if normalized in _KEY_TO_DEF:
            return [_KEY_TO_DEF[normalized]]

        return []

    def _print_category_list(self):
        print("\n📋  Available --category aliases\n")
        print(f"  {'Alias':<18} {'DB categories assigned'}")
        print("  " + "─" * 58)
        for alias, keys in CATEGORY_ALIASES.items():
            if not keys:
                db_cats_str = "(no 9jarocks slug — skipped)"
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
# python manage.py scrape_9jarocks                          # scrape everything
# python manage.py scrape_9jarocks --list-categories        # print all aliases and exit

# # ── Category aliases ────────────────────────────────────────
# python manage.py scrape_9jarocks --category nollywood     # movies + series
# python manage.py scrape_9jarocks --category hollywood     # movies + series
# python manage.py scrape_9jarocks --category kdrama        # (alias: korean)
# python manage.py scrape_9jarocks --category chinese       # (alias: cdrama)
# python manage.py scrape_9jarocks --category thai
# python manage.py scrape_9jarocks --category japanese
# python manage.py scrape_9jarocks --category filipino
# python manage.py scrape_9jarocks --category anime          # all series categories + ongoing
# python manage.py scrape_9jarocks --category wrestling 
# python manage.py scrape_9jarocks --category 18plus        # (alias: 18)
# python manage.py scrape_9jarocks --category all           # everything (default)

# # ── Page control ────────────────────────────────────────────
# python manage.py scrape_9jarocks --startpage 5
# python manage.py scrape_9jarocks --startpage 1 --endpage 10
# python manage.py scrape_9jarocks --category nollywood --max-pages 3

# # ── Speed / social ──────────────────────────────────────────
# python manage.py scrape_9jarocks --no-social              # DB only, skip Twitter/Facebook
# python manage.py scrape_9jarocks --delay 1.0              # 1s between post requests (default 0.3)

# # ── Full slug (advanced) ────────────────────────────────────
# python manage.py scrape_9jarocks --category videodownload/korean-drama
# python manage.py scrape_9jarocks --category videodownload/nollywood-tv-series

# # ── Combined examples ───────────────────────────────────────
# python manage.py scrape_9jarocks --category kdrama --startpage 1 --endpage 5 --no-social
# python manage.py scrape_9jarocks --category nollywood --delay 0.5 --max-pages 10