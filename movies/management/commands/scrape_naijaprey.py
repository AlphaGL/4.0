"""
Management command: scrape_naijaprey
Scrapes naijaprey.tv via its WordPress REST API, then parses each post's
rendered HTML to extract title, image, description, metadata and download links.

WHY REST API (not HTML crawling like 9jarocks / thenkiri):
  • naijaprey.tv exposes a fully working /wp-json/wp/v2/posts endpoint that
    returns the entire post (title, content HTML, excerpt, categories, tags,
    featured image URL) as clean JSON — no Cloudflare/HTML-grid scraping needed.
  • Two content categories exist on the site:
        1228  Movie   (download-movies-vxi)  — ~9.8k posts
        1518  Series  (series-download-v2)   — ~1.2k posts
    Genre / country / language are NOT separate site categories — they live
    inside each post's content HTML, so we assign DB categories *smartly* from
    the detected country/genre instead of from a fixed source-slug.

Post content HTML structure (confirmed from live /wp-json output):
  • Featured image : post['jetpack_featured_media_url']  (most reliable)
  • Description    : first <p class="wp-block-paragraph"> plot lines
  • Metadata       : <p><strong>Genre:</strong> ...</p>, Stars, Release Date,
                     Country, Ratings, Language, Subtitles, Source, Runtime,
                     Episodes — one key:value per <p>
  • Trailer        : <iframe src="youtube.com/embed/..."> in .video-container
  • Movie download : <a class="button" href="...np-downloader...">Download</a>
                     (+ optional <a class="button">Subtitle</a>)
  • Series episodes: <a class="se-button" href="...">Episode N</a> / E1156 ...
  • Latest episode : post['meta']['_subtitle']  e.g. "E12", "E1168"

Usage examples
──────────────
python manage.py scrape_naijaprey
python manage.py scrape_naijaprey --startpage 5
python manage.py scrape_naijaprey --startpage 1 --endpage 10
python manage.py scrape_naijaprey --no-social
python manage.py scrape_naijaprey --category movies
python manage.py scrape_naijaprey --category series
python manage.py scrape_naijaprey --category all

Available friendly --category values:
  movies, series, all
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

SITE_URL = 'https://www.naijaprey.tv'
API_POSTS = f'{SITE_URL}/wp-json/wp/v2/posts'

# Tag every download link this scraper creates with its origin, so the app /
# site can group same-movie links from different sources and fail over between
# them (DownloadLink.source — see models.py "Multi-source fallback metadata").
SOURCE_NAME = 'naijaprey'

# ── Category definitions ──────────────────────────────────────
# naijaprey only splits content into Movie (1228) and Series (1518).
# 'cat_id'  : WordPress category ID used as ?categories=<id>
# 'is_series': default is_series flag for everything under this category
# DB categories are NOT taken from here — they are detected per-post from
# the content's Country / Language / Genre (see detect_db_categories()).
CATEGORY_DEFINITIONS = [
    {
        'key':       'movies',
        'cat_id':    1228,
        'label':     'Movies',
        'is_series': False,
    },
    {
        'key':       'series',
        'cat_id':    1518,
        'label':     'Series',
        'is_series': True,
    },
]

CATEGORY_ALIASES = {
    'movies':  ['movies'],
    'movie':   ['movies'],
    'series':  ['series'],
    'tv':      ['series'],
    'all':     [d['key'] for d in CATEGORY_DEFINITIONS],
}

_KEY_TO_DEF = {d['key']: d for d in CATEGORY_DEFINITIONS}


# ── Ad / monetization redirect domains to SKIP ────────────────
AD_DOMAINS = [
    'associationfoam.com',
    'obqj2.com',
    'cranialhubbed.com',
    'admiredjumper.com',
    'getdirectbonus.com',
    'push-sdk.com',
    'go.getdirectbonus.com',
    'airingsjerky.com',
]

# Download domains and keywords for link detection
KNOWN_DOWNLOAD_DOMAINS = [
    'np-downloader.com',
    'vdl.np-downloader.com',
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
    'sabishares.com',
    'downloadwella.com',
    'wildshare.net',
]

FILE_EXTENSIONS = ['.mp4', '.mkv', '.avi', '.mov', '.zip', '.rar', '.srt']

DOWNLOAD_KEYWORDS = [
    'download', '480p', '720p', '1080p', '4k', 'hd',
    'episode', 'fast server', 'slow server', 'mirror',
    'part ', 'batch', 'sdm_downloads',
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

            lines = [
                f"🎞  <b>{movie.title}</b>",
                "",
            ]

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
            if getattr(movie, 'vi_episodes', ''):
                meta_lines.append(f"🎞  <b>Episodes:</b> {movie.vi_episodes}")
            if movie.is_series:
                status = "✅ Completed" if movie.completed else "🔄 Ongoing"
                meta_lines.append(f"📡  <b>Status:</b>  {status}")
            if meta_lines:
                lines += meta_lines + [""]

            if movie.description:
                desc = movie.description[:280].rstrip()
                if len(movie.description) > 280:
                    desc += "…"
                lines += [f"📖  <i>{desc}</i>", ""]

            lines += [
                f"{'▬' * 22}",
                "",
                "⬇️  <b>Tap the Download Link button below</b> 👇",
                "⚠️  <i>Open it in Chrome — not Telegram's built-in browser.</i>",
                "",
                f"{'▬' * 22}",
                "",
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
                f"📺  <b>{movie.title}</b>",
                f"🎬  <b>Episode:</b>  {episode_label}",
                "",
                f"{'▬' * 22}",
                "",
                "⬇️  <b>Tap the Download Link button below</b> 👇",
                "⚠️  <i>Open it in Chrome — not Telegram's built-in browser.</i>",
                "",
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
            if getattr(movie, 'vi_episodes', ''):
                lines.append(f"🎞  Episodes: {movie.vi_episodes}")
            if movie.is_series:
                status = "✅ Completed" if movie.completed else "🔄 Ongoing"
                lines.append(f"📡  Status:   {status}")
            lines.append("")

            if movie.description:
                desc = movie.description[:350].rstrip()
                if len(movie.description) > 350:
                    desc += "…"
                lines += [f"📖  {desc}", ""]

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


# ══════════════════════════════════════════════════════════════
# REST API + HTML PARSERS
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
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": SITE_URL,
    })
    return scraper


def fetch_posts_page(scraper, cat_id: int, page: int, per_page: int = 20):
    """
    Fetch one page of posts from the WordPress REST API for a category.

    Returns (posts_list, total_pages).  posts_list is [] on a 400/404
    (WordPress returns 400 'rest_post_invalid_page_number' past the last page).
    """
    params = {
        'categories': cat_id,
        'page':       page,
        'per_page':   per_page,
        '_fields':    'id,link,title,content,excerpt,meta,categories,'
                      'jetpack_featured_media_url',
    }
    resp = scraper.get(API_POSTS, params=params, timeout=25)

    if resp.status_code in (400, 404):
        # Past the last page — WP returns 400 rest_post_invalid_page_number
        return [], 0

    resp.raise_for_status()
    total_pages = int(resp.headers.get('X-WP-TotalPages', 0) or 0)
    try:
        posts = resp.json()
    except Exception:
        posts = []
    if not isinstance(posts, list):
        posts = []
    return posts, total_pages


def _meta_from_content(content_soup) -> dict:
    """
    Extract the <p><strong>Key:</strong> Value</p> metadata lines.
    naijaprey renders one key:value per <p class="wp-block-paragraph">.
    """
    meta = {}
    for p in content_soup.find_all('p'):
        text = p.get_text(' ', strip=True)
        if ':' not in text:
            continue
        key, _, val = text.partition(':')
        k = key.strip().lower()
        v = val.strip()
        if k and v and len(k) < 30:
            meta.setdefault(k, v)
    return meta


# Metadata keys that mark the END of the plot/description block
_META_KEY_RE = re.compile(
    r'^\s*(Genre|Stars|Star|Cast|Release\s*Date|Country|Ratings?|Language|'
    r'Subtitles?|Source|Runtime|Running\s*Time|Episodes?|Quality|Director)\b\s*:',
    re.IGNORECASE,
)


def _description_from_content(content_soup, title_raw: str) -> str:
    """
    Collect the leading plot paragraphs (everything before the first metadata
    line and before the bold standalone title line).
    """
    parts = []
    title_low = title_raw.strip().lower()
    for p in content_soup.find_all('p'):
        text = p.get_text(' ', strip=True)
        if not text:
            continue
        # Stop at the first metadata key line (Genre:, Stars:, etc.)
        if _META_KEY_RE.match(text):
            break
        # Skip the bold standalone title paragraph
        if text.lower().rstrip(':').strip() == title_low:
            continue
        if text.lower() == 'advertisements':
            continue
        parts.append(text)

    desc = ' '.join(parts).strip()
    desc = re.sub(r'\s+', ' ', desc)
    if len(desc) > 600:
        desc = desc[:600].rsplit(' ', 1)[0] + '...'
    return desc


def parse_post(post: dict, default_is_series: bool) -> dict | None:
    """
    Parse a single WordPress post JSON object into our normalized dict.

    Returns None if the post has no usable title or download links.
    """
    title_raw = (post.get('title') or {}).get('rendered', '') or ''
    title_raw = BeautifulSoup(title_raw, 'html.parser').get_text(strip=True)
    if not title_raw or len(title_raw) < 2:
        return None

    content_html = (post.get('content') or {}).get('rendered', '') or ''
    soup = BeautifulSoup(content_html, 'html.parser')

    # ── Image (featured media from API is most reliable) ─────────
    image_url = (post.get('jetpack_featured_media_url') or '').strip()
    if not image_url:
        figimg = soup.find('img')
        if figimg:
            image_url = (figimg.get('src') or figimg.get('data-src') or '').strip()

    # ── Trailer / video iframe (YouTube) ─────────────────────────
    video_url = ''
    for iframe in soup.find_all('iframe', src=True):
        src = iframe['src'].strip()
        if any(d in src for d in ('youtube.com/embed', 'youtu.be', 'youtube-nocookie.com')):
            # Skip empty embeds like ".../embed/"
            if re.search(r'/embed/[\w-]{5,}', src):
                video_url = src
                break

    # ── Metadata key:value lines ─────────────────────────────────
    meta = _meta_from_content(soup)

    # ── Description (plot paragraphs before the metadata block) ──
    description = _description_from_content(soup, title_raw)
    if not description:
        excerpt_html = (post.get('excerpt') or {}).get('rendered', '') or ''
        description = BeautifulSoup(excerpt_html, 'html.parser').get_text(' ', strip=True)
        description = re.sub(r'\[\s*…?\s*\].*$', '', description).strip()

    # ── Download links ───────────────────────────────────────────
    # Movies: <a class="button">Download / Subtitle</a>
    # Series: <a class="se-button">Episode N</a>
    download_links = []
    seen_urls = set()
    for a in soup.find_all('a', href=True):
        href     = a.get('href', '').strip()
        btn_text = a.get_text(strip=True) or 'Download'
        href_low = href.lower()

        if not href or href.startswith('#') or 'javascript' in href_low:
            continue
        if href in seen_urls:
            continue
        if any(ad in href_low for ad in AD_DOMAINS):
            print(f"   🚫 [ad skipped] {btn_text} → {href[:80]}")
            continue

        cls = ' '.join(a.get('class', []))
        is_dl = (
            'button' in cls
            or 'se-button' in cls
            or any(d in href_low for d in KNOWN_DOWNLOAD_DOMAINS)
            or any(href_low.endswith(ext) for ext in FILE_EXTENSIONS)
            or any(kw in href_low for kw in ['sdm_downloads', '/dl/', '/get/', '/file/', 'download'])
            or any(kw in btn_text.lower() for kw in DOWNLOAD_KEYWORDS)
        )
        if not is_dl:
            continue

        seen_urls.add(href)
        download_links.append({'url': href, 'label': btn_text})
        print(f"   🔗 {btn_text} → {href}")

    # Put any subtitle (.srt) links last so the primary download_url
    # is the actual video, not the subtitle file.
    download_links.sort(key=lambda d: d['url'].lower().endswith('.srt'))

    is_series = bool(
        default_is_series
        or re.search(
            r'\bS\d{1,2}\b|\bSeason\s?\d{1,2}\b|\bEpisode\b|\bEp\.?\s?\d+\b|Series\b',
            title_raw, re.IGNORECASE,
        )
    )
    # ── Latest-episode label from post meta._subtitle (e.g. "E1168") ──
    episode_meta = ''
    post_meta = post.get('meta') or {}
    if isinstance(post_meta, dict):
        episode_meta = (post_meta.get('_subtitle') or '').strip()

    is_complete = bool(re.search(r'\bcomplete(d)?\b', title_raw, re.IGNORECASE))
    if 'complete' in meta.get('episodes', '').lower():
        is_complete = True
    if 'complete' in episode_meta.lower():
        is_complete = True

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
        'vi_subtitle': _mv(['subtitles', 'subtitle', 'sub']),
        'vi_genre':    _mv(['genre', 'genres', 'category']),
        'vi_cast':     _mv(['stars', 'star', 'cast', 'actors', 'starring']),
        'vi_episodes': _mv(['episodes', 'episode', 'total episodes', 'no of episodes']),
        'vi_status':   _mv(['status', 'series status']),
        'vi_runtime':  _mv(['runtime', 'running time', 'duration', 'run time']),
        'vi_filesize': _mv(['file size', 'filesize', 'size']),
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
        'is_series':      is_series,
        'is_complete':    is_complete,
        'episode_meta':   episode_meta,
        'meta':           meta,
        **vi,
    }


# ══════════════════════════════════════════════════════════════
# SMART DB CATEGORY DETECTION
# ══════════════════════════════════════════════════════════════

def detect_db_categories(parsed: dict, is_series: bool) -> list[str]:
    """
    naijaprey doesn't separate content by country/genre at the site level,
    so we infer the DB category from the parsed Country / Language / Genre.

    Returns a list of DB category names (matching scraper_utils canonical names).
    Always appends 'Series' for series content.
    """
    country  = (parsed.get('vi_country') or '').lower()
    language = (parsed.get('vi_language') or '').lower()
    genre    = (parsed.get('vi_genre') or '').lower()
    title    = (parsed.get('title_raw') or '').lower()
    blob     = ' '.join([country, language, genre, title])

    is_animation = any(k in genre for k in ['animation', 'anime'])

    cats: list[str] = []
    if any(k in blob for k in ['korea', 'korean']):
        cats = ['Korean drama']
    elif any(k in blob for k in ['japan', 'japanese']):
        # Japanese content on naijaprey is almost always anime
        cats = ['Anime'] if is_animation else ['Anime']
    elif any(k in blob for k in ['china', 'chinese', 'hong kong', 'taiwan']):
        cats = ['Chinese drama']
    elif 'thai' in blob or 'thailand' in blob:
        cats = ['Thai drama']
    elif any(k in blob for k in ['philippine', 'filipino', 'tagalog']):
        cats = ['Filipino drama']
    elif any(k in blob for k in ['turkey', 'turkish']):
        cats = ['Turkish drama']
    elif any(k in blob for k in ['india', 'indian', 'hindi', 'bollywood', 'telugu', 'tamil']):
        cats = ['Bollywood movies']
    elif any(k in blob for k in ['nigeria', 'nigerian', 'nollywood']):
        cats = ['Nollywood movies']
    elif is_animation:
        cats = ['Animation']
    else:
        cats = ['Hollywood movies']

    if is_series and 'Series' not in cats:
        cats.append('Series')
    return cats


def assign_db_categories(movie, db_cats: list[str]):
    """Replace the movie's categories with exactly the detected set."""
    if not db_cats:
        return
    target_cats = []
    for name in db_cats:
        cat_obj = get_or_create_category(name.strip())
        if cat_obj:
            target_cats.append(cat_obj)
    movie.categories.set(target_cats)
    for cat in target_cats:
        print(f"      🏷  Assigned category: '{cat.name}'")


# ══════════════════════════════════════════════════════════════
# TITLE CLEANING
# ══════════════════════════════════════════════════════════════

_SEASON_RE = re.compile(
    r'\b(?:S(?:eason\s*)?|Season\s*)0*(\d{1,2})\b',
    re.IGNORECASE,
)


def _canonicalize_season(text: str) -> str:
    return _SEASON_RE.sub(lambda m: f"Season {int(m.group(1))}", text)


def clean_title_parts(raw: str) -> tuple[str, str]:
    """Returns (main_title, episode_label)."""
    title       = re.sub(r'\s+', ' ', raw).strip()
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


# Hosts whose download links THIS scraper owns. Used so we only prune our own
# stale links and never delete links contributed by another source (9jarocks,
# thenkiri, …). Keeping links from multiple sources is what gives the site/app a
# working fallback when one host's link is dead.
_OWN_LINK_HOSTS = ('np-downloader.com', 'naijaprey.tv')


def _is_own_link(url: str) -> bool:
    """True if this URL came from naijaprey (so it's safe for us to prune)."""
    try:
        host = (urlparse(url).netloc or '').lower()
    except Exception:
        return False
    return any(host == h or host.endswith('.' + h) for h in _OWN_LINK_HOSTS)


def _season_variants(title: str) -> list[str]:
    m = _SEASON_RE.search(title)
    if not m:
        return [title]
    n      = int(m.group(1))
    prefix = title[:m.start()]
    suffix = title[m.end():]
    forms = [
        f"Season {n}", f"Season {n:02d}",
        f"S{n}",       f"S{n:02d}",
        f"SEASON {n}", f"season {n}",
    ]
    return list(dict.fromkeys(f"{prefix}{f}{suffix}" for f in forms))


def find_existing_movie(title: str, max_retries: int = 3):
    from django.db import connection

    base_title = re.sub(r'\s*\((complete|completed)\)\s*$', '', title, flags=re.IGNORECASE).strip()
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


# ══════════════════════════════════════════════════════════════
# MANAGEMENT COMMAND
# ══════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = (
        'Scrape naijaprey.tv via its WordPress REST API → save to DB → '
        'optionally post to Telegram + Facebook'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--startpage', type=int, default=1,
            help='API page to start from (default: 1)',
        )
        parser.add_argument(
            '--endpage', type=int, default=None,
            help='Stop after this API page (inclusive)',
        )
        parser.add_argument(
            '--max-pages', type=int, default=None,
            help='Maximum API pages to fetch per category',
        )
        parser.add_argument(
            '--per-page', type=int, default=20,
            help='Posts per API request (max 100, default: 20)',
        )
        parser.add_argument(
            '--category', type=str, default='all',
            help='Which category to scrape: movies, series, all  (default: all)',
        )
        parser.add_argument(
            '--no-social', action='store_true', default=False,
            help='Save to DB only — skip all social posts',
        )
        parser.add_argument(
            '--delay', type=float, default=0.3,
            help='Seconds to wait between API page requests (default: 0.3)',
        )
        parser.add_argument(
            '--list-categories', action='store_true', default=False,
            help='Print all available category aliases and exit',
        )

    def handle(self, *args, **options):
        from django.db import connection

        if options['list_categories']:
            self._print_category_list()
            return

        start_page = options['startpage']
        end_page   = options['endpage']
        max_pages  = options['max_pages']
        per_page   = max(1, min(options['per_page'], 100))
        no_social  = options['no_social']
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
        print("🚀  naijaprey.tv scraper starting")
        print(f"    Method  : WordPress REST API (/wp-json/wp/v2/posts)")
        print(f"    Cats    : {', '.join(d['label'] for d in cats_to_crawl)}")
        print(f"    Pages   : {start_page} → {end_page or '∞'}"
              + (f"  (max {max_pages})" if max_pages else ""))
        if no_social:
            print("    Social  : DISABLED (--no-social)")
        else:
            print("    Social  : Telegram + Facebook")
        print("=" * 60)

        scraper = _make_scraper()

        total_posts_scraped = 0
        total_new           = 0
        total_updated       = 0

        for cat_def in cats_to_crawl:
            cat_id        = cat_def['cat_id']
            default_serie = cat_def['is_series']

            print(f"\n\n{'═'*60}")
            print(f"📂  Category : {cat_def['label']}")
            print(f"    Cat ID   : {cat_id}")
            print(f"    Endpoint : {API_POSTS}?categories={cat_id}")
            print(f"{'═'*60}")

            page            = start_page
            pages_crawled   = 0
            consecutive_err = 0

            while True:
                if end_page and page > end_page:
                    print(f"\n✅ Reached end page {end_page}.")
                    break
                if max_pages and pages_crawled >= max_pages:
                    print(f"\n✅ Fetched {max_pages} pages for this category.")
                    break

                print(f"\n{'─'*60}")
                print(f"🌐 API page {page} (categories={cat_id}, per_page={per_page})")

                if delay > 0:
                    time.sleep(delay)

                try:
                    posts, total_pages = fetch_posts_page(scraper, cat_id, page, per_page)
                except Exception as e:
                    print(f"   ❌ Failed to fetch API page: {e}")
                    consecutive_err += 1
                    if consecutive_err >= 5:
                        print("   ❌ Too many errors — moving to next category.")
                        break
                    time.sleep(5)
                    continue

                consecutive_err = 0
                pages_crawled  += 1

                if not posts:
                    print("   ✅ No more posts — end of category.")
                    break

                print(f"   📋 Got {len(posts)} posts"
                      + (f"  (total pages: {total_pages})" if total_pages else ""))

                for post in posts:
                    post_link = post.get('link', '')
                    print(f"\n   🎬 {post_link}")

                    parsed = parse_post(post, default_is_series=default_serie)
                    if not parsed:
                        print(f"      ⚠️ Could not parse post — skipping")
                        continue

                    if not parsed['download_links']:
                        print(f"      ⛔ No download links — skipping '{parsed['title_raw']}'")
                        continue

                    title, title_b = clean_title_parts(parsed['title_raw'])
                    # Prefer the site's explicit latest-episode marker for series
                    if parsed['is_series'] and parsed['episode_meta']:
                        title_b = parsed['episode_meta']
                    print(f"      📝 Title: {title}")
                    if title_b:
                        print(f"      📝 Episode: {title_b}")

                    db_cats = detect_db_categories(parsed, parsed['is_series'])

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

                        # ── Assign categories (smart-detected) ──
                        assign_db_categories(movie, db_cats)

                        # ── Download link sync ─────────────────────────
                        existing = {normalize_url(dl.url): dl for dl in movie.download_links.all()}
                        current  = {normalize_url(dl['url']): dl for dl in parsed['download_links'] if is_valid_download_url(dl['url'])}
                        added    = 0

                        for norm, dl in current.items():
                            if norm not in existing:
                                DownloadLink.objects.create(movie=movie, label=dl['label'], url=dl['url'], source=SOURCE_NAME)
                                added += 1
                            else:
                                if existing[norm].label != dl['label']:
                                    existing[norm].label = dl['label']
                                    existing[norm].save()

                        # Only prune OUR OWN stale links — never delete links a
                        # different source added (keeps cross-source fallbacks).
                        for norm in set(existing) - set(current):
                            if _is_own_link(existing[norm].url):
                                existing[norm].delete()

                        total_posts_scraped += 1
                        status = "created" if created else ("updated" if updated else "unchanged")
                        print(f"      📋 {status} | links: {len(parsed['download_links'])} (+{added} new)")

                    except Exception as db_err:
                        print(f"      💥 DB error: {db_err}")
                        import traceback; traceback.print_exc()
                        connection.close()
                        continue

                # Stop if the API reports we've reached the last page
                if total_pages and page >= total_pages:
                    print(f"\n   ✅ Reached last API page ({total_pages}).")
                    break

                page += 1

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
        if cat_arg in CATEGORY_ALIASES:
            keys = CATEGORY_ALIASES[cat_arg]
            return [_KEY_TO_DEF[k] for k in keys if k in _KEY_TO_DEF]
        normalized = cat_arg.replace('-', '_')
        if normalized in _KEY_TO_DEF:
            return [_KEY_TO_DEF[normalized]]
        return []

    def _print_category_list(self):
        print("\n📋  Available --category aliases (naijaprey.tv)\n")
        print(f"  {'Alias':<12} {'Source category'}")
        print("  " + "─" * 40)
        for alias, keys in CATEGORY_ALIASES.items():
            labels = ', '.join(_KEY_TO_DEF[k]['label'] for k in keys if k in _KEY_TO_DEF)
            print(f"  {alias:<12} {labels}")
        print()
        print("  DB categories are detected per-post from Country / Genre.")
        print()


# # ── Basic usage ─────────────────────────────────────────────
# python manage.py scrape_naijaprey                       # scrape everything
# python manage.py scrape_naijaprey --list-categories     # print aliases and exit

# # ── Category aliases ────────────────────────────────────────
# python manage.py scrape_naijaprey --category movies
# python manage.py scrape_naijaprey --category series
# python manage.py scrape_naijaprey --category all        # default

# # ── Page control ────────────────────────────────────────────
# python manage.py scrape_naijaprey --startpage 5
# python manage.py scrape_naijaprey --startpage 1 --endpage 10
# python manage.py scrape_naijaprey --category movies --max-pages 3
# python manage.py scrape_naijaprey --per-page 50

# # ── Speed / social ──────────────────────────────────────────
# python manage.py scrape_naijaprey --no-social           # DB only
# python manage.py scrape_naijaprey --delay 1.0           # 1s between API pages
