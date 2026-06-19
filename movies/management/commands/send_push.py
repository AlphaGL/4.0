"""
Broadcast a custom push notification to ALL app users, and save it to the
in-app notification inbox.

  python manage.py send_push --title "We're back!" --body "500 new movies added tonight 🍿"
  python manage.py send_push --title "New drop" --body "Watch it now" --movie 1234
  python manage.py send_push --title "..." --body "..." --no-push   # inbox only

How it reaches people:
  • PUSH  → an FCM notification to the "all_users" topic, which every install is
            already subscribed to. So it reaches users on the CURRENT build too —
            no app update needed. The phone shows the notification immediately.
  • INBOX → also inserts a row into the chat project's `notifications` table, so
            once the new build (with the bell icon) is live, it shows up there
            with read-all / clear.

Env needed:
  FIREBASE_SERVICE_ACCOUNT  the Firebase service-account JSON (for the push).
  CHAT_DATABASE_URL         the chat Supabase DB url (for the inbox row).
Either one missing just skips that half (with a notice) — the other still runs.
"""
import json
import os

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Broadcast a custom push to all users (topic all_users) + save to the in-app inbox."

    def add_arguments(self, parser):
        parser.add_argument('--title', required=True, help='Notification title.')
        parser.add_argument('--body', required=True, help='Notification body text.')
        parser.add_argument('--movie', type=int, default=None,
                            help='Optional movie id to open when tapped.')
        parser.add_argument('--image', default=None,
                            help='Optional image URL (shown in the inbox).')
        parser.add_argument('--no-push', action='store_true',
                            help='Only save to the inbox; do not send the FCM push.')

    def handle(self, *args, **o):
        title, body = o['title'].strip(), o['body'].strip()
        if not title or not body:
            self.stderr.write("Both --title and --body are required.")
            return
        self._save_to_inbox(title, body, o.get('movie'), o.get('image'))
        if not o['no_push']:
            self._send_push(title, body, o.get('movie'))

    # ── Inbox (chat Supabase project) ──────────────────────────────────────
    def _save_to_inbox(self, title, body, movie_id, image):
        url = (os.environ.get('CHAT_DATABASE_URL') or '').strip()
        if not url:
            self.stderr.write("CHAT_DATABASE_URL not set — skipping inbox save.")
            return
        try:
            import psycopg2
            conn = psycopg2.connect(url)
            with conn, conn.cursor() as cur:
                cur.execute(
                    "insert into notifications (title, body, movie_id, image) "
                    "values (%s, %s, %s, %s)",
                    (title, body, movie_id, image),
                )
            conn.close()
            self.stdout.write(self.style.SUCCESS("Saved to the in-app inbox."))
        except Exception as e:
            self.stderr.write(f"Inbox save failed (non-fatal): {e}")

    # ── Push (FCM, topic all_users) ────────────────────────────────────────
    def _send_push(self, title, body, movie_id):
        sa = (os.environ.get('FIREBASE_SERVICE_ACCOUNT') or '').strip()
        if not sa:
            self.stderr.write("FIREBASE_SERVICE_ACCOUNT not set — skipping FCM push.")
            return
        try:
            import firebase_admin
            from firebase_admin import credentials, messaging

            if not firebase_admin._apps:
                firebase_admin.initialize_app(
                    credentials.Certificate(json.loads(sa)))

            # A NOTIFICATION message (not a data message): existing installs show
            # it automatically (no 'type', so the app's rich-card path is skipped).
            # movie_id rides along so a tap can deep-link to the title.
            data = {'movie_id': str(movie_id)} if movie_id else {}
            message = messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                data=data,
                topic='all_users',
                android=messaging.AndroidConfig(priority='high'),
            )
            resp = messaging.send(message)
            self.stdout.write(self.style.SUCCESS(
                f"Push sent to all_users: {resp}"))
        except Exception as e:
            self.stderr.write(f"Push failed: {e}")
