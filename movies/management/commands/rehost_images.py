"""
Re-host movie poster images on Cloudflare R2 so they survive the source site
going down. Skips images already on your R2 domain. Safe to re-run.

    python manage.py rehost_images --dry-run
    python manage.py rehost_images               # all external images
    python manage.py rehost_images --limit 500   # in batches

Needs R2_* env vars set (see movies/r2.py) and: pip install boto3
"""
from django.core.management.base import BaseCommand
from decouple import config

from movies.models import Movie
from movies.r2 import rehost_image, is_configured


class Command(BaseCommand):
    help = "Re-host external poster images onto Cloudflare R2."

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=None,
                            help="Only process this many (for batching).")
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        if not is_configured():
            self.stderr.write(self.style.ERROR(
                "R2 not configured. Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
                "R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_URL in your .env."))
            return

        public = config('R2_PUBLIC_URL', default='').rstrip('/')
        qs = (Movie.objects.exclude(image_url__isnull=True)
              .exclude(image_url='')
              .exclude(image_url__startswith=public)   # already re-hosted
              .only('id', 'image_url'))
        if opts['limit']:
            qs = qs[:opts['limit']]
        movies = list(qs)

        self.stdout.write(f"{len(movies)} images to re-host...")
        done = failed = 0
        for m in movies:
            if opts['dry_run']:
                self.stdout.write(f"  would rehost {m.id}: {m.image_url[:70]}")
                continue
            new_url = rehost_image(m.image_url, m.id)
            if new_url:
                Movie.objects.filter(pk=m.id).update(image_url=new_url)
                done += 1
            else:
                failed += 1
            if (done + failed) % 100 == 0 and not opts['dry_run']:
                self.stdout.write(f"  …{done} done, {failed} failed")

        if not opts['dry_run']:
            self.stdout.write(self.style.SUCCESS(
                f"Re-hosted {done}, failed {failed}, of {len(movies)}."))
