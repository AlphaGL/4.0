"""
Send ONE batched push notification to all app users about recently-added
content. Run at the end of the scrape workflow (on the app-DB account).

  python manage.py notify_new_content            # last 13h of new content
  python manage.py notify_new_content --hours 24

Needs the env var FIREBASE_SERVICE_ACCOUNT (the Firebase service-account JSON).
If it's not set, the command no-ops gracefully.
"""
import json
import os
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from movies.models import Movie


class Command(BaseCommand):
    help = "Send one batched FCM push about new movies/episodes."

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=13,
                            help='Look-back window for "new" content (default 13).')

    def handle(self, *args, **opts):
        since = timezone.now() - timedelta(hours=opts['hours'])
        qs = (Movie.objects
              .filter(Q(created_at__gte=since) | Q(title_b_updated_at__gte=since))
              .order_by('-created_at'))
        count = qs.count()
        if count == 0:
            self.stdout.write(f"No new content in the last {opts['hours']}h — no push.")
            return

        # Feature the newest title that has artwork — the app renders this as a
        # Netflix-style card ("<name>'s New Arrival", big picture, action row).
        featured = (qs.exclude(image_url='').exclude(image_url__isnull=True)
                    .only('id', 'title', 'slug', 'image_url').first() or
                    qs.only('id', 'title', 'slug', 'image_url').first())

        self._send(featured, count)

    def _send(self, movie, count):
        sa = (os.environ.get('FIREBASE_SERVICE_ACCOUNT') or '').strip()
        if not sa:
            self.stderr.write("FIREBASE_SERVICE_ACCOUNT not set — skipping push.")
            return
        try:
            import firebase_admin
            from firebase_admin import credentials, messaging

            if not firebase_admin._apps:
                firebase_admin.initialize_app(credentials.Certificate(json.loads(sa)))

            # DATA message: the app builds the rich, personalized notification.
            message = messaging.Message(
                data={
                    'type': 'new_arrival',
                    'movie_id': str(movie.id),
                    'title': movie.title,
                    'image': movie.image_url or '',
                    'slug': movie.slug or '',
                },
                topic='all_users',
                android=messaging.AndroidConfig(priority='high'),
            )
            resp = messaging.send(message)
            self.stdout.write(self.style.SUCCESS(
                f"Push sent (featured '{movie.title}', {count} new): {resp}"))
        except Exception as e:
            self.stderr.write(f"Push failed (non-fatal): {e}")
