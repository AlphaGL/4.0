"""
Management command: merge_stream_dupes
======================================
One-time (re-runnable) cleanup for duplicate stream records created before the
streamimdb/moviebox scrapers learned your per-season storage convention.

A "stray" = a record that carries a stream_url but has NO downloads
(download_links == 0 and no download_url). These were created by the streaming
scrapers when they should have ENRICHED an existing download record instead.

For each stray we look for the real records of the same show and, when found:
  • SERIES → copy the stream onto EVERY season-record of that show, then delete
    the stray (the whole-show player covers all seasons).
  • MOVIE  → copy the stream onto the matching record, but ONLY when the year is
    compatible (same year, or one side has no year). Year mismatch (e.g.
    "Beast (2026)" vs "Beast (2022)") = different films → left untouched.

A stray with no real (download-bearing) target is left alone (it's a legit
stream-only title, e.g. a show you only have on streamimdb).

Safe by default: prints a full plan and changes NOTHING unless you pass
--execute. Runs the mutation in a single transaction.

Usage
─────
python manage.py merge_stream_dupes              # dry run (shows the plan)
python manage.py merge_stream_dupes --execute    # apply it
"""

import re

from django.core.management.base import BaseCommand
from django.db import transaction

from movies.models import Movie
from .scrape_streamimdb import show_key


def _prefix(title: str) -> str:
    """A short title prefix for narrowing the candidate query."""
    p = re.split(r'\s+(?:S(?:eason)?\s*\d|\(|–|\||[-:])', title)[0]
    return p.strip()[:25]


class Command(BaseCommand):
    help = 'Merge stray stream-only records into the real download records of the same show.'

    def add_arguments(self, parser):
        parser.add_argument('--execute', action='store_true', default=False,
                            help='Apply the changes (default: dry run — show plan only).')

    def handle(self, *args, **options):
        execute = options['execute']

        strays = [m for m in Movie.objects.filter(stream_url__isnull=False)
                  if m.download_links.count() == 0 and not m.download_url]

        print('=' * 64)
        print(f'  Stray stream-only records to examine: {len(strays)}')
        print(f'  Mode: {"EXECUTE" if execute else "DRY RUN (no changes)"}')
        print('=' * 64)

        plan = []          # (stray, [targets])
        skipped_year = []  # (stray, candidate) year-mismatch, left alone
        left_alone = 0

        for s in strays:
            key = show_key(s.title)
            if not key:
                left_alone += 1
                continue

            cands = Movie.objects.filter(is_series=s.is_series,
                                         title__istartswith=_prefix(s.title)).exclude(pk=s.pk)
            targets, year_block = [], None
            for c in cands:
                if show_key(c.title) != key:
                    continue
                has_dl = c.download_links.count() > 0 or bool(c.download_url)
                if not has_dl:
                    continue
                if not s.is_series and s.vi_year and c.vi_year and s.vi_year != c.vi_year:
                    year_block = c          # same name, different year → different film
                    continue
                targets.append(c)

            if targets:
                plan.append((s, targets))
            else:
                if year_block is not None:
                    skipped_year.append((s, year_block))
                left_alone += 1

        # ── Report ──────────────────────────────────────────────────────────
        n_del = len(plan)
        n_enrich = sum(len(t) for _, t in plan)
        print(f'\n  Strays to MERGE + delete : {n_del}')
        print(f'  Download records gaining stream: {n_enrich}')
        print(f'  Left alone (no download twin)  : {left_alone}')
        print(f'  Skipped (year mismatch)        : {len(skipped_year)}')

        print('\n  ── MERGE PLAN ──')
        for s, targets in plan:
            kind = 'TV' if s.is_series else 'MOVIE'
            print(f'   [{kind}] delete stray {s.title!r} (pk{s.pk}) → stream onto:')
            for t in targets:
                print(f'        • {t.title!r} (pk{t.pk}, dl={t.download_links.count()})')

        if skipped_year:
            print('\n  ── SKIPPED (year mismatch — kept as distinct films) ──')
            for s, c in skipped_year[:30]:
                print(f'   keep {s.title!r} (pk{s.pk}) ≠ {c.title!r} (pk{c.pk})')

        if not execute:
            print('\n  DRY RUN — nothing changed. Re-run with --execute to apply.')
            return

        # ── Execute ─────────────────────────────────────────────────────────
        enriched = deleted = 0
        with transaction.atomic():
            for s, targets in plan:
                for t in targets:
                    changed = False
                    if not t.stream_url:
                        t.stream_url = s.stream_url[:600]
                        changed = True
                    if not t.image_url and s.image_url:
                        t.image_url = s.image_url
                        changed = True
                    if not t.description and s.description:
                        t.description = s.description
                        changed = True
                    if changed:
                        t.save()
                        enriched += 1
                s.delete()
                deleted += 1

        print(f'\n  ✅ Done. Enriched {enriched} record(s), deleted {deleted} stray(s).')
