"""
Backfill the new `backdrop_url` field for movies that already matched TMDB
(have a tmdb_id) before this field existed — the vast majority of the
catalogue. Re-fetches details() (1 API call per title, no search ambiguity)
and only pulls the backdrop_path out of the response.

    python manage.py backfill_backdrops --limit 50 --verbose
    python manage.py backfill_backdrops --workers 8
"""
import concurrent.futures

from django.core.management.base import BaseCommand
from django.db import connections
from django.db.models import Q

from movies.models import Movie
from movies import tmdb
from movies.r2 import rehost_image, is_configured as r2_ready


class Command(BaseCommand):
    help = "Backfill backdrop_url for movies that already have a tmdb_id."

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=None)
        parser.add_argument('--workers', type=int, default=6)
        parser.add_argument('--verbose', action='store_true')

    def handle(self, *args, **opts):
        if not tmdb.is_configured():
            self.stderr.write(self.style.ERROR("Set TMDB_API_KEY in your .env."))
            return

        r2 = r2_ready()
        workers = max(1, opts['workers'])
        verbose = opts['verbose']

        qs = (
            Movie.objects
            .filter(tmdb_id__isnull=False)
            .filter(Q(backdrop_url__isnull=True) | Q(backdrop_url=''))
            .only('id', 'title', 'is_series', 'tmdb_id')
        )
        if opts['limit']:
            qs = qs[:opts['limit']]
        movies = list(qs)
        total = len(movies)
        self.stdout.write(f"{total} titles missing a backdrop...")

        def work(m):
            try:
                media = 'tv' if m.is_series else 'movie'
                d = tmdb.details(m.tmdb_id, media)
                if not d or not d.get('backdrop_url'):
                    return (m, False)
                url = d['backdrop_url']
                if r2:
                    hosted = rehost_image(url)
                    if hosted:
                        url = hosted
                Movie.objects.filter(pk=m.id).update(backdrop_url=url)
                return (m, True)
            finally:
                connections.close_all()

        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(work, m) for m in movies]
            for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                try:
                    m, ok = fut.result()
                except Exception:
                    m, ok = None, False
                if ok:
                    done += 1
                elif verbose and m is not None:
                    self.stdout.write(f"  no backdrop: {m.title[:60]}")
                if i % 200 == 0:
                    self.stdout.write(f"  ...{i}/{total}")

        self.stdout.write(self.style.SUCCESS(f"Done. Backfilled backdrop for {done}/{total} titles."))
