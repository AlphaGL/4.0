"""
Fulfil user "Request a Title" submissions (app_title_request table, app DB).

For each pending request we:
  1. Check the catalogue for a match (the title may already be here, or have
     been added by the regular crawl) → fulfil + push the user. RELIABLE.
  2. If not found, attempt a TARGETED scrape, in the user-requested order:
       my9jarocks.bz  →  thenkiri.com
     We hit the site's search, pick the best title match, parse the post with
     the existing scraper functions, and ingest it. Streaming (streamimdb) is
     then attached by the normal streamimdb pipeline / enrich step.
  3. Re-check the catalogue → fulfil + push. If a request goes unfulfilled for
     too many runs, it's marked 'not_found'.

Gate to the app-DB account (DATA_ONLY=true) and provide FIREBASE_SERVICE_ACCOUNT
so the push can be sent. Targeted scraping hits live sites — wrapped defensively
so a failure just leaves the request pending for the next run / normal crawl.

    python manage.py process_requests
    python manage.py process_requests --no-scrape   # only fulfil from catalogue
"""
import difflib
import importlib
import json
import os
import re
from urllib.parse import quote_plus

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

from movies.models import Movie, DownloadLink

MAX_ATTEMPTS = 8          # ~4 days at 2 runs/day before giving up
CANDIDATES_PER_SITE = 6   # how many search hits to inspect per source

SITES = [
    ('scrape_9jarocks', 'https://my9jarocks.bz'),
    ('scrape_thenkiri', 'https://thenkiri.com'),
]


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


class Command(BaseCommand):
    help = "Fulfil 'Request a Title' submissions: match/scrape + push the user."

    def add_arguments(self, parser):
        parser.add_argument('--no-scrape', action='store_true', default=False,
                            help="Only fulfil from the existing catalogue.")
        parser.add_argument('--limit', type=int, default=50,
                            help="Max pending requests to process this run.")

    def handle(self, *args, **opts):
        cur = connection.cursor()
        try:
            cur.execute(
                "SELECT id, user_id, title, year, media_type, attempts "
                "FROM app_title_request "
                "WHERE status IN ('pending','searching') "
                "ORDER BY created_at ASC LIMIT %s", [opts['limit']])
            rows = cur.fetchall()
        except Exception as e:
            self.stderr.write(f"Can't read app_title_request: {e}")
            return

        if not rows:
            self.stdout.write("No pending title requests.")
            return

        fcm = self._fcm()
        fulfilled = scraped = gaveup = 0

        for rid, uid, title, year, media_type, attempts in rows:
            movie = self._catalogue_match(title, year)

            if movie is None and not opts['no_scrape']:
                movie = self._targeted_scrape(title, year, media_type)
                if movie is not None:
                    scraped += 1

            if movie is not None:
                self._mark(cur, rid, 'added', movie.id, attempts + 1)
                if fcm:
                    self._push(fcm, str(uid), movie)
                fulfilled += 1
                self.stdout.write(self.style.SUCCESS(
                    f"  ✅ '{title}' → #{movie.id} {movie.title}"))
                continue

            # Still nothing — bump attempts, retire if we've tried enough.
            new_attempts = attempts + 1
            if new_attempts >= MAX_ATTEMPTS:
                self._mark(cur, rid, 'not_found', None, new_attempts)
                gaveup += 1
            else:
                self._mark(cur, rid, 'searching', None, new_attempts)

        self.stdout.write(self.style.SUCCESS(
            f"Requests: {fulfilled} fulfilled ({scraped} freshly scraped), "
            f"{gaveup} marked not-found."))

    # ── Catalogue match ─────────────────────────────────────────────────────
    def _catalogue_match(self, title, year):
        qs = Movie.objects.filter(title__icontains=title.strip()[:60])
        if year:
            qs = qs.filter(vi_year__icontains=year) | qs.filter(
                title__icontains=year)
        best, best_score = None, 0.0
        for m in qs[:25]:
            score = max(_similar(title, m.title),
                        _similar(title, m.title_b or ''))
            if score > best_score:
                best, best_score = m, score
        return best if best_score >= 0.72 else None

    # ── Targeted scrape: 9jarocks → thenkiri ────────────────────────────────
    def _targeted_scrape(self, title, year, media_type):
        for mod_name, base in SITES:
            try:
                mod = importlib.import_module(
                    f'movies.management.commands.{mod_name}')
            except Exception:
                continue
            needed = ('_make_scraper', 'parse_post_page',
                      'get_post_urls_from_listing_page')
            if any(not hasattr(mod, fn) for fn in needed):
                continue
            try:
                movie = self._scrape_from(mod, base, title)
                if movie is not None:
                    self.stdout.write(f"    🔎 found on {base}")
                    return movie
            except Exception as e:
                self.stderr.write(f"    {base} attempt failed: {e}")
        return None

    def _scrape_from(self, mod, base, title):
        scraper = mod._make_scraper()
        search_url = f"{base}/?s={quote_plus(title)}"
        resp = scraper.get(search_url, timeout=(8, 25))
        if resp.status_code != 200:
            return None
        urls = mod.get_post_urls_from_listing_page(resp.text, base)
        if not urls:
            return None

        best = None  # (score, parsed, url)
        for url in urls[:CANDIDATES_PER_SITE]:
            try:
                r = scraper.get(url, timeout=(8, 25))
                if r.status_code != 200:
                    continue
                parsed = mod.parse_post_page(r.text, url)
                if not parsed or not parsed.get('download_links'):
                    continue
                score = _similar(title, parsed['title'])
                if best is None or score > best[0]:
                    best = (score, parsed, url)
            except Exception:
                continue

        if best is None or best[0] < 0.72:
            return None
        return self._ingest(mod, best[1])

    def _ingest(self, mod, parsed):
        """Create/update a Movie from a parsed post, mirroring the scraper."""
        title = parsed['title']
        movie = None
        if hasattr(mod, 'find_existing_movie'):
            movie = mod.find_existing_movie(title)
        if movie is None:
            movie = Movie.objects.create(
                title=title[:200],
                title_b=(parsed.get('title_b') or '')[:200] or None,
                title_b_updated_at=timezone.now() if parsed.get('title_b') else None,
                description=parsed.get('description', ''),
                video_url=(parsed.get('video_url') or '')[:500],
                download_url=parsed['download_links'][0]['url'][:500],
                image_url=(parsed.get('image_url') or '')[:500],
                completed=parsed.get('is_complete', False),
                is_series=parsed.get('is_series', False),
                scraped=True,
                vi_year=parsed.get('vi_year', '')[:10],
                vi_country=parsed.get('vi_country', '')[:120],
                vi_language=parsed.get('vi_language', '')[:120],
                vi_subtitle=parsed.get('vi_subtitle', '')[:60],
                vi_genre=parsed.get('vi_genre', '')[:200],
                vi_cast=parsed.get('vi_cast', ''),
                vi_episodes=parsed.get('vi_episodes', '')[:20],
                vi_status=parsed.get('vi_status', '')[:60],
                vi_runtime=parsed.get('vi_runtime', '')[:30],
                vi_filesize=parsed.get('vi_filesize', '')[:30],
            )

        if hasattr(mod, 'assign_db_categories'):
            try:
                mod.assign_db_categories(
                    movie, scraped_cats=parsed.get('categories', []),
                    forced_db_cats=[])
            except Exception:
                pass

        # Sync download links (reuse the scraper's url helpers if present).
        norm = getattr(mod, 'normalize_url', lambda u: u)
        valid = getattr(mod, 'is_valid_download_url', lambda u: True)
        existing = {norm(dl.url) for dl in movie.download_links.all()}
        for dl in parsed['download_links']:
            if valid(dl['url']) and norm(dl['url']) not in existing:
                DownloadLink.objects.create(
                    movie=movie, label=dl.get('label', ''), url=dl['url'])
        return movie

    # ── Notify ──────────────────────────────────────────────────────────────
    def _fcm(self):
        sa = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
        if not sa:
            self.stderr.write("No FIREBASE_SERVICE_ACCOUNT — fulfilment will "
                              "not push (still updates the request).")
            return None
        try:
            import firebase_admin
            from firebase_admin import credentials, messaging
            if not firebase_admin._apps:
                firebase_admin.initialize_app(
                    credentials.Certificate(json.loads(sa)))
            return messaging
        except Exception as e:
            self.stderr.write(f"FCM init failed: {e}")
            return None

    def _push(self, messaging, uid, movie):
        cur = connection.cursor()
        try:
            cur.execute(
                "SELECT token FROM app_device WHERE user_id = %s::uuid", [uid])
            tokens = list({r[0] for r in cur.fetchall() if r[0]})
        except Exception:
            return
        if not tokens:
            return
        data = {
            'type': 'new_arrival',
            'movie_id': str(movie.id),
            'title': movie.title,
            'image': movie.image_url or '',
            'slug': movie.slug or '',
        }
        try:
            messaging.send_each_for_multicast(
                messaging.MulticastMessage(
                    tokens=tokens, data=data,
                    android=messaging.AndroidConfig(priority='high')))
        except Exception as e:
            self.stderr.write(f"push failed: {e}")

    def _mark(self, cur, rid, status, movie_id, attempts):
        try:
            cur.execute(
                "UPDATE app_title_request "
                "SET status=%s, movie_id=%s, attempts=%s, updated_at=now() "
                "WHERE id=%s", [status, movie_id, attempts, rid])
        except Exception as e:
            self.stderr.write(f"update failed for request {rid}: {e}")
