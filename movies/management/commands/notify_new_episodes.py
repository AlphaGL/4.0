"""
Personalized push: when a SERIES gets a new episode, notify only the users who
have it on their watchlist. Targets each user's stored FCM token(s).

    python manage.py notify_new_episodes --hours 13

Needs FIREBASE_SERVICE_ACCOUNT in the env, and the app_device / app_watchlist
tables (the app keeps app_device in sync on sign-in).
"""
import json
import os
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

from movies.models import Movie


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class Command(BaseCommand):
    help = "Notify watchlisters of a series that just got a new episode."

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=13,
                            help="Look back this many hours for new episodes.")

    def handle(self, *args, **opts):
        sa = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
        if not sa:
            self.stderr.write("No FIREBASE_SERVICE_ACCOUNT set.")
            return

        import firebase_admin
        from firebase_admin import credentials, messaging
        if not firebase_admin._apps:
            firebase_admin.initialize_app(
                credentials.Certificate(json.loads(sa)))

        since = timezone.now() - timedelta(hours=opts['hours'])
        movies = list(
            Movie.objects.filter(title_b_updated_at__gte=since, is_series=True)
            .exclude(title_b='').exclude(title_b__isnull=True)[:300])
        if not movies:
            self.stdout.write("No new episodes in the window.")
            return

        cur = connection.cursor()
        sent = 0
        for m in movies:
            cur.execute(
                "SELECT user_id FROM app_watchlist WHERE movie_id = %s", [m.id])
            uids = [str(r[0]) for r in cur.fetchall()]
            if not uids:
                continue
            cur.execute(
                "SELECT token FROM app_device WHERE user_id = ANY(%s::uuid[])",
                [uids])
            tokens = list({r[0] for r in cur.fetchall() if r[0]})
            if not tokens:
                continue

            # DATA message → the app builds the rich, Netflix-style card.
            data = {
                'type': 'new_episode',
                'movie_id': str(m.id),
                'title': m.title,
                'image': m.image_url or '',
                'slug': m.slug or '',
            }
            for batch in _chunks(tokens, 500):
                try:
                    resp = messaging.send_each_for_multicast(
                        messaging.MulticastMessage(
                            tokens=batch,
                            data=data,
                            android=messaging.AndroidConfig(priority='high'),
                        ))
                    sent += resp.success_count
                except Exception as e:
                    self.stderr.write(f"send failed for {m.title}: {e}")

        self.stdout.write(self.style.SUCCESS(
            f"Sent {sent} personalized new-episode pushes "
            f"across {len(movies)} updated series."))
