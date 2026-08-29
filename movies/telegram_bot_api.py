"""
Thin, shared wrapper around the plain Telegram Bot API — used by both the
persistent delivery bot (management/commands/run_telegram_bot.py) and the
ad-gate view (views.py::telegram_ad_gate_deliver). Kept in one place so
both call sites send messages/copy files identically.
"""
import html

import requests
from django.conf import settings

# Channels the bot can actually verify membership of (getChatMember) — the
# bot must be an admin of both. Anything else (WhatsApp/X/Facebook) has no
# API for a Telegram bot to confirm a follow, so those are a soft ask only.
# Shared between run_telegram_bot.py (the follow-gate) and views.py (the
# ad-gate delivery endpoint), so every message stays consistent.
TELEGRAM_FOLLOW_CHANNELS = [
    ('📢 Telegram Channel', 'https://t.me/+wUlsP5Yv8h9iZDJk', -1003266960032),
    ('📢 Telegram Channel 2', 'https://t.me/+Lve6_XzFxCwxNDdk', -1002231007764),
]
SOCIAL_LINKS = [
    ('💬 WhatsApp Channel', 'https://whatsapp.com/channel/0029VavDAbsEFeXpbo2lEg3f'),
    ('🐦 X (Twitter)', 'https://x.com/watch2download'),
    ('📘 Facebook', 'https://web.facebook.com/WATCH2D'),
]


def social_footer() -> str:
    """One compact line of all 5 platform handles, HTML-linked — appended to
    the key bot messages (ad-gate offer, delivery) so the socials stay
    visible on every interaction, not just the one-time follow-gate."""
    all_links = [(label, url) for label, url, *_ in TELEGRAM_FOLLOW_CHANNELS] + SOCIAL_LINKS
    parts = [f'<a href="{url}">{html.escape(label)}</a>' for label, url in all_links]
    return "  •  ".join(parts)


def api_url(method: str) -> str:
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
    return f"https://api.telegram.org/bot{token}/{method}"


def send_message(chat_id, text: str, reply_markup: dict = None,
                  disable_preview: bool = False):
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': disable_preview,
    }
    if reply_markup:
        payload['reply_markup'] = reply_markup
    try:
        requests.post(api_url('sendMessage'), json=payload, timeout=15)
    except Exception as e:
        print(f"⚠️  sendMessage failed: {e}")


def send_photo(chat_id, photo_url: str, caption: str, reply_markup: dict = None):
    """Send a photo with caption + buttons. Falls back to text-only (keeping
    the buttons) if the photo fails to send — e.g. a dead/missing poster URL."""
    payload = {
        'chat_id': chat_id,
        'photo': photo_url,
        'caption': caption,
        'parse_mode': 'HTML',
    }
    if reply_markup:
        payload['reply_markup'] = reply_markup
    try:
        resp = requests.post(api_url('sendPhoto'), json=payload, timeout=15)
        if resp.ok and resp.json().get('ok'):
            return
    except Exception:
        pass
    send_message(chat_id, caption, reply_markup=reply_markup)


def is_channel_member(user_id, chat_id) -> bool:
    """True only if user_id is a current member/admin/creator of chat_id.
    Requires the bot to be an admin of that channel."""
    try:
        resp = requests.get(api_url('getChatMember'), params={
            'chat_id': chat_id, 'user_id': user_id,
        }, timeout=10)
        data = resp.json()
        if not data.get('ok'):
            return False
        return data['result'].get('status') in ('member', 'administrator', 'creator')
    except Exception:
        return False


def copy_message(chat_id, from_chat_id, message_id) -> bool:
    try:
        resp = requests.post(api_url('copyMessage'), json={
            'chat_id': chat_id,
            'from_chat_id': from_chat_id,
            'message_id': message_id,
        }, timeout=20)
        return resp.ok and resp.json().get('ok', False)
    except Exception as e:
        print(f"⚠️  copyMessage failed: {e}")
        return False


def answer_callback(callback_query_id):
    try:
        requests.post(api_url('answerCallbackQuery'),
                      json={'callback_query_id': callback_query_id}, timeout=10)
    except Exception:
        pass


def validate_init_data(init_data: str):
    """
    Verify Telegram's WebApp initData signature (documented HMAC-SHA256
    check: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)
    and return the numeric chat/user id on success, or None if invalid/missing.
    """
    import hashlib
    import hmac
    from urllib.parse import parse_qsl

    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
    if not init_data or not token:
        return None

    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = pairs.pop('hash', None)
        if not received_hash:
            return None

        data_check_string = '\n'.join(f'{k}={v}' for k, v in sorted(pairs.items()))
        secret_key = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(),
                                  hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed_hash, received_hash):
            return None

        import json
        user = json.loads(pairs.get('user', '{}'))
        return user.get('id')
    except Exception:
        return None
