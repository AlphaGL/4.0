"""
One-shot full-catalog backfill: runs both scrapers across every category with
no page limit, skips social posting (a 20k-title backfill would spam
Telegram/Twitter/Facebook with years-old "new" posts), then fixes ordering so
the homepage's "Latest" section reflects real recency instead of scrape
insertion order.

Why the ordering fix is needed: Movie.created_at defaults to the moment the
row is INSERTED, not the date the title was actually posted on 9jarocks/
thenkiri (neither site exposes a real post-date to scrape). Both scrapers
paginate newest-first within a category (page 1 = newest), so within a
single category the scrape order already matches true recency — but without
this fix, "Latest" would just show whatever got inserted last (e.g. the
final page of the final category), which is usually old content.

This command rewrites created_at for every row touched by this run to a
strictly decreasing synthetic timestamp in scrape order (first-scraped =
closest to now), so "Latest" shows real-newest-first within each source.
Limitation: neither site exposes real post dates, so there's no way to
perfectly interleave recency ACROSS the two sources — 9jarocks (scraped
first) ends up ranked as a block above thenkiri. Good enough to stop old
backfilled titles from showing as "latest", not a substitute for real dates.

Run once for the initial backfill:
    python manage.py scrape_full_catalog
Safe to re-run later for incremental catches — only rows inserted during
THIS run get re-dated, nothing already on the site is touched.
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone


class Command(BaseCommand):
    help = 'Full-catalog backfill: scrape every 9jarocks + thenkiri category, then fix Latest ordering.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--skip-9jarocks', action='store_true', default=False,
            help='Skip the 9jarocks pass (e.g. re-running after it already finished).',
        )
        parser.add_argument(
            '--skip-thenkiri', action='store_true', default=False,
            help='Skip the thenkiri pass.',
        )
        parser.add_argument(
            '--startpage', type=int, default=1,
            help=(
                'Category listing page to start from, per category (default: 1). '
                'E.g. --startpage 6 skips the newest 5 pages — useful for a historical '
                'backfill pass that leaves the freshest pages for a separate pass run '
                'closer to launch, so recency stays naturally correct.'
            ),
        )
        parser.add_argument(
            '--endpage', type=int, default=None,
            help=(
                'Stop after this listing page, per category (default: none — '
                'crawls to the end). E.g. --startpage 1 --endpage 5 scrapes just '
                'the newest 5 pages, for a follow-up pass after a --startpage 6 backfill.'
            ),
        )
        parser.add_argument(
            '--spacing-seconds', type=int, default=30,
            help='Synthetic gap between consecutive Latest-ordering timestamps (default: 30).',
        )

    def handle(self, *args, **options):
        run_started_at = timezone.now()
        self.stdout.write(f"🚀 Full catalog backfill started at {run_started_at.isoformat()}")
        page_kwargs = {'startpage': options['startpage'], 'endpage': options['endpage']}

        if not options['skip_9jarocks']:
            self.stdout.write(f"\n=== 9jarocks: scraping --category all (pages {options['startpage']}-{options['endpage'] or 'end'}) ===")
            call_command('scrape_9jarocks', category='all', no_social=True, **page_kwargs)
        else:
            self.stdout.write("\n(skipping 9jarocks pass)")

        if not options['skip_thenkiri']:
            self.stdout.write(f"\n=== thenkiri: scraping --category all (pages {options['startpage']}-{options['endpage'] or 'end'}) ===")
            call_command('scrape_thenkiri', category='all', no_social=True, **page_kwargs)
        else:
            self.stdout.write("\n(skipping thenkiri pass)")

        self._fix_latest_ordering(run_started_at, options['spacing_seconds'])
        self.stdout.write(self.style.SUCCESS("\n✅ Full catalog backfill complete."))

    def _fix_latest_ordering(self, run_started_at, spacing_seconds):
        """Re-date every row inserted during this run so scrape order (which
        matches real newest-first recency within each source) is reflected
        in created_at, instead of raw insertion time."""
        self.stdout.write("\n=== Fixing Latest ordering for rows scraped this run ===")
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH ordered AS (
                    SELECT id, ROW_NUMBER() OVER (ORDER BY id ASC) AS rn
                    FROM movies_movie
                    WHERE created_at >= %s
                )
                UPDATE movies_movie
                SET created_at = %s - (ordered.rn * %s * interval '1 second')
                FROM ordered
                WHERE movies_movie.id = ordered.id
                """,
                [run_started_at, run_started_at, spacing_seconds],
            )
            self.stdout.write(f"  re-dated {cursor.rowcount} rows")
