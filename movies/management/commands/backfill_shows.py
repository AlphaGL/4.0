"""
Populate show_key + season_number on every Movie so the app can group all the
seasons of a show ("From S01", "From S02", …) under one parent.

Safe to re-run. Does NOT touch titles, slugs, downloads or anything else.

    python manage.py backfill_shows --dry-run    # preview only
    python manage.py backfill_shows              # apply
"""
from django.core.management.base import BaseCommand
from django.db.models import Count

from movies.models import Movie
from movies.scraper_utils import parse_show


class Command(BaseCommand):
    help = "Derive show_key + season_number for all movies (groups seasons)."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help="Show what would change without writing.")
        parser.add_argument('--only-missing', action='store_true',
                            help="Only rows with an empty show_key. Cheap to run "
                                 "every CI build (no-op once everything is filled).")

    def handle(self, *args, **opts):
        dry = opts['dry_run']
        # Note: no .iterator() — Supabase's pooler doesn't support server-side
        # cursors. Load once (only 4 small fields) and bulk_update in chunks.
        qs = Movie.objects.all()
        if opts['only_missing']:
            qs = qs.filter(show_key='')
        movies = list(qs.only('id', 'title', 'show_key', 'season_number'))
        total = len(movies)
        to_update = []

        for m in movies:
            key, season = parse_show(m.title)
            if m.show_key != key or m.season_number != season:
                m.show_key = key
                m.season_number = season
                to_update.append(m)

        if not dry and to_update:
            for i in range(0, len(to_update), 500):
                Movie.objects.bulk_update(
                    to_update[i:i + 500], ['show_key', 'season_number'])

        self.stdout.write(self.style.SUCCESS(
            f"{'[dry-run] ' if dry else ''}Backfilled {len(to_update)}/{total} movies."))

        # Report the shows that actually have more than one season/row.
        groups = (Movie.objects.exclude(show_key='')
                  .values('show_key')
                  .annotate(n=Count('id'))
                  .filter(n__gt=1)
                  .order_by('-n'))
        self.stdout.write(f"Shows with multiple rows: {groups.count()}")
        for g in groups[:20]:
            self.stdout.write(f"   {g['show_key']}: {g['n']} rows")
