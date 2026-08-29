"""
Rate-limited backfill: archive DownloadLinks that predate the Telegram
File Storage pipeline (or were otherwise missed) — one small batch per run,
most-viewed movies first, so the titles people actually download get
covered soonest.

Deliberately NOT a "process everything now" command. With ~180k existing
links, blasting them all through one Telethon account in a burst is a real
risk of Telegram flagging the account for automated abuse — which would
break the whole delivery pipeline, not just slow it down. Instead this is
meant to run automatically every scheduled cycle (wired into the same
matrix as the scrapers in scrape_thenkiri.yml) and just chip away at the
backlog a little each time.

Run:  python manage.py backfill_telegram_uploads --limit 20 --delay 8
"""
import time

import requests
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        'Archive existing (not-yet-uploaded) DownloadLinks to the private '
        'Telegram channel, most-viewed movies first. Rate-limited per run.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=20,
            help='Max files to upload this run (default: 20).',
        )
        parser.add_argument(
            '--delay', type=float, default=8.0,
            help='Seconds to wait between uploads (default: 8).',
        )
        # Accepted (and ignored) purely so this command is compatible with
        # the shared "Run scraper" step in scrape_thenkiri.yml, which
        # appends one of these to every matrix.upload command — this
        # command has no social-posting step and always uploads, so
        # neither flag changes anything here.
        parser.add_argument('--no-social', action='store_true', default=False)
        parser.add_argument('--upload-files', action='store_true', default=False)

    def handle(self, *args, **options):
        from movies.management.commands._telegram_upload import upload_movie_file
        from movies.models import DownloadLink

        limit = options['limit']
        delay = options['delay']

        candidates = list(
            DownloadLink.objects
            .filter(telegram_message_id__isnull=True)
            .exclude(url='')
            .select_related('movie')
            .order_by('-movie__views')[:limit]
        )

        print("=" * 60)
        print(f"📦  Telegram archive backfill — {len(candidates)} this run "
              f"(limit {limit})")
        print("=" * 60)

        if not candidates:
            print("✅  Nothing left to backfill — caught up.")
            return

        session = requests.Session()
        archived = 0

        for i, dl in enumerate(candidates):
            views = getattr(dl.movie, 'views', 0)
            print(f"\n[{i+1}/{len(candidates)}] 🎬 {dl.movie.title}"
                  f"{f' — {dl.label}' if dl.label else ''} "
                  f"(views: {views})")

            try:
                msg_id = upload_movie_file(dl.movie, dl.url, session)
            except Exception as e:
                print(f"      💥 unexpected error: {e}")
                msg_id = None

            if msg_id:
                dl.telegram_message_id = msg_id
                dl.save(update_fields=['telegram_message_id'])
                archived += 1
            else:
                print("      ⛔ skipped/failed — will retry a future run")

            if i < len(candidates) - 1:
                time.sleep(delay)

        print("\n" + "=" * 60)
        print(f"📦  Backfill run complete: {archived}/{len(candidates)} archived.")
        remaining = (DownloadLink.objects
                     .filter(telegram_message_id__isnull=True)
                     .exclude(url='').count())
        print(f"    {remaining} still not archived — will continue next run.")
        print("=" * 60)
