"""
Management command: split_merged_seasons

Fixes a real data bug found across the DB: many multi-season shows were
scraped as one big "(Complete)" pack and every episode landed on whichever
season's Movie record the title-matching happened to attach to — so e.g.
"Attack on Titan Season 1" ends up holding Season 1 *and* Season 2/3/4
episodes too.

Detection is independent of the DownloadLink.season_number field, which
can't be trusted for this: backfill_download_meta.py sets it by copying
the PARENT MOVIE's season_number first, only falling back to parsing the
label if the movie has none — so a wrongly-attached link just inherits the
wrong season number and the mismatch is invisible if you only look at that
field. Instead this command parses the SxxExx pattern directly out of each
link's own url/label (the actual scraped filename, e.g.
"Attack.On.Titan.S02E01.540p...mkv"), which is ground truth independent of
which Movie row it happens to be attached to.

For each mismatched link (found season != the Movie's own season_number):
  • If a Movie already exists for (show_key, found_season)  → move the link there.
  • If no such Movie exists yet                              → create one,
    cloning metadata (image, description, genre, cast, country, language,
    categories) from the movie the link is currently misfiled under, since
    it's necessarily a sibling season of the same show.

Safe by default: prints a full dry-run summary and does not touch the DB
unless --apply is passed. Also idempotent — re-running after --apply finds
nothing left to fix.

Usage
─────
  python manage.py split_merged_seasons                # dry run (default)
  python manage.py split_merged_seasons --apply         # actually fix it
  python manage.py split_merged_seasons --apply --limit 20   # fix only the first 20 shows (testing)
"""
import re
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from movies.models import Movie, DownloadLink
from movies.scraper_utils import normalize_url, build_season_title

# Captures season + episode together straight from the scraped filename/label,
# e.g. "Attack.On.Titan.S02E01.540p..." or "EPISODE 1" won't match (no S/E
# pair) — those without a season marker in the text are left alone, since we
# have nothing to check them against. Same pattern the live scrapers now use
# at scrape time (movies.scraper_utils.route_and_sync_download_links) — this
# command is the one-off cleanup for everything scraped before that fix.
SXE_RE = re.compile(r'[Ss](\d{1,2})[Ee](\d{1,3})')


class Command(BaseCommand):
    help = (
        "Find episodes attached to the wrong season's Movie record (from "
        "multi-season packs scraped as one bundle) and split them onto the "
        "correct season, creating a new Movie for that season if needed. "
        "Dry-run by default — pass --apply to actually write changes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true', default=False,
            help='Actually write changes. Without this, only prints what would happen.',
        )
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Only process the first N affected shows (for testing before a full run).',
        )

    def handle(self, *args, **options):
        import sys
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass

        apply_changes = options['apply']
        limit = options['limit']

        self.stdout.write("=" * 70)
        self.stdout.write(f"🔍  Scanning for merged-season download links "
                           f"({'APPLYING CHANGES' if apply_changes else 'DRY RUN — no writes'})")
        self.stdout.write("=" * 70)

        # Not filtering on is_series here — a handful of genuine season
        # records (show_key + season_number both set, "(Complete)" titles,
        # real episode links) turned out to be mis-flagged is_series=False.
        # show_key + season_number both present is already a strong enough
        # signal of being a real season entry on its own.
        season_movies = list(
            Movie.objects
            .exclude(season_number__isnull=True)
            .exclude(show_key='')
            .prefetch_related('download_links', 'categories')
        )
        season_lookup = {(m.show_key, m.season_number): m for m in season_movies}

        # Group mismatches by (show_key, target_season) so every episode
        # destined for the same new/target season is handled together.
        groups = defaultdict(list)  # (show_key, target_season) -> [(source_movie, dl, found_ep)]

        for m in season_movies:
            for dl in m.download_links.all():
                text = dl.url or dl.label or ''
                match = SXE_RE.search(text)
                if not match:
                    continue
                found_season, found_ep = int(match.group(1)), int(match.group(2))
                if found_season == m.season_number:
                    continue
                groups[(m.show_key, found_season)].append((m, dl, found_ep))

        move_groups = {k: v for k, v in groups.items() if k in season_lookup}
        create_groups = {k: v for k, v in groups.items() if k not in season_lookup}

        total_move_links = sum(len(v) for v in move_groups.values())
        total_create_links = sum(len(v) for v in create_groups.values())

        self.stdout.write(f"\n📦 Shows affected: {len(groups)}")
        self.stdout.write(f"   → {len(move_groups)} target seasons already exist "
                           f"({total_move_links} links to move)")
        self.stdout.write(f"   → {len(create_groups)} target seasons need a new Movie created "
                           f"({total_create_links} links to move)")

        if limit:
            move_keys = list(move_groups.keys())[:limit]
            create_keys = list(create_groups.keys())[:limit]
            move_groups = {k: move_groups[k] for k in move_keys}
            create_groups = {k: create_groups[k] for k in create_keys}
            self.stdout.write(f"\n⚠️  --limit {limit}: only processing the first {limit} "
                               f"groups of each kind this run.")

        moved_count = 0
        skipped_dupe_count = 0
        created_count = 0

        # ── Bucket 1: target season movie already exists — just move links ──
        self.stdout.write(f"\n{'─' * 70}")
        self.stdout.write("BUCKET 1 — move to an existing season record")
        self.stdout.write(f"{'─' * 70}")

        for (show_key, target_season), items in move_groups.items():
            target = season_lookup[(show_key, target_season)]
            existing_urls = {normalize_url(l.url) for l in target.download_links.all()}
            self.stdout.write(
                f"\n  {show_key!r} → Season {target_season} "
                f"(existing record pk={target.pk}, {target.title!r}): {len(items)} link(s)"
            )
            for source_movie, dl, found_ep in items:
                norm = normalize_url(dl.url)
                if norm in existing_urls:
                    self.stdout.write(
                        f"      ⏭  DUPLICATE, would delete from pk={source_movie.pk}: {dl.url[:90]}"
                    )
                    skipped_dupe_count += 1
                    if apply_changes:
                        dl.delete()
                    continue
                self.stdout.write(
                    f"      ➡  move link {dl.pk} (S{target_season:02d}E{found_ep:02d}) "
                    f"from pk={source_movie.pk} → pk={target.pk}: {dl.url[:90]}"
                )
                moved_count += 1
                if apply_changes:
                    dl.movie = target
                    dl.season_number = target_season
                    dl.episode_number = found_ep
                    dl.save(update_fields=['movie', 'season_number', 'episode_number'])
                    existing_urls.add(norm)

        # ── Bucket 2: no target season movie yet — create one ───────────────
        self.stdout.write(f"\n{'─' * 70}")
        self.stdout.write("BUCKET 2 — create a new season record")
        self.stdout.write(f"{'─' * 70}")

        for (show_key, target_season), items in create_groups.items():
            template = items[0][0]  # any sibling season movie works as the metadata template
            new_title = build_season_title(template.title, target_season)

            if Movie.objects.filter(title=new_title).exists():
                self.stdout.write(
                    f"\n  ⚠️  '{show_key}' Season {target_season}: computed title "
                    f"{new_title!r} already exists on a different record — SKIPPING "
                    f"this group, needs manual review."
                )
                continue

            self.stdout.write(
                f"\n  {show_key!r} → Season {target_season} (NEW): "
                f"{len(items)} link(s), title={new_title!r}, "
                f"cloned from pk={template.pk} ({template.title!r})"
            )

            if not apply_changes:
                created_count += 1
                seen_urls = set()
                for source_movie, dl, found_ep in items:
                    norm = normalize_url(dl.url)
                    if norm in seen_urls:
                        self.stdout.write(
                            f"      ⏭  DUPLICATE within group, would delete: {dl.url[:90]}"
                        )
                        skipped_dupe_count += 1
                        continue
                    seen_urls.add(norm)
                    self.stdout.write(
                        f"      ➡  would move link {dl.pk} (S{target_season:02d}E{found_ep:02d}) "
                        f"from pk={source_movie.pk} → new record: {dl.url[:90]}"
                    )
                    moved_count += 1
                continue

            with transaction.atomic():
                new_movie = Movie.objects.create(
                    title=new_title,
                    is_series=True,
                    completed=template.completed,
                    show_key=show_key,
                    season_number=target_season,
                    description=template.description,
                    video_url='',
                    image_url=template.image_url,
                    scraped=True,
                    vi_country=template.vi_country,
                    vi_language=template.vi_language,
                    vi_cast=template.vi_cast,
                    vi_genre=template.vi_genre,
                    vi_year=template.vi_year,
                    vi_subtitle=template.vi_subtitle,
                )
                new_movie.categories.set(template.categories.all())
                created_count += 1

                seen_urls = set()
                for source_movie, dl, found_ep in items:
                    norm = normalize_url(dl.url)
                    if norm in seen_urls:
                        # Same episode scraped twice into the same wrong season —
                        # only keep one copy on the new record.
                        dl.delete()
                        skipped_dupe_count += 1
                        continue
                    seen_urls.add(norm)
                    dl.movie = new_movie
                    dl.season_number = target_season
                    dl.episode_number = found_ep
                    dl.save(update_fields=['movie', 'season_number', 'episode_number'])
                    moved_count += 1

        # ── Summary ──────────────────────────────────────────────────────────
        self.stdout.write(f"\n\n{'=' * 70}")
        self.stdout.write("🎉  Done!" if apply_changes else "🔎  Dry run complete — nothing was changed.")
        self.stdout.write(f"    New season records {'created' if apply_changes else 'that would be created'}: {created_count}")
        self.stdout.write(f"    Links {'moved' if apply_changes else 'that would be moved'}: {moved_count}")
        self.stdout.write(f"    Duplicate links {'deleted' if apply_changes else 'that would be deleted'}: {skipped_dupe_count}")
        if not apply_changes:
            self.stdout.write(f"\n    Re-run with --apply to actually make these changes.")
        self.stdout.write("=" * 70)
