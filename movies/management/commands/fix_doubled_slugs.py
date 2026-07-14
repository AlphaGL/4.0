"""
One-off (re-runnable) fixer for slugs that ended up with a doubled
"-download-download" suffix.

Cause: Movie._compute_seo_suffix() returns 'download' as its fallback, and
_generate_unique_slug() then appended another '-download', producing
"<title>-download-download". The generation bug is fixed in models.py; this
command repairs the ~442 rows already saved.

Each affected slug is rewritten by dropping the trailing "-download" (so
"foo-download-download" -> "foo-download"), preserving uniqueness. The old URL
still resolves: MovieDetailView redirects any non-matching slug (301) to the
movie's canonical get_absolute_url(), so Google reconsolidates on the clean URL.

    python manage.py fix_doubled_slugs           # apply
    python manage.py fix_doubled_slugs --dry-run  # preview only
"""
from django.core.management.base import BaseCommand
from movies.models import Movie


class Command(BaseCommand):
    help = "Repair movie slugs with a doubled '-download-download' suffix."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Show what would change without writing.",
        )

    def handle(self, *args, **opts):
        dry = opts['dry_run']
        qs = Movie.objects.filter(slug__endswith='-download-download').order_by('pk')
        total = qs.count()
        self.stdout.write(f"Found {total} movie(s) with a doubled slug suffix.")
        if not total:
            return

        # Cache existing slugs to detect collisions without hammering the DB.
        taken = set(Movie.objects.values_list('slug', flat=True))
        fixed = skipped = 0

        for m in qs.iterator():
            old = m.slug
            new = old[: -len('-download')]  # drop ONE trailing '-download'
            if new == old:
                continue
            # Ensure uniqueness (someone else may already own the clean slug).
            candidate, n = new, 1
            while candidate in taken and candidate != old:
                n += 1
                candidate = f"{new}-{n}"
            if candidate == old:
                skipped += 1
                continue
            self.stdout.write(f"  {m.pk}: {old}  ->  {candidate}")
            if not dry:
                taken.discard(old)
                taken.add(candidate)
                Movie.objects.filter(pk=m.pk).update(slug=candidate)
            fixed += 1

        verb = "Would fix" if dry else "Fixed"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {fixed} slug(s); skipped {skipped}."
        ))
