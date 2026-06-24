"""
Give movies a stream by formatting an embed-provider URL from their tmdb_id.

No scraping — purely deterministic. Any movie that has a tmdb_id but no
stream_url gets one instantly (streamimdb by default). This opens the stream
gate for it in both the web app and the Flutter app (both read stream_url).

  python manage.py backfill_stream_links                 # movies, streamimdb, empty only
  python manage.py backfill_stream_links --dry-run       # report only
  python manage.py backfill_stream_links --provider vidsrc
  python manage.py backfill_stream_links --media both    # also series (S{n}E1 entry)
  python manage.py backfill_stream_links --overwrite     # replace existing stream_url too

Series note: TV embeds need a season/episode; we store the record's
season_number (default 1) at episode 1 as the landing point. Defaults to
movies-only because the web episode selector is provider-specific — flip with
--media tv|both when you're ready to cover series.
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from movies.models import Movie
from movies.stream_providers import build_stream_url, PROVIDERS


class Command(BaseCommand):
    help = "Backfill Movie.stream_url from tmdb_id using an embed provider."

    def add_arguments(self, parser):
        parser.add_argument('--provider', default='streamimdb', choices=list(PROVIDERS),
                            help='Embed provider to use (default: streamimdb).')
        parser.add_argument('--media', default='movie', choices=['movie', 'tv', 'both'],
                            help='Which titles to fill (default: movie).')
        parser.add_argument('--overwrite', action='store_true', default=False,
                            help='Replace an existing stream_url too (default: only fill empty).')
        parser.add_argument('--limit', type=int, default=None,
                            help='Max rows to update this run.')
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help='Report what would change; write nothing.')

    def handle(self, *args, **opts):
        provider = opts['provider']
        media    = opts['media']
        dry      = opts['dry_run']

        qs = Movie.objects.filter(tmdb_id__isnull=False)
        if media == 'movie':
            qs = qs.filter(is_series=False)
        elif media == 'tv':
            qs = qs.filter(is_series=True)
        if not opts['overwrite']:
            qs = qs.filter(Q(stream_url__isnull=True) | Q(stream_url=''))
        qs = qs.order_by('id')
        if opts['limit']:
            qs = qs[:opts['limit']]

        total = qs.count() if opts['limit'] is None else len(qs)
        self.stdout.write(f"{total} title(s) eligible (provider={provider}, media={media}, "
                          f"overwrite={opts['overwrite']}).")
        if not total:
            return

        updated = skipped = 0
        batch = []
        for mv in qs.iterator():
            url = build_stream_url(provider, mv.tmdb_id, is_series=mv.is_series,
                                   season=mv.season_number or 1, episode=1)
            if not url:
                skipped += 1
                continue
            if mv.stream_url == url:
                skipped += 1
                continue
            mv.stream_url = url[:600]
            batch.append(mv)
            updated += 1
            if not dry and len(batch) >= 500:
                Movie.objects.bulk_update(batch, ['stream_url'])
                batch.clear()

        if dry:
            self.stdout.write(self.style.WARNING(
                f"[dry-run] would set stream_url on {updated} title(s); {skipped} skipped."))
            return

        if batch:
            Movie.objects.bulk_update(batch, ['stream_url'])
        self.stdout.write(self.style.SUCCESS(
            f"Set stream_url on {updated} title(s) via {provider}. {skipped} skipped."))
