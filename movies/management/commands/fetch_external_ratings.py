"""
Backfill/refresh critic scores (IMDb + Rotten Tomatoes via OMDb, Letterboxd
via scrape) into ExternalRating rows. Run nightly via cron (see CRONJOBS in
settings.py); safe to re-run manually at any time.

    python manage.py fetch_external_ratings --limit 50
    python manage.py fetch_external_ratings --force   # refresh even recent rows

Needs OMDB_API_KEY in .env (free key from omdbapi.com) for IMDb/RT. Letterboxd
needs nothing but is scraped politely (1 request/sec, best-effort).
"""
import time

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from datetime import timedelta

from movies.models import Movie, ExternalRating
from movies import tmdb, external_ratings as ext

STALE_AFTER = timedelta(days=7)


class Command(BaseCommand):
    help = "Fetch/refresh IMDb, Rotten Tomatoes and Letterboxd ratings for movies."

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=None)
        parser.add_argument('--force', action='store_true',
                            help="Refresh even movies with recent ratings.")

    def handle(self, *args, **opts):
        cutoff = timezone.now() - STALE_AFTER

        qs = Movie.objects.filter(tmdb_id__isnull=False).exclude(title='')
        if not opts['force']:
            qs = qs.filter(
                Q(external_ratings__isnull=True) |
                Q(external_ratings__fetched_at__lt=cutoff)
            ).distinct()
        if opts['limit']:
            qs = qs[:opts['limit']]

        movies = list(qs)
        total = len(movies)
        self.stdout.write(f"{total} titles to check for external ratings...")

        updated = 0
        for i, movie in enumerate(movies, 1):
            # Backfill imdb_id lazily if we didn't have it yet.
            if not movie.imdb_id and movie.tmdb_id:
                media = 'tv' if movie.is_series else 'movie'
                imdb_id = tmdb.external_ids(movie.tmdb_id, media)
                if imdb_id:
                    Movie.objects.filter(pk=movie.pk).update(imdb_id=imdb_id)
                    movie.imdb_id = imdb_id

            omdb = ext.fetch_omdb(movie.imdb_id) if movie.imdb_id else {}
            if omdb.get('imdb'):
                ExternalRating.objects.update_or_create(
                    movie=movie, source='imdb',
                    defaults={
                        'score': omdb['imdb']['score'],
                        'display': omdb['imdb']['display'],
                        'url': omdb['imdb']['url'],
                    },
                )
                updated += 1
            if omdb.get('rt'):
                ExternalRating.objects.update_or_create(
                    movie=movie, source='rt',
                    defaults={
                        'score': omdb['rt']['score'],
                        'display': omdb['rt']['display'],
                        'url': omdb['rt']['url'],
                    },
                )
                updated += 1

            year = (movie.vi_year or '').strip()[:4] or None
            lb = ext.fetch_letterboxd(movie.title, year)
            if lb:
                ExternalRating.objects.update_or_create(
                    movie=movie, source='letterboxd',
                    defaults={
                        'score': lb['score'],
                        'display': lb['display'],
                        'url': lb['url'],
                    },
                )
                updated += 1

            if i % 25 == 0:
                self.stdout.write(f"  ...{i}/{total}")
            time.sleep(1)  # stay polite to Letterboxd/OMDb

        self.stdout.write(self.style.SUCCESS(
            f"Done. Checked {total} titles, wrote/updated {updated} rating rows."))
