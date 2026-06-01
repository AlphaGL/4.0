"""
Management command: reslug_movies
─────────────────────────────────
Regenerates the slug for every Movie in the database using the new
SEO-rich format:  <title-slug>-<content-type>-download

Examples of new slugs:
  rick-and-morty-s09-hollywood-tv-series-download
  filing-for-love-s01-korean-drama-download
  omukade-2025-hollywood-movie-download
  she-knows-my-secret-2026-nollywood-movie-download

Your existing old_movie_redirect view already handles the old slug URLs
with a 301 redirect, so all old links/bookmarks/Google rankings are safe.

Usage
─────
# Dry run — shows what would change, touches nothing:
python manage.py reslug_movies --dry-run

# Live run — updates all slugs:
python manage.py reslug_movies

# Live run on a single movie by ID (useful for testing):
python manage.py reslug_movies --movie-id 2502

# Limit to a batch (e.g. first 500 movies):
python manage.py reslug_movies --limit 500

Notes
─────
• Runs in batches of 200 to avoid memory spikes on large databases.
• Skips movies whose slug already ends with '-download' (already updated).
• A movie is skipped (not overwritten) if it has no categories yet —
  re-run after the scraper assigns categories.
• On collision the command appends -2, -3 etc. just like the model does.
"""

from django.core.management.base import BaseCommand
from django.utils.text import slugify
from movies.models import Movie


SEO_SUFFIX_MAP = [
    # (category_name_lower,          suffix)
    # ── Dramas / series FIRST (before generic movie checks) ──────────────
    ('chinese drama',               'chinese-drama'),
    ('k drama',                     'korean-drama'),
    ('korean drama',                'korean-drama'),
    ('thai drama',                  'thai-drama'),
    ('turkish drama',               'turkish-drama'),
    ('spanish drama',               'spanish-drama'),
    ('filipino drama',              'filipino-drama'),
    ('filipino',                    'filipino-drama'),
    ('anime',                       'anime-series'),
    ('nollywood tv series',         'nollywood-series'),
    ('hollywood tv series',         'hollywood-tv-series'),
    ('sa series',                   'sa-series'),
    ('south africa',                'sa-series'),
    ('tv series',                   'tv-series'),
    ('series',                      'tv-series'),
    # ── Movies ───────────────────────────────────────────────────────────
    ('japanese movie',              'japanese-movie'),
    ('animation movie',             'animation-movie'),
    ('bollywood movies',            'bollywood-movie'),
    ('bollywood',                   'bollywood-movie'),
    ('nollywood movies',            'nollywood-movie'),
    ('nollywood movie',             'nollywood-movie'),
    ('nollywood',                   'nollywood-movie'),
    ('hollywood movies',            'hollywood-movie'),
    ('hollywood movie',             'hollywood-movie'),
    ('hollywood',                   'hollywood-movie'),
    ('18+ movie',                   'movie'),
    ('18plus',                      'movie'),
    ('adult',                       'movie'),
]

COUNTRY_SUFFIX_MAP = [
    ('korean',   'korean-drama'),
    ('china',    'chinese-drama'),
    ('chinese',  'chinese-drama'),
    ('thai',     'thai-drama'),
    ('turkish',  'turkish-drama'),
    ('spanish',  'spanish-drama'),
]


def compute_seo_suffix(movie):
    """
    Return the right suffix string for this movie based on its categories
    and vi_country field.  Mirrors the logic in Movie._compute_seo_suffix().
    """
    cat_names = [c.name.lower() for c in movie.categories.all()]
    country = (movie.vi_country or '').lower()

    for cat, suffix in SEO_SUFFIX_MAP:
        if cat in cat_names:
            return suffix

    for kw, suffix in COUNTRY_SUFFIX_MAP:
        if kw in country:
            return suffix

    return 'download'   # generic fallback


def build_unique_slug(movie, seo_suffix):
    """
    Build a slug like '<title>-<seo_suffix>-download' and ensure it is unique
    in the DB (appends -2, -3, … on collision).
    """
    base = f"{slugify(movie.title)}-{seo_suffix}-download"
    slug = base
    n = 1
    qs = Movie.objects.exclude(pk=movie.pk)
    while qs.filter(slug=slug).exists():
        n += 1
        slug = f"{base}-{n}"
    return slug


class Command(BaseCommand):
    help = "Regenerate all movie slugs with SEO-rich content-type suffixes."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Print proposed changes without touching the database."
        )
        parser.add_argument(
            '--movie-id', type=int, default=None,
            help="Only reslug this specific movie ID."
        )
        parser.add_argument(
            '--limit', type=int, default=None,
            help="Only process the first N movies (useful for testing)."
        )
        parser.add_argument(
            '--force', action='store_true',
            help="Re-slug ALL movies, even those already ending in '-download'."
        )

    def handle(self, *args, **options):
        dry_run   = options['dry_run']
        movie_id  = options['movie_id']
        limit     = options['limit']
        force     = options['force']

        qs = Movie.objects.prefetch_related('categories').order_by('id')

        if movie_id:
            qs = qs.filter(pk=movie_id)
        if limit:
            qs = qs[:limit]

        total   = qs.count()
        changed = 0
        skipped = 0

        self.stdout.write(f"\n{'[DRY RUN] ' if dry_run else ''}Processing {total} movies...\n")

        BATCH = 200
        offset = 0

        while offset < total:
            batch = list(qs[offset:offset + BATCH])
            offset += BATCH

            for movie in batch:
                old_slug = movie.slug

                # Skip already-updated slugs unless --force
                if not force and old_slug.endswith('-download'):
                    skipped += 1
                    continue

                # Skip movies with no categories (scraper hasn't assigned them yet)
                if not movie.categories.exists():
                    self.stdout.write(
                        self.style.WARNING(f"  SKIP (no categories) [{movie.pk}] {movie.title}")
                    )
                    skipped += 1
                    continue

                seo_suffix = compute_seo_suffix(movie)
                new_slug   = build_unique_slug(movie, seo_suffix)

                if new_slug == old_slug:
                    skipped += 1
                    continue

                self.stdout.write(
                    f"  {'WOULD UPDATE' if dry_run else 'UPDATE'} [{movie.pk}] "
                    f"{old_slug}  →  {new_slug}"
                )

                if not dry_run:
                    movie.slug = new_slug
                    Movie.objects.filter(pk=movie.pk).update(slug=new_slug)

                changed += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\n{'[DRY RUN] ' if dry_run else ''}"
                f"Done. Changed: {changed} | Skipped/already-done: {skipped} | Total: {total}\n"
            )
        )

        if dry_run and changed:
            self.stdout.write(
                "Run without --dry-run to apply these changes.\n"
            )