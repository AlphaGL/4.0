"""
backfill_genres — link TMDB's canonical genres to existing movies as browsable
Categories, using each movie's stored tmdb_id (no re-search needed).

  python manage.py backfill_genres                 # all un-synced with a tmdb_id
  python manage.py backfill_genres --limit 500 --workers 6
  python manage.py backfill_genres --force         # re-tag even synced rows

Only touches movies that already have a tmdb_id. Idempotent + resumable (via the
`genres_synced` flag), so it's safe to run in the scrape workflow.
"""
import concurrent.futures

from django.core.management.base import BaseCommand
from django.db import connections

from movies.models import Movie
from movies import tmdb
from movies.genres import link_tmdb_genres


class Command(BaseCommand):
    help = "Link TMDB genres to movies as categories (from the stored tmdb_id)."

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0,
                            help='Only process the first N (0 = all).')
        parser.add_argument('--workers', type=int, default=6)
        parser.add_argument('--force', action='store_true', default=False,
                            help='Re-tag even rows already marked genres_synced.')

    def handle(self, *args, **opts):
        qs = Movie.objects.filter(tmdb_id__isnull=False)
        if not opts['force']:
            qs = qs.filter(genres_synced=False)
        qs = qs.only('id', 'tmdb_id', 'is_series')
        if opts['limit']:
            qs = qs[:opts['limit']]
        movies = list(qs)
        total = len(movies)
        workers = max(1, opts['workers'])
        self.stdout.write(f"{total} movies to genre-tag (workers={workers})…")
        if total == 0:
            return

        def work(m):
            try:
                media = 'tv' if m.is_series else 'movie'
                d = tmdb.details(m.tmdb_id, media)
                n = 0
                if d and d.get('genres'):
                    n = link_tmdb_genres(m, d['genres'])
                Movie.objects.filter(pk=m.id).update(genres_synced=True)
                return n
            except Exception:
                return -1
            finally:
                connections.close_all()

        done = tagged = failed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(work, m) for m in movies]
            for i, fut in enumerate(
                    concurrent.futures.as_completed(futures), 1):
                n = fut.result()
                if n < 0:
                    failed += 1
                else:
                    done += 1
                    tagged += n
                if i % 200 == 0:
                    self.stdout.write(
                        f"  …{done} done, {tagged} genre-links, "
                        f"{failed} failed ({i}/{total})")

        self.stdout.write(self.style.SUCCESS(
            f"Genre backfill complete: {done} movies, {tagged} genre-links, "
            f"{failed} failed."))
