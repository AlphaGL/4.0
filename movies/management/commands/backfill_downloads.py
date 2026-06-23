"""
Backfill download links for stream-only movies.

Some titles arrive via the streaming source (streamimdb) and therefore have a
``stream_url`` but no download links at all. This command finds those movies and
tries to attach downloads by doing a TARGETED search of the download sites, in
order:  9jarocks.net  →  thenkiri.com

For each stream-only movie we hit the site's ``?s=`` search, parse the best
title match with the existing scraper functions, and copy its download links
onto the SAME movie record (no new movie is created). Fuzzy title matching means
a few won't match — those are simply left for a later run / the normal crawl.

Safe to run repeatedly (idempotent): a movie that already has links is skipped,
and duplicate URLs are never added.

    python manage.py backfill_downloads                 # all stream-only movies
    python manage.py backfill_downloads --limit 200     # cap this run
    python manage.py backfill_downloads --dry-run       # report only
    python manage.py backfill_downloads --min-score 0.8 # stricter matching
"""
import difflib
import importlib
import re
from urllib.parse import quote_plus

from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from movies.models import Movie, DownloadLink

CANDIDATES_PER_SITE = 6

SITES = [
    ('scrape_9jarocks', 'https://9jarocks.net'),
    ('scrape_thenkiri', 'https://thenkiri.com'),
]


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


class Command(BaseCommand):
    help = "Attach download links to stream-only movies via targeted 9jarocks → thenkiri scrape."

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=None,
                            help='Max stream-only movies to process this run.')
        parser.add_argument('--min-score', type=float, default=0.72,
                            help='Title-similarity threshold for a match (default: 0.72).')
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help='Report what would happen; change nothing.')

    def handle(self, *args, **opts):
        dry = opts['dry_run']
        min_score = opts['min_score']

        # Stream-only = has an embeddable stream, but zero download links and no
        # legacy single download_url.
        qs = (Movie.objects
              .annotate(dlc=Count('download_links'))
              .filter(dlc=0)
              .filter(stream_url__isnull=False).exclude(stream_url='')
              .filter(Q(download_url__isnull=True) | Q(download_url=''))
              .order_by('id'))
        if opts['limit']:
            qs = qs[:opts['limit']]

        movies = list(qs)
        total = len(movies)
        self.stdout.write(f"Found {total} stream-only movie(s) needing downloads.")
        if not total:
            return

        mods = self._load_modules()
        if not mods:
            self.stderr.write("No usable scraper modules — aborting.")
            return

        filled = scanned = 0
        for mv in movies:
            scanned += 1
            links = self._search_links(mods, mv.title, min_score)
            if not links:
                continue
            added = self._attach(mv, links, dry)
            if added:
                filled += 1
                tag = '[dry-run] would add' if dry else 'added'
                self.stdout.write(self.style.SUCCESS(
                    f"  ✅ #{mv.id} {mv.title[:60]} — {tag} {added} link(s)"))

        self.stdout.write(self.style.SUCCESS(
            f"Done. Scanned {scanned}, filled {filled}."))

    # ── load 9jarocks / thenkiri modules that expose the needed functions ────
    def _load_modules(self):
        needed = ('_make_scraper', 'parse_post_page',
                  'get_post_urls_from_listing_page')
        out = []
        for mod_name, base in SITES:
            try:
                mod = importlib.import_module(
                    f'movies.management.commands.{mod_name}')
            except Exception as e:
                self.stderr.write(f"  skip {mod_name}: {e}")
                continue
            if any(not hasattr(mod, fn) for fn in needed):
                self.stderr.write(f"  skip {mod_name}: missing scraper functions")
                continue
            out.append((mod, base))
        return out

    # ── search a title across the sites, return best-match download links ────
    def _search_links(self, mods, title, min_score):
        for mod, base in mods:
            try:
                links = self._search_one(mod, base, title, min_score)
                if links:
                    return links
            except Exception as e:
                self.stderr.write(f"    {base} failed for '{title[:40]}': {e}")
        return None

    def _search_one(self, mod, base, title, min_score):
        scraper = mod._make_scraper()
        resp = scraper.get(f"{base}/?s={quote_plus(title)}", timeout=(8, 25))
        if resp.status_code != 200:
            return None
        urls = mod.get_post_urls_from_listing_page(resp.text, base)
        if not urls:
            return None

        best = None  # (score, parsed)
        for url in urls[:CANDIDATES_PER_SITE]:
            try:
                r = scraper.get(url, timeout=(8, 25))
                if r.status_code != 200:
                    continue
                parsed = mod.parse_post_page(r.text, url)
                if not parsed or not parsed.get('download_links'):
                    continue
                score = _similar(title, parsed.get('title', ''))
                if best is None or score > best[0]:
                    best = (score, parsed)
            except Exception:
                continue

        if best is None or best[0] < min_score:
            return None

        valid = getattr(mod, 'is_valid_download_url', lambda u: True)
        return [dl for dl in best[1]['download_links'] if valid(dl.get('url', ''))]

    # ── attach links to the movie, never duplicating a URL ───────────────────
    def _attach(self, movie, links, dry):
        def norm(u):
            return re.sub(r'/+$', '', (u or '').strip().lower())

        existing = {norm(dl.url) for dl in movie.download_links.all()}
        to_add = []
        for dl in links:
            u = dl.get('url', '')
            if u and norm(u) not in existing:
                to_add.append(dl)
                existing.add(norm(u))

        if not to_add or dry:
            return len(to_add)

        DownloadLink.objects.bulk_create([
            DownloadLink(movie=movie, label=dl.get('label', '')[:255], url=dl['url'])
            for dl in to_add
        ])
        # Populate the legacy single download_url if it's empty.
        if not movie.download_url:
            movie.download_url = to_add[0]['url'][:500]
            movie.save(update_fields=['download_url'])
        return len(to_add)
