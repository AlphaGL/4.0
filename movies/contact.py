"""
Contact / complaint endpoint for the app. Users send a message from the in-app
"Contact Us" screen; it's stored (never lost) and emailed to the admin via
Brevo — so the admin address is NEVER exposed in the app.

  POST /contact/   {email?, subject?, message, user_id?, app_version?}
"""
import logging

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import ContactMessage

logger = logging.getLogger(__name__)


def _send_to_admin(msg: ContactMessage):
    """Email the complaint to the admin via Brevo. Best-effort."""
    api_key = getattr(settings, 'BREVO_API_KEY', '')
    admin_email = getattr(settings, 'BREVO_ADMIN_EMAIL', '')
    sender_email = getattr(settings, 'BREVO_SENDER_EMAIL', '') or admin_email
    sender_name = getattr(settings, 'BREVO_SENDER_NAME', 'Watch2D')
    if not api_key or not admin_email:
        logger.error('contact: Brevo not configured — message saved only.')
        return
    try:
        import sib_api_v3_sdk
        cfg = sib_api_v3_sdk.Configuration()
        cfg.api_key['api-key'] = api_key
        api = sib_api_v3_sdk.TransactionalEmailsApi(
            sib_api_v3_sdk.ApiClient(cfg))
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
          <h2 style="color:#e50914;">📨 New message from a user</h2>
          <p><strong>Subject:</strong> {msg.subject or '(none)'}</p>
          <p><strong>From:</strong> {msg.email or 'anonymous'}</p>
          <p><strong>App version:</strong> {msg.app_version or '?'} ·
             <strong>User:</strong> {msg.user_id or 'guest'}</p>
          <hr>
          <p style="white-space:pre-wrap;">{msg.message}</p>
          <p style="margin-top:24px;color:#a0aec0;font-size:12px;">
            — Watch2D contact form (message #{msg.id})</p>
        </div>"""
        email = sib_api_v3_sdk.SendSmtpEmail(
            to=[{'email': admin_email}],
            sender={'name': sender_name, 'email': sender_email},
            reply_to=({'email': msg.email} if msg.email else None),
            subject=f"📨 Watch2D: {msg.subject or 'New message'}",
            html_content=html,
        )
        api.send_transac_email(email)
    except Exception as e:
        logger.error(f'contact: Brevo send failed: {e}')


@csrf_exempt
@require_POST
def contact(request):
    message = (request.POST.get('message') or '').strip()
    if not message:
        return JsonResponse({'ok': False, 'error': 'empty'}, status=400)

    msg = ContactMessage.objects.create(
        email=(request.POST.get('email') or '').strip()[:254],
        subject=(request.POST.get('subject') or '').strip()[:140],
        message=message[:4000],
        user_id=(request.POST.get('user_id') or '').strip()[:64],
        app_version=(request.POST.get('app_version') or '').strip()[:20],
    )
    _send_to_admin(msg)
    return JsonResponse({'ok': True, 'message': 'Thanks — we got your message!'})
