"""
Delete specific (junk / mis-scraped) categories by name. The MOVIES are kept —
only the category rows and their movie<->category links are removed.

    python manage.py remove_categories --dry-run
    python manage.py remove_categories
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from movies.models import Category

# Genre-style categories that got polluted with the same Nollywood movies.
JUNK = [
    'Western', 'war', 'turkish drama', 'Thriller', 'sci-fi', 'romance',
    'reality tv', 'other foreign movies', 'mystery', 'horror', 'History',
    'filipino drama', 'fantasy', 'family', 'drama', 'documentary', 'crime',
    'comedy', 'biography', 'adventure', 'adult', 'action',
]


class Command(BaseCommand):
    help = "Delete the listed junk categories (movies are kept)."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help="Show what would be deleted without deleting.")

    def handle(self, *args, **opts):
        dry = opts['dry_run']
        q = Q()
        for name in JUNK:
            q |= Q(name__iexact=name)
        qs = Category.objects.filter(q)

        for c in qs:
            self.stdout.write(f"  {c.name} (id={c.id}) — {c.movies.count()} movies")

        count = qs.count()
        if dry:
            self.stdout.write(self.style.WARNING(f"[dry-run] would delete {count} categories"))
            return
        qs.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {count} categories (movies kept)."))
