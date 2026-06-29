"""
Management command: backfill_download_meta

Populates the multi-source FALLBACK metadata on DownloadLink rows so the app/site
can fail over per-episode and main-first:

  • priority        — lower = tried first. Original sources stay primary; known
                      secondary sources (e.g. naijaprey) are ranked as fallbacks.
  • season_number   — from the movie's season, or parsed from the link label.
  • episode_number  — parsed from the label ("Episode 3"), so the SAME episode
                      from different sources groups together for failover.

Source-agnostic + post-processing: it runs AFTER scraping (in the cleanse job),
so links from every scraper — 9jarocks, thenkiri, naijaprey — get organised
without touching any scraper's code.

Idempotent: by default only touches rows still at the default priority (100),
so re-runs are cheap no-ops. Use --all to recompute everything.

Usage
─────
  python manage.py backfill_download_meta
  python manage.py backfill_download_meta --all
"""
import re

from django.core.management.base import BaseCommand
from movies.models import DownloadLink

# Lower number = higher priority (tried first). Original primary scrapers and
# blank/legacy links stay the "main" link; secondary sources are fallbacks.
SOURCE_PRIORITY = {
    '': 10,            # legacy / original link (no source tag) = primary
    '9jarocks': 10,
    'thenkiri': 15,
    'nkiri': 15,
    'naijaprey': 30,   # supplementary fallback host
    'streamimdb': 40,
}
DEFAULT_PRIORITY = 20  # an unknown but tagged source

_EP_RE = re.compile(r'(?:episode|ep|e)\s*[._-]?\s*(\d{1,3})\b', re.IGNORECASE)
_SEASON_RE = re.compile(r'(?:season|s)\s*[._-]?\s*(\d{1,2})\b', re.IGNORECASE)
# A range/pack ("Episode 1-20", "1 to 10") or a "Complete" pack isn't a single
# episode, so it must NOT be tagged with one episode number.
_RANGE_RE = re.compile(r'\d+\s*(?:-|–|to)\s*\d+', re.IGNORECASE)


def parse_episode(label: str):
    l = (label or '').lower()
    if 'complete' in l or 'all episode' in l or _RANGE_RE.search(l):
        return None  # whole-season pack → groups at the movie level
    m = _EP_RE.search(l)
    return int(m.group(1)) if m else None


def parse_season(label: str):
    m = _SEASON_RE.search(label or '')
    return int(m.group(1)) if m else None


def priority_for(source: str) -> int:
    return SOURCE_PRIORITY.get((source or '').strip().lower(), DEFAULT_PRIORITY)


class Command(BaseCommand):
    help = "Backfill priority / season_number / episode_number on DownloadLinks."

    def add_arguments(self, parser):
        parser.add_argument(
            '--all', action='store_true',
            help='Recompute every link (default: only un-processed rows).')
        parser.add_argument(
            '--batch', type=int, default=500,
            help='Bulk-update batch size.')

    def handle(self, *args, **opts):
        qs = DownloadLink.objects.select_related('movie')
        if not opts['all']:
            # Default priority (100) marks a row we haven't organised yet.
            qs = qs.filter(priority=100)

        total = qs.count()
        if not total:
            self.stdout.write('Nothing to backfill — all links already organised.')
            return

        self.stdout.write(f'Backfilling {total} download link(s)…')
        updated = []
        changed = 0
        for link in qs.iterator(chunk_size=opts['batch']):
            movie = link.movie
            is_series = bool(getattr(movie, 'is_series', False))

            new_priority = priority_for(link.source)
            new_season = None
            new_episode = None
            if is_series:
                new_season = (getattr(movie, 'season_number', None)
                              or parse_season(link.label))
                new_episode = parse_episode(link.label)

            if (link.priority != new_priority
                    or link.season_number != new_season
                    or link.episode_number != new_episode):
                link.priority = new_priority
                link.season_number = new_season
                link.episode_number = new_episode
                updated.append(link)
                changed += 1

            if len(updated) >= opts['batch']:
                DownloadLink.objects.bulk_update(
                    updated, ['priority', 'season_number', 'episode_number'])
                updated.clear()

        if updated:
            DownloadLink.objects.bulk_update(
                updated, ['priority', 'season_number', 'episode_number'])

        self.stdout.write(self.style.SUCCESS(
            f'✅ Backfilled {changed} link(s).'))
