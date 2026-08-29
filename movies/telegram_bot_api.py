"""
Thin, shared wrapper around the plain Telegram Bot API — used by both the
persistent delivery bot (management/commands/run_telegram_bot.py) and the
ad-gate view (views.py::telegram_ad_gate_deliver). Kept in one place so
both call sites send messages/copy files identically.
"""
import requests
from django.conf import settings


def api_url(method: str) -> str:
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
    return f"https://api.telegram.org/bot{token}/{method}"


def send_message(chat_id, text: str, reply_markup: dict = None):
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': False,
    }
    if reply_markup:
        payload['reply_markup'] = reply_markup
    try:
        requests.post(api_url('sendMessage'), json=payload, timeout=15)
    except Exception as e:
        print(f"⚠️  sendMessage failed: {e}")


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
