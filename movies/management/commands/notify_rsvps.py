"""
Notify a watch-along HOST when new people RSVP to their party ("3 people are in
for your watch-along!"), so hosting feels rewarding and they come back.

  python manage.py notify_rsvps

Runs alongside the reminders (every ~5 min). Tracks `rsvp_seen` on each event so
the host is only pinged about *new* sign-ups, and never about their own RSVP.

  CHAT_DATABASE_URL → events (going[] RSVPs + host_uid + rsvp_seen)
  APP_DATABASE_URL  → app_device (host's FCM tokens)
  FIREBASE_SERVICE_ACCOUNT → to send
"""
import json

import psycopg2
from decouple import config
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Notify watch-along hosts when new people RSVP."

    def handle(self, *args, **opts):
        sa = config('FIREBASE_SERVICE_ACCOUNT', default='').strip()
        chat_url = config('CHAT_DATABASE_URL', default='').strip()
        app_url = config('APP_DATABASE_URL', default='').strip()
        if not (sa and chat_url and app_url):
            self.stderr.write(
                "Need FIREBASE_SERVICE_ACCOUNT + CHAT_DATABASE_URL + APP_DATABASE_URL.")
            return

        import firebase_admin
        from firebase_admin import credentials, messaging
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(json.loads(sa)))

        chat = psycopg2.connect(chat_url)
        app = psycopg2.connect(app_url)
        try:
            ccur = chat.cursor()
            # Upcoming/just-started parties that have a host to notify.
            ccur.execute("""
                SELECT id, room_id, title, host_uid, going, rsvp_seen
                FROM events
                WHERE host_uid IS NOT NULL
                  AND scheduled_for > now() - interval '15 minutes'
            """)
            rows = ccur.fetchall()
            appcur = app.cursor()
            sent = 0
            reported = 0
            for ev_id, room_id, title, host_uid, going, seen in rows:
                # Count RSVPs excluding the host themselves.
                others = [u for u in (going or []) if str(u) != str(host_uid)]
                n = len(others)
                if n <= seen:
                    continue  # no new sign-ups since last ping
                reported += 1

                appcur.execute(
                    "SELECT token FROM app_device WHERE user_id = %s::uuid",
                    [host_uid])
                tokens = list({r[0] for r in appcur.fetchall() if r[0]})
                # Update the high-water mark regardless, so we never double-ping.
                ccur.execute("UPDATE events SET rsvp_seen = %s WHERE id = %s",
                             [n, ev_id])
                if not tokens:
                    continue

                movie_id = room_id[6:] if room_id.startswith('movie_') else ''
                verb = 'person is' if n == 1 else 'people are'
                msg = messaging.MulticastMessage(
                    tokens=tokens,
                    notification=messaging.Notification(
                        title="🎉 Your watch-along is filling up",
                        body=f"{n} {verb} in for {title}!"),
                    data={'movie_id': movie_id} if movie_id else {},
                    android=messaging.AndroidConfig(priority='high'),
                )
                try:
                    resp = messaging.send_each_for_multicast(msg)
                    sent += resp.success_count
                except Exception as e:
                    self.stderr.write(f"send failed for {title!r}: {e}")

            chat.commit()
            if reported:
                self.stdout.write(self.style.SUCCESS(
                    f"Reported new RSVPs on {reported} event(s); {sent} host pushes."))
            else:
                self.stdout.write("No new RSVPs to report.")
        finally:
            chat.close()
            app.close()
