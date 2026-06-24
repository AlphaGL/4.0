"""
Fetch each series' season → episode-count map from TMDB and store it on
Movie.tmdb_seasons as JSON (e.g. {"1": 10, "2": 8}).

This powers the episode selector: vidlink/streamimdb TV embeds need an explicit
/{season}/{episode}, so the player must know how many episodes each season has.

  python manage.py enrich_tv_seasons                 # series missing the map
  python manage.py enrich_tv_seasons --workers 8
  python manage.py enrich_tv_seasons --dry-run
  python manage.py enrich_tv_seasons --overwrite     # refresh all series

Needs TMDB_API_KEY in .env. TMDB is reachable from a normal network (unlike the
scraper sites), so this runs fine locally on each DB.
"""
import json
from concurrent.futures import ThreadPoolExecutor

import requests
from decouple import config
from django.core.management.base import BaseCommand
from django.db import connection

from movies.models import Movie

TMDB_TV = 'https://api.themoviedb.org/3/tv/{id}'


class Command(BaseCommand):
    help = "Store TMDB season→episode counts on series (Movie.tmdb_seasons)."

    def add_arguments(self, parser):
        parser.add_argument('--workers', type=int, default=6)
        parser.add_argument('--limit', type=int, default=None)
        parser.add_argument('--overwrite', action='store_true', default=False)
        parser.add_argument('--dry-run', action='store_true', default=False)

    def handle(self, *args, **opts):
        key = config('TMDB_API_KEY', default='')
        if not key:
            self.stderr.write(self.style.ERROR('Set TMDB_API_KEY in your .env.'))
            return

        qs = Movie.objects.filter(is_series=True, tmdb_id__isnull=False)
        if not opts['overwrite']:
            qs = qs.filter(tmdb_seasons='')
        qs = qs.order_by('id').values_list('id', 'tmdb_id')
        if opts['limit']:
            qs = qs[:opts['limit']]
        rows = list(qs)

        self.stdout.write(f"{len(rows)} series to enrich (overwrite={opts['overwrite']}).")
        if not rows:
            return

        dry = opts['dry_run']
        sess = requests.Session()

        def fetch(row):
            pk, tmdb = row
            try:
                r = sess.get(TMDB_TV.format(id=tmdb),
                             params={'api_key': key}, timeout=15)
                if r.status_code != 200:
                    return pk, None
                seasons = r.json().get('seasons') or []
                mp = {}
                for s in seasons:
                    sn = s.get('season_number')
                    ec = s.get('episode_count') or 0
                    if sn is not None and sn >= 1 and ec > 0:
                        mp[str(sn)] = ec
                return pk, (json.dumps(mp) if mp else None)
            except Exception:
                return pk, None

        done = enriched = 0
        with ThreadPoolExecutor(max_workers=opts['workers']) as ex:
            for pk, payload in ex.map(fetch, rows):
                done += 1
                if payload:
                    enriched += 1
                    if not dry:
                        Movie.objects.filter(pk=pk).update(tmdb_seasons=payload)
                if done % 500 == 0:
                    self.stdout.write(f"  …{done}/{len(rows)} ({enriched} enriched)")
                    connection.close()

        tag = '[dry-run] would enrich' if dry else 'Enriched'
        self.stdout.write(self.style.SUCCESS(
            f"{tag} {enriched}/{len(rows)} series with season maps."))
