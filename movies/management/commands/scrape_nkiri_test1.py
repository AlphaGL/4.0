from django.core.management.base import BaseCommand
from django.utils import timezone
from movies.models import Movie, Category, DownloadLink
import requests
from bs4 import BeautifulSoup
import re
import cloudscraper
from urllib.parse import urlparse, unquote
import ssl
import urllib3
import time

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ══════════════════════════════════════════════════════════════
# PLATFORM LINKS  — update these to your real handles/links
# ══════════════════════════════════════════════════════════════

PLATFORM_LINKS = {
    'telegram': 'https://t.me/+oFCiWwxKmT5jNDM8',
    'twitter':  'https://x.com/Watch2D',
    'facebook': 'https://facebook.com/BigBrotherEntertainmentBlog',
    'website':  'https://watch2d.org',
}

# ── Cross-platform follow footers ─────────────────────────────
TELEGRAM_FOOTER = (
    "\n\n"
    "━━━━━━━━━━━━━━━━━━━\n"
    "🌐 <b>Follow us everywhere:</b>\n"
    f"🐦 X/Twitter → {PLATFORM_LINKS['twitter']}\n"
    f"📘 Facebook  → {PLATFORM_LINKS['facebook']}\n"
    f"🌍 Website   → {PLATFORM_LINKS['website']}\n"
    "━━━━━━━━━━━━━━━━━━━"
)

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
# Keeps Facebook & Twitter safe from spam detection.
# ══════════════════════════════════════════════════════════════

class _RateLimiter:
    def __init__(self):
        self._counts    = {'facebook': 0, 'twitter': 0}
        self._last_post = {'facebook': 0.0, 'twitter': 0.0}
        self._min_gap   = {'facebook': 45, 'twitter': 60}   # seconds between posts
        self._run_cap   = {'facebook': 80, 'twitter': 40}   # max posts per scraper run

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
# SHARED HASHTAG LOGIC  (viral-optimised)
# Returns (telegram_tags, twitter_tags, facebook_tags)
# Twitter gets max 5 tags — X algo penalises hashtag spam
# ══════════════════════════════════════════════════════════════

def _detect_hashtags(movie):
    title_lower = movie.title.lower()
    try:
        cat_names = ' '.join(c.name.lower() for c in movie.categories.all())
    except Exception:
        cat_names = ''
    combined = title_lower + ' ' + cat_names

    if any(kw in combined for kw in [
        'south africa', 'sa series', 'mzansi', 'inimba', 'ithonga',
        'pimville', 'generations', 'skeem', 'uzalo', 'isibaya',
        'rhythm city', 'scandal', 'gomora', 'diep city'
    ]):
        tg = (
            "#Watch2D #SASeries #SouthAfricanSeries #MzansiMagic #AfricanDrama "
            "#Mzansi #AfricanEntertainment #FreeDownload #HDDownload #NowStreaming "
            "#MustWatch #BingeWatch #SouthAfrica #AfricanTV #BlackExcellence "
            "#WatchFree #StreamFree #Trending #Entertainment"
        )
        tw = "#Watch2D #SASeries #MzansiMagic #AfricanDrama #FreeDownload"
        fb = tg

    elif any(kw in combined for kw in ['korean', 'kdrama', 'k-drama', 'korea']):
        tg = (
            "#Watch2D #KDrama #KoreanDrama #KoreanSeries #KDramaLover #KDramaAddict "
            "#AsianDrama #KoreanTV #FreeDownload #HDDownload #NowStreaming #MustWatch "
            "#BingeWatch #KoreanContent #Hallyu #KDramaEnglishSub #WatchFree #Trending"
        )
        tw = "#Watch2D #KDrama #KoreanDrama #AsianDrama #FreeDownload"
        fb = tg

    elif any(kw in combined for kw in ['nigerian', 'nollywood', 'naija', 'nigeria']):
        tg = (
            "#Watch2D #Nollywood #NigerianMovies #NaijaMovies #AfricanMovies "
            "#NollywoodSeries #FreeDownload #HDDownload #NowStreaming #MustWatch "
            "#BingeWatch #NigerianEntertainment #Naija #AfricanCinema #9jaMovies "
            "#NollywoodFinest #WatchFree #Trending #BlackExcellence"
        )
        tw = "#Watch2D #Nollywood #NaijaMovies #AfricanMovies #FreeDownload"
        fb = tg

    elif any(kw in combined for kw in ['turkish', 'turkey', 'dizi']):
        tg = (
            "#Watch2D #TurkishSeries #TurkishDrama #Dizi #TurkishTV #FreeDownload "
            "#HDDownload #NowStreaming #MustWatch #BingeWatch #TurkishContent "
            "#TurkDizi #EnglishSubtitles #WatchFree #StreamFree #Trending"
        )
        tw = "#Watch2D #TurkishDrama #Dizi #TurkishSeries #FreeDownload"
        fb = tg

    elif any(kw in combined for kw in ['indian', 'bollywood', 'hindi', 'telugu', 'tamil']):
        tg = (
            "#Watch2D #Bollywood #IndianSeries #HindiSeries #IndianDrama "
            "#TeluguMovies #TamilMovies #FreeDownload #HDDownload #NowStreaming "
            "#MustWatch #BingeWatch #IndianCinema #Tollywood #Kollywood "
            "#WatchFree #StreamFree #Trending #IndianEntertainment"
        )
        tw = "#Watch2D #Bollywood #IndianSeries #HindiSeries #FreeDownload"
        fb = tg

    elif any(kw in combined for kw in ['chinese', 'china', 'cdrama', 'c-drama']):
        tg = (
            "#Watch2D #CDrama #ChineseDrama #ChineseSeries #AsianDrama "
            "#ChineseTV #FreeDownload #HDDownload #NowStreaming #MustWatch "
            "#BingeWatch #Cdramaland #ChineseEntertainment #WatchFree #Trending"
        )
        tw = "#Watch2D #CDrama #ChineseDrama #AsianDrama #FreeDownload"
        fb = tg

    elif movie.is_series:
        tg = (
            "#Watch2D #NewSeries #TVSeries #Series #NowStreaming #FreeDownload "
            "#HDDownload #MustWatch #BingeWatch #SeriesAlert #Entertainment "
            "#WatchFree #StreamFree #NewRelease #Trending #NetflixAlternative "
            "#FreeMovies #OnlineTV #BingeAlert #WeekendWatch"
        )
        tw = "#Watch2D #TVSeries #NowStreaming #FreeDownload #BingeWatch"
        fb = tg

    else:
        tg = (
            "#Watch2D #NewMovie #Hollywood #FullMovie #FreeDownload #HDMovie "
            "#NowStreaming #MustWatch #MovieLovers #Cinema #Entertainment "
            "#WatchFree #StreamFree #NewRelease #Trending #NetflixAlternative "
            "#FreeMovies #MovieNight #FilmLovers #WeekendWatch"
        )
        tw = "#Watch2D #NewMovie #Hollywood #FreeDownload #MustWatch"
        fb = tg

    return tg, tw, fb


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

        url = f"{site_url}/movies/movie/{movie.pk}/"
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
                f"🔗 <a href='{url}'>▶️ Watch FREE on Watch2D</a>",
                "",
                tg_tags,
                TELEGRAM_FOOTER,
            ]

            from automation.models import TelegramPost
            TelegramPost.objects.get_or_create(
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
                f"🔗 <a href='{url}'>▶️ Watch FREE Now</a>",
                "",
                tg_tags,
                TELEGRAM_FOOTER,
            ]

            from automation.models import TelegramUpdate
            TelegramUpdate.objects.get_or_create(
                content_type='movie',
                content_id=movie.id,
                update_key=episode_label.strip(),
                defaults={'content_title': movie.title, 'success': True},
            )

        caption = "\n".join(lines)
        if movie.image_url:
            send_photo(channel, movie.image_url, caption)
        else:
            send_message(channel, caption)

        print(f"📢 Telegram: {'NEW' if is_new else 'UPDATE'} posted — {movie.title}")

    except Exception as e:
        print(f"⚠️ Telegram post failed (non-critical): {e}")


# ══════════════════════════════════════════════════════════════
# TWITTER / X POSTER
# Algorithm tips applied:
#  - Short punchy hook as first line (drives clicks)
#  - Max 5 hashtags (X penalises spam tagging)
#  - Image always attached (images get 2-3x more impressions)
#  - Cross-platform footer trimmed if >280 chars
# ══════════════════════════════════════════════════════════════

def _post_movie_to_twitter(movie, is_new: bool):
    if not _limiter.can_post('twitter'):
        return

    try:
        from django.conf import settings
        import tweepy

        api_key       = getattr(settings, 'TWITTER_API_KEY', '')
        api_secret    = getattr(settings, 'TWITTER_API_SECRET', '')
        access_token  = getattr(settings, 'TWITTER_ACCESS_TOKEN', '')
        access_secret = getattr(settings, 'TWITTER_ACCESS_SECRET', '')

        if not all([api_key, api_secret, access_token, access_secret]):
            print("⚠️ Twitter credentials missing — skipping.")
            return

        site_url      = getattr(settings, 'SITE_URL', 'https://watch2d.org')
        url           = f"{site_url}/movies/movie/{movie.pk}/"
        _, tw_tags, _ = _detect_hashtags(movie)

        if is_new:
            emoji = "🎬" if not movie.is_series else "📺"
            cats  = movie.categories.all()
            genre = f"({', '.join(c.name for c in cats[:2])})" if cats else ""

            hook = (
                f"{emoji} {movie.title} {genre} is now FREE on Watch2D!"
                if not movie.is_series
                else f"{emoji} {movie.title} {genre} — watch every episode FREE!"
            )
            tweet_text = f"{hook}\n\n▶️ {url}\n\n{tw_tags}{TWITTER_FOOTER}"

        else:
            episode_label = movie.title_b or "New Episode"
            tweet_text = (
                f"🆕 {movie.title}\n"
                f"New: {episode_label}\n\n"
                f"▶️ Watch FREE → {url}\n\n"
                f"{tw_tags}{TWITTER_FOOTER}"
            )

        # Trim to 280 — drop footer first, then hard cut
        if len(tweet_text) > 280:
            tweet_text = tweet_text.replace(TWITTER_FOOTER, '').strip()
        tweet_text = tweet_text[:280]

        # Upload image via v1, post via v2
        media_ids = None
        if movie.image_url:
            try:
                import tempfile, os
                auth   = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
                v1_api = tweepy.API(auth)

                img_data = requests.get(movie.image_url, timeout=10).content
                suffix   = '.jpg' if 'jpg' in movie.image_url.lower() else '.png'
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(img_data)
                    tmp_path = tmp.name

                media     = v1_api.media_upload(tmp_path)
                media_ids = [media.media_id]
                os.unlink(tmp_path)
            except Exception as img_err:
                print(f"⚠️ Twitter image upload failed (posting without image): {img_err}")
                media_ids = None

        v2_client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_secret,
        )

        if media_ids:
            v2_client.create_tweet(text=tweet_text, media_ids=media_ids)
        else:
            v2_client.create_tweet(text=tweet_text)

        _limiter.record('twitter')
        print(f"🐦 Twitter: {'NEW' if is_new else 'UPDATE'} posted — {movie.title}")

    except Exception as e:
        print(f"⚠️ Twitter post failed (non-critical): {e}")


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
            print("⚠️ Facebook credentials missing — skipping.")
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

            if movie.is_series:
                status = "✅ Completed" if movie.completed else "🔄 Ongoing Series"
                lines.append(f"📡 Status: {status}")

            lines += [
                "",
                f"▶️ Watch FREE on Watch2D: {url}",
                "",
                "💬 Tag a friend who needs to see this!",
                "👍 Like & Share to spread the word!",
                "",
                fb_tags,
                FACEBOOK_FOOTER,
            ]

        else:
            episode_label = movie.title_b or "New Episode"
            lines = [
                "🆕 New Episode Available!",
                "",
                f"📺 {movie.title}",
                f"🎬 Episode: {episode_label}",
                "",
                f"▶️ Watch FREE Now: {url}",
                "",
                "💬 Tag a friend who watches this series!",
                "👍 Like & Share so others don't miss out!",
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
        print(f"⚠️ Facebook post failed (non-critical): {e}")


# ══════════════════════════════════════════════════════════════
# MASTER POSTER
# ══════════════════════════════════════════════════════════════

def _post_to_all_platforms(movie, is_new: bool):
    _post_movie_to_telegram(movie, is_new=is_new)
    _post_movie_to_twitter(movie,  is_new=is_new)
    _post_movie_to_facebook(movie, is_new=is_new)


# ══════════════════════════════════════════════════════════════
# SCRAPER CONSTANTS
# ══════════════════════════════════════════════════════════════

API_URL = 'https://thenkiri.ng/wp-json/wp/v2/posts/'

KNOWN_DOWNLOAD_DOMAINS = [
    'dl.downloadwella.com.ng', 'archive.org', 'mega.nz', 'drive.google.com',
    'mediafire.com', 'pixeldrain.com', 'terabox.com', 'onedrive.live.com',
    'downloadwella.com', 'netnaijafiles.xyz', 'loadedfiles.org',
    'sabishares.com', 'meetdownload.com', 'webloaded.com.ng'
]

FILE_EXTENSIONS = ['.mp4', '.mkv', '.zip', '.rar', '.srt']


def normalize_url(url):
    parsed    = urlparse(url)
    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return unquote(clean_url).lower()


# ══════════════════════════════════════════════════════════════
# DOWNLOAD LINK EXTRACTION
# ══════════════════════════════════════════════════════════════

def extract_real_download_link(url):
    print(f"🔍 Extracting real link from: {url}")
    try:
        if 'downloadwella.com' in url:
            headers = {
                "User-Agent":                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer":                   "https://thenkiri.ng/",
                "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language":           "en-US,en;q=0.9",
                "Accept-Encoding":           "gzip, deflate, br",
                "Connection":                "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }

            try:
                scraper = cloudscraper.create_scraper()
                res     = scraper.get(url, headers=headers, timeout=15)
                res.raise_for_status()
            except requests.exceptions.SSLError:
                print("⚠️ SSL failed — retrying without SSL verification...")
                res = requests.get(url, headers=headers, timeout=15, verify=False)
                res.raise_for_status()

            soup = BeautifulSoup(res.text, 'html.parser')

            page_title = soup.find('title')
            if page_title:
                print(f"📄 Page title: {page_title.get_text()}")

            bdpg_button = soup.find('a', class_='bdpg-button')
            if bdpg_button and bdpg_button.get('href'):
                real_url = bdpg_button.get('href').split('?')[0]
                print(f"✅ Real link found (bdpg-button): {real_url}")
                return real_url

            for selector in [
                {'class_': 'bdpg-button'},
                {'id':     'download_link'},
                {'class_': 'download-btn'},
                {'class_': 'btn-download'},
                {'class_': 'download_button'},
                {'class_': 'button'},
                {'class_': 'btn'},
            ]:
                tag = soup.find('a', selector)
                if tag and tag.get('href'):
                    real_url = tag.get('href', '').split('?')[0]
                    print(f"✅ Real link found (selector {selector}): {real_url}")
                    return real_url

            all_links = soup.find_all('a', href=True)
            print(f"🔍 Found {len(all_links)} total links on page")

            for link in all_links:
                href = link.get('href', '')
                text = link.get_text().strip().lower()

                if 'downloadwella.com.ng' in href and any(ext in href for ext in ['.mkv', '.mp4', '.zip']):
                    print(f"🎯 Direct file link: {text} -> {href}")
                    return href.split('?')[0]

                if any(domain in href.lower() for domain in [
                    'mega.nz', 'mediafire.com', 'drive.google.com',
                    'archive.org', 'pixeldrain.com', 'terabox.com'
                ]):
                    print(f"🎯 External download link: {text} -> {href}")
                    return href.split('?')[0]

            for link in all_links:
                href           = link.get('href', '')
                text           = link.get_text().strip().lower()
                parent_classes = ' '.join(link.parent.get('class', [])) if link.parent else ''

                if 'download' in text or 'download' in parent_classes:
                    if href and href not in [
                        'https://downloadwella.com', 'https://downloadwella.com/',
                        'http://downloadwella.com',  'http://downloadwella.com/',
                    ]:
                        if href.count('/') > 3:
                            print(f"🎯 Download button link: {text} -> {href}")
                            return href.split('?')[0]

            print("⚠️ No download link found with any method.")

    except requests.exceptions.SSLError as ssl_err:
        print(f"⚠️ SSL Error (unrecoverable): {ssl_err}")
        return url

    except Exception as e:
        import traceback
        print(f"⚠️ Error extracting download link: {e}")
        print(f"🐛 Traceback: {traceback.format_exc()}")

    return url


def extract_real_download_link_with_retry(url, max_retries=3):
    for attempt in range(max_retries):
        if attempt > 0:
            print(f"🔄 Retry attempt {attempt + 1}/{max_retries}")
            time.sleep(2)
        result = extract_real_download_link(url)
        if result != url:
            return result
    print(f"❌ Failed to extract download link after {max_retries} attempts")
    return url


# ══════════════════════════════════════════════════════════════
# DATABASE HELPERS
# ══════════════════════════════════════════════════════════════

def get_base_title_for_matching(title):
    return re.sub(r'\s*\((complete|completed)\)\s*$', '', title, flags=re.IGNORECASE).strip()


def find_existing_movie(title, is_complete, max_retries=3):
    from django.db import connection

    base_title      = get_base_title_for_matching(title)
    search_variants = list(dict.fromkeys([
        title,
        base_title,
        f"{base_title} (Complete)",
        f"{base_title} (Completed)",
    ]))

    print(f"🔍 Searching for existing movie with variants: {search_variants}")

    for attempt in range(max_retries):
        try:
            movie = Movie.objects.filter(title__in=search_variants).first()
            if movie:
                print(f"✅ Found existing movie: '{movie.title}' (completed: {movie.completed})")
            else:
                print(f"❌ No existing movie found for: '{title}'")
            return movie

        except Exception as e:
            print(f"⚠️ Database error on attempt {attempt + 1}/{max_retries}: {e}")
            if attempt < max_retries - 1:
                connection.close()
                wait_time = 2 ** attempt
                print(f"🔄 Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                print(f"❌ Failed to query database after {max_retries} attempts")
                raise

    return None


# ══════════════════════════════════════════════════════════════
# DJANGO MANAGEMENT COMMAND
# ══════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = 'Scrape thenkiri.ng → save to DB → post to Telegram + Twitter + Facebook'

    def add_arguments(self, parser):
        parser.add_argument(
            '--startpage', type=int, default=1,
            help='Page to start from (default: 1)'
        )
        parser.add_argument(
            '--endpage', type=int, default=None,
            help='Page to stop at (optional)'
        )
        parser.add_argument(
            '--max-pages', type=int, default=None,
            help=(
                'Max pages this run. '
                'USE --max-pages 2 or 3 for your FIRST ever run '
                'to avoid hitting rate limits on bulk backfill. '
                'Twitter free = ~1500 tweets/month. '
                'Facebook safe = ~80 posts/day.'
            )
        )
        parser.add_argument(
            '--no-social', action='store_true', default=False,
            help='DB only — skip all social media. Use for bulk backfilling old content.'
        )
        parser.add_argument(
            '--telegram-only', action='store_true', default=False,
            help='Post to Telegram only — skips Twitter & Facebook rate limits.'
        )

    def clean_title_parts(self, title):
        title       = re.sub(r'\s+', ' ', title).strip()
        title_lower = title.lower()
        is_complete = 'complete' in title_lower or 'completed' in title_lower

        series_pattern = re.compile(r'(?i)(.*?\b(S\d{1,2}|Season\s?\d{1,2}))[\s\-–|:]*\s*(.*)')
        match          = series_pattern.match(title)
        if match:
            base_title = match.group(1).strip()
            title_b    = match.group(3).strip()
            if is_complete and '(complete' not in base_title.lower():
                base_title += ' (Completed)' if 'completed' in title_lower else ' (Complete)'
            return base_title, title_b

        movie_year_match = re.search(r'^(.*?\(\d{4}\))', title)
        if movie_year_match:
            return movie_year_match.group(1).strip(), ''

        return title, ''

    def handle(self, *args, **options):
        from django.db import connection

        start_page    = options['startpage']
        end_page      = options['endpage']
        max_pages     = options['max_pages']
        no_social     = options['no_social']
        telegram_only = options['telegram_only']

        page               = start_page
        pages_scraped      = 0
        consecutive_errors = 0
        max_consecutive_errors = 5

        print(f"🚀 Starting scrape from page {start_page}")
        if end_page:
            print(f"📄 Will stop at page {end_page}")
        if max_pages:
            print(f"📊 Will scrape maximum {max_pages} pages")
        if no_social:
            print("🔇 --no-social active: DB save only, no social posts")
        if telegram_only:
            print("📢 --telegram-only active: Telegram posts only")
        if not no_social and not telegram_only and not max_pages:
            print(
                "⚠️  TIP: First bulk run? Use --max-pages 3 or --no-social\n"
                "    Twitter limit: ~1500 tweets/month | Facebook safe: ~80/day"
            )

        while True:
            if end_page and page > end_page:
                print(f"✅ Reached end page {end_page}. Stopping.")
                break

            if max_pages and pages_scraped >= max_pages:
                print(f"✅ Scraped {max_pages} pages. Stopping.")
                break

            try:
                print(f"\n🌐 Fetching page {page}...")
                scraper = cloudscraper.create_scraper()
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
                    "Accept":     "application/json",
                }
                response = scraper.get(API_URL, params={'page': page}, headers=headers, timeout=10)
                response.raise_for_status()
                data = response.json()

            except requests.exceptions.HTTPError as http_err:
                if response.status_code == 404:
                    print("✅ All pages processed (404 received).")
                    break
                print(f"🔥 HTTP error: {http_err}")
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    print(f"❌ Too many consecutive errors. Stopping.")
                    return
                time.sleep(5)
                continue

            except Exception as e:
                print(f"🔥 Request failed: {e}")
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    print(f"❌ Too many consecutive errors. Stopping.")
                    return
                connection.close()
                time.sleep(5)
                continue

            consecutive_errors = 0
            pages_scraped     += 1

            if not data:
                print("✅ No data returned. Finished.")
                break

            for item in data:
                raw_title = item.get('title', {}).get('rendered', '').strip()
                if not raw_title:
                    print("⚠️ Skipped: empty title.")
                    continue

                print(f"\n🎬 Processing: {raw_title}")
                title, title_b = self.clean_title_parts(raw_title)
                is_complete    = bool(re.search(r'\bcomplete(d)?\b', raw_title, re.IGNORECASE))

                description = BeautifulSoup(
                    item.get('excerpt', {}).get('rendered', ''), 'html.parser'
                ).get_text()

                soup = BeautifulSoup(item.get('content', {}).get('rendered', ''), 'html.parser')

                video_url = ''
                iframe    = soup.find('iframe')
                if iframe and iframe.get('src'):
                    video_url = iframe['src']
                else:
                    video = soup.find('video')
                    if video:
                        src = video.find('source')
                        if src and src.get('src'):
                            video_url = src['src']
                if video_url:
                    print(f"🎥 Video URL: {video_url}")

                download_links = []
                print("🔗 Looking for download links...")
                for a in soup.find_all('a', href=True):
                    href       = a['href'].strip()
                    label      = ' '.join(a.stripped_strings).strip()
                    href_lower = href.lower()

                    if (any(domain in href_lower for domain in KNOWN_DOWNLOAD_DOMAINS)
                            or any(href_lower.endswith(ext) for ext in FILE_EXTENSIONS)
                            or 'dl' in href_lower
                            or 'dl' in label.lower()):
                        print(f"🔍 Found: {label} -> {href}")
                        real = extract_real_download_link(href)
                        download_links.append({'url': real, 'label': label})

                if not download_links:
                    print(f"⛔ No valid links for: {title}")
                    continue

                image_url = ''
                media_id  = item.get('featured_media')
                if media_id:
                    try:
                        img_res = scraper.get(
                            f"https://thenkiri.ng/wp-json/wp/v2/media/{media_id}",
                            headers=headers,
                        )
                        img_res.raise_for_status()
                        image_url = img_res.json().get('source_url', '')
                        print(f"🖼️ Image: {image_url}")
                    except Exception:
                        print("⚠️ Failed to get image")

                try:
                    movie   = find_existing_movie(title, is_complete)
                    created = False

                    if not movie:
                        movie = Movie.objects.create(
                            title=title,
                            title_b=title_b,
                            title_b_updated_at=timezone.now() if title_b else None,
                            description=description,
                            video_url=video_url,
                            download_url=download_links[0]['url'],
                            image_url=image_url,
                            completed=is_complete,
                            scraped=True,
                        )
                        created = True
                        print(f"✅ Created new movie: {title}")

                        if not no_social:
                            if telegram_only:
                                _post_movie_to_telegram(movie, is_new=True)
                            else:
                                _post_to_all_platforms(movie, is_new=True)

                    else:
                        updated = False
                        print(f"✏️ Updating existing movie: {movie.title}")

                        if movie.title != title:
                            print(f"📝 Title: '{movie.title}' → '{title}'")
                            movie.title = title
                            updated     = True

                        if title_b and movie.title_b != title_b:
                            movie.title_b            = title_b
                            movie.title_b_updated_at = timezone.now()
                            updated                  = True
                            if not no_social:
                                if telegram_only:
                                    _post_movie_to_telegram(movie, is_new=False)
                                else:
                                    _post_to_all_platforms(movie, is_new=False)

                        if not movie.video_url and video_url:
                            movie.video_url = video_url
                            updated         = True

                        if not movie.image_url and image_url:
                            movie.image_url = image_url
                            updated         = True

                        if movie.download_url and normalize_url(movie.download_url) != normalize_url(download_links[0]['url']):
                            print("🔁 Updating main download_url...")
                            movie.download_url = download_links[0]['url']
                            updated            = True

                        if movie.completed != is_complete:
                            print(f"🏁 Completion: {movie.completed} → {is_complete}")
                            movie.completed = is_complete
                            updated         = True

                        if updated:
                            movie.save()
                            print("🔄 Updated movie info.")

                    added, updated_labels, deleted = 0, 0, 0
                    existing_links = {normalize_url(dl.url): dl for dl in movie.download_links.all()}
                    current_links  = {normalize_url(dl['url']): dl for dl in download_links}

                    for norm_url, dl in current_links.items():
                        if norm_url in existing_links:
                            existing_dl = existing_links[norm_url]
                            if existing_dl.label != dl['label']:
                                existing_dl.label = dl['label']
                                existing_dl.save()
                                updated_labels += 1
                        else:
                            DownloadLink.objects.create(movie=movie, label=dl['label'], url=dl['url'])
                            added += 1

                    for norm_url in set(existing_links.keys()) - set(current_links.keys()):
                        existing_links[norm_url].delete()
                        deleted += 1

                    print(f"➕ {added} new link(s) added.")
                    if updated_labels:
                        print(f"✏️ {updated_labels} label(s) updated.")
                    if deleted:
                        print(f"🗑️ {deleted} outdated link(s) deleted.")

                    for cat_id in item.get('categories', []):
                        try:
                            r = scraper.get(
                                f"https://thenkiri.ng/wp-json/wp/v2/categories/{cat_id}",
                                headers=headers,
                            )
                            r.raise_for_status()
                            cat_name = r.json().get('name')
                            if cat_name:
                                cat_obj, _ = Category.objects.get_or_create(name=cat_name.capitalize())
                                movie.categories.add(cat_obj)
                                print(f"📁 Category added: {cat_name}")
                        except Exception:
                            print("⚠️ Category fetch failed.")

                    if not created and added == 0 and updated_labels == 0 and deleted == 0:
                        print("ℹ️ No updates.")

                except Exception as db_error:
                    print(f"💥 Database error processing '{title}': {db_error}")
                    print("🔄 Closing database connection and continuing...")
                    connection.close()
                    continue

            page += 1

        print(f"\n🎉 Scraping complete! Processed {pages_scraped} pages "
              f"(from page {start_page} to page {page - 1})")
        print(_limiter.stats())