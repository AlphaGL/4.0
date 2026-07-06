"""
Send batched push notifications to all app users about recently-added content.
Instead of ONE push featuring a single title, this sends up to --max pushes,
each featuring a DIFFERENT category's newest title, with a rotating "variant"
so the app can vary the wording (not always "New Arrival for you").

Run at the end of the scrape workflow (on the app-DB account).

  python manage.py notify_new_content            # last 13h, up to 3 categories
  python manage.py notify_new_content --hours 24 --max 4

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
    help = "Send batched FCM pushes about new content, spread across categories."

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=13,
                            help='Look-back window for "new" content (default 13).')
        parser.add_argument('--max', type=int, default=3,
                            help='Max pushes (one per category) to send (default 3).')

    def handle(self, *args, **opts):
        since = timezone.now() - timedelta(hours=opts['hours'])
        qs = (Movie.objects
              .filter(Q(created_at__gte=since) | Q(title_b_updated_at__gte=since))
              .exclude(image_url='').exclude(image_url__isnull=True)
              .order_by('-created_at')
              .prefetch_related('categories'))
        total = qs.count()
        if total == 0:
            self.stdout.write(f"No new content in the last {opts['hours']}h — no push.")
            return

        # Pick the newest title (with artwork) per DISTINCT category, up to --max.
        picks = []          # (movie, category_name)
        seen_cats = set()
        for m in qs[:200]:
            cats = list(m.categories.all()[:1])
            cat = cats[0].name if cats else 'New'
            if cat in seen_cats:
                continue
            seen_cats.add(cat)
            picks.append((m, cat))
            if len(picks) >= opts['max']:
                break

        # Fallback: no categories at all → just feature the newest.
        if not picks:
            picks = [(qs.first(), 'New')]

        sent = 0
        for i, (movie, cat) in enumerate(picks):
            if self._send(movie, cat, i, total):
                sent += 1
        self.stdout.write(self.style.SUCCESS(
            f"Sent {sent} push(es) across {len(picks)} categor(ies) "
            f"[{', '.join(c for _, c in picks)}] — {total} new item(s)."))

    def _send(self, movie, category, variant, count):
        sa = (os.environ.get('FIREBASE_SERVICE_ACCOUNT') or '').strip()
        if not sa:
            self.stderr.write("FIREBASE_SERVICE_ACCOUNT not set — skipping push.")
            return False
        try:
            import firebase_admin
            from firebase_admin import credentials, messaging

            if not firebase_admin._apps:
                firebase_admin.initialize_app(credentials.Certificate(json.loads(sa)))

            # Is this an episode update (title_b bumped) rather than a brand-new
            # title? The app varies the wording for episodes.
            is_episode = bool(
                movie.title_b
                and movie.title_b_updated_at
                and (not movie.created_at
                     or movie.title_b_updated_at >= movie.created_at))

            # DATA message: the app builds the rich, varied notification from
            # category + variant (see firebase_service.showRichNotification).
            message = messaging.Message(
                data={
                    'type': 'new_episode' if is_episode else 'new_arrival',
                    'movie_id': str(movie.id),
                    'title': movie.title,
                    'image': movie.image_url or '',
                    'slug': movie.slug or '',
                    'category': category,
                    'variant': str(variant),
                },
                topic='all_users',
                android=messaging.AndroidConfig(priority='high'),
            )
            resp = messaging.send(message)
            self.stdout.write(
                f"  → [{category}] '{movie.title}' (variant {variant}): {resp}")
            return True
        except Exception as e:
            self.stderr.write(f"Push failed for '{movie.title}' (non-fatal): {e}")
            return False
