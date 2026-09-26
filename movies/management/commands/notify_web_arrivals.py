"""
Emails everyone who tapped "Notify Me" on an upcoming title, once that title
actually enters the catalogue as a real Movie. One-time notification — the
NotifyRequest row is deleted right after sending (or on any hard failure that
means it'll never succeed, to avoid emailing forever).

Deliberately does NOT depend on UpcomingTitle still existing — it's deleted
the moment fetch_upcoming.py sees a matching Movie (see NotifyRequest's model
docstring). This command checks Movie.tmdb_id directly instead.

    python manage.py notify_web_arrivals

Needs BREVO_API_KEY (+ BREVO_SENDER_EMAIL/NAME) in .env — same Brevo account
already used for the contact form and broken-link alerts.
"""
import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from movies.models import Movie, NotifyRequest

logger = logging.getLogger(__name__)


def _send_arrival_email(user, movie):
    api_key = getattr(settings, 'BREVO_API_KEY', '')
    sender_email = getattr(settings, 'BREVO_SENDER_EMAIL', '')
    sender_name = getattr(settings, 'BREVO_SENDER_NAME', 'Watch2D')
    if not api_key or not sender_email or not user.email:
        return False
    try:
        import sib_api_v3_sdk
        cfg = sib_api_v3_sdk.Configuration()
        cfg.api_key['api-key'] = api_key
        api = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(cfg))
        url = f"https://www.watch2d.org{movie.get_absolute_url()}"
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;">
          <h2 style="color:#e50914;">🔔 {movie.title} is here!</h2>
          <p>Hey {user.username}, the title you asked to be notified about just
             landed on Watch2D.</p>
          {f'<img src="{movie.image_url}" style="width:160px;border-radius:8px;margin:12px 0;">' if movie.image_url else ''}
          <p><a href="{url}" style="display:inline-block;padding:10px 20px;background:#e50914;color:#fff;
             border-radius:6px;text-decoration:none;font-weight:bold;">Watch Now</a></p>
          <p style="margin-top:24px;color:#a0aec0;font-size:12px;">— Watch2D</p>
        </div>"""
        email = sib_api_v3_sdk.SendSmtpEmail(
            to=[{'email': user.email}],
            sender={'name': sender_name, 'email': sender_email},
            subject=f"🔔 {movie.title} is now on Watch2D",
            html_content=html,
        )
        api.send_transac_email(email)
        return True
    except Exception as e:
        logger.error(f'notify_web_arrivals: Brevo send failed for {user.email}: {e}')
        return False


class Command(BaseCommand):
    help = "Email users who tapped 'Notify Me' once their title arrives in the catalogue."

    def handle(self, *args, **opts):
        tmdb_ids = set(NotifyRequest.objects.values_list('tmdb_id', flat=True).distinct())
        if not tmdb_ids:
            self.stdout.write("No pending notify requests.")
            return

        arrived = {
            m.tmdb_id: m for m in
            Movie.objects.filter(tmdb_id__in=tmdb_ids).only('id', 'tmdb_id', 'title', 'image_url', 'slug')
        }
        if not arrived:
            self.stdout.write(f"{len(tmdb_ids)} pending requests, none have arrived yet.")
            return

        sent = 0
        for tmdb_id, movie in arrived.items():
            requests_for_title = NotifyRequest.objects.filter(tmdb_id=tmdb_id).select_related('user')
            for req in requests_for_title:
                if _send_arrival_email(req.user, movie):
                    sent += 1
                req.delete()  # one-time notification either way — never retry forever

        self.stdout.write(self.style.SUCCESS(
            f"{len(arrived)} titles arrived, {sent} emails sent."))
