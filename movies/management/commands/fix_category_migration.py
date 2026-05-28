"""
Place this file at:
  movies/management/commands/fix_category_migration.py

(Create the management/commands/ folders and empty __init__.py files if they don't exist)

Run with:
  python manage.py fix_category_migration
"""
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = 'Cleans up the broken category slug migration so it can re-run'

    def handle(self, *args, **options):
        with connection.cursor() as cursor:

            cursor.execute('DROP INDEX IF EXISTS "movies_category_slug_ca1e303d_like";')
            self.stdout.write('Dropped index: movies_category_slug_ca1e303d_like')

            cursor.execute('DROP INDEX IF EXISTS "movies_category_slug_key";')
            self.stdout.write('Dropped index: movies_category_slug_key')

            cursor.execute('ALTER TABLE movies_category DROP COLUMN IF EXISTS slug;')
            self.stdout.write('Dropped column: movies_category.slug (if it existed)')

            cursor.execute("""
                DELETE FROM django_migrations
                WHERE app = 'movies'
                AND name = '0006_category_slug_alter_movie_slug';
            """)
            self.stdout.write('Cleared migration record from django_migrations')

        self.stdout.write(self.style.SUCCESS('\nAll clean! Now run: python manage.py migrate'))