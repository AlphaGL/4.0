"""
Flag catalogue titles that are ALSO streaming on Netflix / Amazon Prime Video,
using TMDB's watch/providers endpoint (JustWatch-sourced data). Region NG.

    python manage.py fetch_providers
    python manage.py fetch_providers --region NG --days 7 --workers 6
    python manage.py fetch_providers --all      # re-check every matched title

Only titles with a tmdb_id are checked (enrich_tmdb sets that). Availability
changes over time, so by default we refresh titles not checked in --days days.
Needs TMDB_API_KEY in .env.
"""
import concurrent.futures
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import connections
from django.db.models import Q
from django.utils import timezone

from movies.models import Movie
from movies import tmdb


class Command(BaseCommand):
    help = "Flag titles on Netflix / Prime from TMDB watch-providers (region NG)."

    def add_arguments(self, parser):
        parser.add_argument('--region', default='NG',
                            help="ISO country for availability (default NG).")
        parser.add_argument('--days', type=int, default=7,
                            help="Refresh titles not checked in this many days.")
        parser.add_argument('--all', action='store_true',
                            help="Re-check every title with a tmdb_id.")
        parser.add_argument('--limit', type=int, default=None)
        parser.add_argument('--workers', type=int, default=6)

    def handle(self, *args, **opts):
        if not tmdb.is_configured():
            self.stderr.write(self.style.ERROR("Set TMDB_API_KEY in your .env."))
            return

        region = opts['region']
        qs = Movie.objects.exclude(tmdb_id__isnull=True)
        if not opts['all']:
            stale = timezone.now() - timedelta(days=opts['days'])
            qs = qs.filter(
                Q(providers_checked_at__isnull=True) |
                Q(providers_checked_at__lt=stale))
        qs = qs.only('id', 'tmdb_id', 'is_series')
        if opts['limit']:
            qs = qs[:opts['limit']]
        movies = list(qs)
        total = len(movies)
        workers = max(1, opts['workers'])
        self.stdout.write(
            f"Checking {total} titles on {region} (workers={workers})...")

        net = prime = 0

        def work(m):
            nonlocal net, prime
            try:
                media = 'tv' if m.is_series else 'movie'
                on_net, on_prime = tmdb.watch_providers(m.tmdb_id, media, region)
                Movie.objects.filter(pk=m.id).update(
                    on_netflix=on_net, on_prime=on_prime,
                    providers_checked_at=timezone.now())
                if on_net:
                    net += 1
                if on_prime:
                    prime += 1
            except Exception as e:
                self.stderr.write(f"{m.id}: {e}")
            finally:
                connections.close_all()

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, movies))

        self.stdout.write(self.style.SUCCESS(
            f"Done. On Netflix: {net}, on Prime: {prime} (of {total} checked)."))
