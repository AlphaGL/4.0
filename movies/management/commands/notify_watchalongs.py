"""
Watch-along reminders: ~10-15 min before a scheduled watch-along starts, push
the people who RSVP'd ("I'm in") so they actually show up together. This is the
appointment-viewing hook that drives people back into the community.

  python manage.py notify_watchalongs

Run it on a schedule (every ~5 min) — see .github/workflows/watchalong_reminders.yml.

Reads from two databases (both via direct connections, so it's self-contained):
  CHAT_DATABASE_URL  → the `events` table (watch-alongs + RSVP uids in `going`)
  APP_DATABASE_URL   → the `app_device` table (each user's FCM tokens)
  FIREBASE_SERVICE_ACCOUNT → to send the push
Any missing → it no-ops with a notice.
"""
import json

import psycopg2
from decouple import config
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Remind RSVP'd users that their watch-along is about to start."

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
            # Parties starting within the next ~15 min that we haven't pinged yet.
            ccur.execute("""
                SELECT id, room_id, title, going
                FROM events
                WHERE notified = false
                  AND scheduled_for <= now() + interval '15 minutes'
                  AND scheduled_for >  now() - interval '15 minutes'
            """)
            rows = ccur.fetchall()
            if not rows:
                self.stdout.write("No watch-alongs starting soon.")
                return

            appcur = app.cursor()
            sent = 0
            done_ids = []
            for ev_id, room_id, title, going in rows:
                done_ids.append(ev_id)
                uids = [str(u) for u in (going or [])]
                if not uids:
                    continue
                appcur.execute(
                    "SELECT token FROM app_device WHERE user_id = ANY(%s::uuid[])",
                    [uids])
                tokens = list({r[0] for r in appcur.fetchall() if r[0]})
                if not tokens:
                    continue

                movie_id = room_id[6:] if room_id.startswith('movie_') else ''
                msg = messaging.MulticastMessage(
                    tokens=tokens,
                    notification=messaging.Notification(
                        title="🍿 Watch-along starting",
                        body=f"{title} is starting now — join everyone watching!"),
                    data={'movie_id': movie_id} if movie_id else {},
                    android=messaging.AndroidConfig(priority='high'),
                )
                try:
                    resp = messaging.send_each_for_multicast(msg)
                    sent += resp.success_count
                except Exception as e:
                    self.stderr.write(f"send failed for {title!r}: {e}")

            # Mark them so nobody gets pinged twice.
            if done_ids:
                ccur.execute(
                    "UPDATE events SET notified = true WHERE id = ANY(%s::uuid[])",
                    [[str(i) for i in done_ids]])
                chat.commit()

            self.stdout.write(self.style.SUCCESS(
                f"Sent {sent} watch-along reminders across {len(rows)} event(s)."))
        finally:
            chat.close()
            app.close()
