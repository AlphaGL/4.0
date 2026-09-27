"""
Targeted backfill for movies missing a trailer and/or rating — as opposed to
`enrich_tmdb`, which by default skips anything already marked tmdb_synced=True
(so a title that matched TMDB but had no trailer *at the time* stays trailer-
less forever, and a title that failed to match stays unmatched forever).

Two groups, handled differently:
  1. Has tmdb_id, missing trailer_url — just re-fetch details() (1 API call,
     no search ambiguity). TMDB adds trailers to titles over time.
  2. No tmdb_id at all — re-run the full search()+details() flow, since the
     first attempt found no match (often fixable — better title parsing,
     TMDB adding the title since, etc.).

    python manage.py backfill_missing_media --limit 50 --verbose
    python manage.py backfill_missing_media --workers 8
"""
import concurrent.futures

from django.core.management.base import BaseCommand
from django.db import connections
from decouple import config

from movies.models import Movie
from movies import tmdb
from movies.genres import link_tmdb_genres
from movies.r2 import rehost_image, is_configured as r2_ready
from movies.management.commands.enrich_tmdb import _sync_cast, _sync_crew


class Command(BaseCommand):
    help = "Backfill trailer/rating for movies TMDB enrichment missed the first time."

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=None,
                            help="Cap total titles processed (across both groups).")
        parser.add_argument('--workers', type=int, default=6)
        parser.add_argument('--verbose', action='store_true')

    def handle(self, *args, **opts):
        if not tmdb.is_configured():
            self.stderr.write(self.style.ERROR("Set TMDB_API_KEY in your .env."))
            return

        public = config('R2_PUBLIC_URL', default='').rstrip('/')
        r2 = r2_ready()
        workers = max(1, opts['workers'])
        verbose = opts['verbose']
        limit = opts['limit']

        # ── Group 1: has tmdb_id, missing trailer_url — re-check only ──────────
        recheck_qs = (
            Movie.objects
            .filter(tmdb_id__isnull=False, trailer_url__isnull=True)
            .only('id', 'title', 'is_series', 'tmdb_id')
        )
        if limit:
            recheck_qs = recheck_qs[:limit]
        recheck = list(recheck_qs)

        def recheck_one(m):
            try:
                media = 'tv' if m.is_series else 'movie'
                d = tmdb.details(m.tmdb_id, media)
                if not d:
                    return (m, False)
                updates = {}
                if d.get('trailer_url'):
                    updates['trailer_url'] = d['trailer_url']
                if d.get('rating') is not None:
                    updates['rating'] = d['rating']
                if d.get('imdb_id'):
                    updates['imdb_id'] = d['imdb_id']
                if not updates:
                    return (m, False)
                Movie.objects.filter(pk=m.id).update(**updates)
                return (m, True)
            finally:
                connections.close_all()

        self.stdout.write(f"Group 1 (has tmdb_id, no trailer): {len(recheck)} titles...")
        done1 = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for i, (m, ok) in enumerate(ex.map(recheck_one, recheck), 1):
                if ok:
                    done1 += 1
                elif verbose:
                    self.stdout.write(f"  still no trailer: {m.title[:60]}")
                if i % 200 == 0:
                    self.stdout.write(f"  ...{i}/{len(recheck)}")
        self.stdout.write(self.style.SUCCESS(f"Group 1 done: {done1}/{len(recheck)} updated."))

        remaining_limit = None if limit is None else max(0, limit - len(recheck))
        if limit is not None and remaining_limit == 0:
            return

        # ── Group 2: no tmdb_id at all — re-run search()+details() ─────────────
        search_qs = (
            Movie.objects
            .filter(tmdb_id__isnull=True)
            .only('id', 'title', 'is_series', 'vi_year', 'vi_cast',
                  'vi_genre', 'vi_runtime', 'description', 'image_url')
        )
        if remaining_limit:
            search_qs = search_qs[:remaining_limit]
        to_search = list(search_qs)

        def search_one(m):
            try:
                year = (m.vi_year or '').strip() or None
                match = tmdb.search(m.title, year, m.is_series)
                if not match:
                    return (m, False)
                tid, media = match
                d = tmdb.details(tid, media)
                if not d:
                    return (m, False)

                updates = {'tmdb_synced': True, 'tmdb_id': tid}
                if d['rating'] is not None:
                    updates['rating'] = d['rating']
                if d.get('imdb_id'):
                    updates['imdb_id'] = d['imdb_id']
                if d['trailer_url']:
                    updates['trailer_url'] = d['trailer_url']
                if not (m.vi_cast or '').strip() and d['cast']:
                    updates['vi_cast'] = d['cast']
                if not (m.description or '').strip() and d['overview']:
                    updates['description'] = d['overview']
                if not (m.vi_genre or '').strip() and d['genres']:
                    updates['vi_genre'] = d['genres'][:200]
                if not (m.vi_runtime or '').strip() and d['runtime']:
                    updates['vi_runtime'] = d['runtime'][:30]

                cur = m.image_url or ''
                already_ok = public and cur.startswith(public)
                if d['poster_url'] and r2 and not already_ok:
                    new_img = rehost_image(d['poster_url'])
                    if new_img:
                        updates['image_url'] = new_img

                updates['genres_synced'] = True
                Movie.objects.filter(pk=m.id).update(**updates)
                _sync_cast(m.id, d.get('cast_list'), r2)
                _sync_crew(m.id, d.get('crew_list'), r2)
                if d['genres']:
                    link_tmdb_genres(m, d['genres'])
                return (m, True)
            finally:
                connections.close_all()

        self.stdout.write(f"Group 2 (no tmdb_id): {len(to_search)} titles...")
        done2 = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(search_one, m) for m in to_search]
            for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                try:
                    m, ok = fut.result()
                except Exception:
                    m, ok = None, False
                if ok:
                    done2 += 1
                elif verbose and m is not None:
                    self.stdout.write(f"  still no match: {m.title[:60]}")
                if i % 200 == 0:
                    self.stdout.write(f"  ...{i}/{len(to_search)}")
        self.stdout.write(self.style.SUCCESS(f"Group 2 done: {done2}/{len(to_search)} matched."))
