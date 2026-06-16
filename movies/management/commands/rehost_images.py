"""
Re-host movie poster images on Cloudflare R2 so they survive the source site
going down. Skips images already on your R2 domain. Safe to re-run.

    python manage.py rehost_images --dry-run
    python manage.py rehost_images                  # all external images
    python manage.py rehost_images --workers 16     # faster (more parallel)
    python manage.py rehost_images --limit 2000     # in batches

Needs R2_* env vars set (see movies/r2.py) and: pip install boto3
"""
import concurrent.futures

from django.core.management.base import BaseCommand
from django.db import connections
from decouple import config

from movies.models import Movie
from movies.r2 import rehost_image, is_configured


class Command(BaseCommand):
    help = "Re-host external poster images onto Cloudflare R2 (parallel)."

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=None,
                            help="Only process this many (for batching).")
        parser.add_argument('--workers', type=int, default=8,
                            help="Parallel downloads/uploads (default 8).")
        parser.add_argument('--verbose', action='store_true',
                            help="Print each image URL that fails.")
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
        total = len(movies)
        workers = max(1, opts['workers'])

        self.stdout.write(f"{total} images to re-host (workers={workers})...")

        if opts['dry_run']:
            for m in movies[:30]:
                self.stdout.write(f"  would rehost {m.id}: {m.image_url[:70]}")
            if total > 30:
                self.stdout.write(f"  …and {total - 30} more")
            return

        verbose = opts['verbose']

        def work(m):
            try:
                new_url = rehost_image(m.image_url)
                if new_url:
                    Movie.objects.filter(pk=m.id).update(image_url=new_url)
                    return (m, True)
                return (m, False)
            finally:
                connections.close_all()  # release this thread's DB connection

        done = failed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(work, m) for m in movies]
            for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                try:
                    m, ok = fut.result()
                except Exception:
                    m, ok = None, False
                if ok:
                    done += 1
                else:
                    failed += 1
                    if verbose and m is not None:
                        self.stdout.write(f"  FAIL  {m.id}  {m.image_url[:85]}")
                if i % 100 == 0:
                    self.stdout.write(f"  …{done} done, {failed} failed ({i}/{total})")

        self.stdout.write(self.style.SUCCESS(
            f"Re-hosted {done}, failed {failed}, of {total}."))
