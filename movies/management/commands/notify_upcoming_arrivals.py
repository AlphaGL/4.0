"""
Personalized "it's here!" push: when a title someone tapped "Notify me" on
(Coming Soon) finally enters the catalogue, push the users who asked for it,
then clear their subscription so they're only told once.

    python manage.py notify_upcoming_arrivals

Needs FIREBASE_SERVICE_ACCOUNT in the env, plus the app_upcoming_notify /
app_device tables (app DB only — gate with DATA_ONLY=true in CI).

Sends a DATA message (not a notification message) so the app builds the rich,
Netflix-style notification (big picture + Watch / Download / My List actions).
"""
import json
import os

from django.core.management.base import BaseCommand
from django.db import connection

from movies.models import Movie


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class Command(BaseCommand):
    help = "Notify users when a Coming Soon title they followed is added."

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

        cur = connection.cursor()
        cur.execute("SELECT DISTINCT tmdb_id FROM app_upcoming_notify")
        tmdb_ids = [r[0] for r in cur.fetchall()]
        if not tmdb_ids:
            self.stdout.write("No upcoming-notify subscriptions.")
            return

        sent = 0
        arrived = 0
        for tid in tmdb_ids:
            movie = (Movie.objects.filter(tmdb_id=tid)
                     .only('id', 'title', 'slug', 'image_url').first())
            if movie is None:
                continue  # not in the catalogue yet — keep the subscription
            arrived += 1

            cur.execute(
                "SELECT user_id FROM app_upcoming_notify WHERE tmdb_id = %s",
                [tid])
            uids = [str(r[0]) for r in cur.fetchall()]
            if uids:
                cur.execute(
                    "SELECT token FROM app_device WHERE user_id = ANY(%s::uuid[])",
                    [uids])
                tokens = list({r[0] for r in cur.fetchall() if r[0]})
                data = {
                    'type': 'new_arrival',
                    'movie_id': str(movie.id),
                    'title': movie.title,
                    'image': movie.image_url or '',
                    'slug': movie.slug or '',
                }
                for batch in _chunks(tokens, 500):
                    try:
                        resp = messaging.send_each_for_multicast(
                            messaging.MulticastMessage(
                                tokens=batch,
                                data=data,
                                android=messaging.AndroidConfig(
                                    priority='high'),
                            ))
                        sent += resp.success_count
                    except Exception as e:
                        self.stderr.write(f"send failed for {movie.title}: {e}")

            # Only told once — clear everyone who followed this title.
            cur.execute(
                "DELETE FROM app_upcoming_notify WHERE tmdb_id = %s", [tid])

        self.stdout.write(self.style.SUCCESS(
            f"Upcoming arrivals: {arrived} titles now in catalogue, "
            f"{sent} pushes sent."))
