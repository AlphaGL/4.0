"""
automation/telegram.py
Low-level helpers for sending messages/photos to Telegram.
All other code imports from here — keeps API calls in one place.
"""

import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def _token():
    return getattr(settings, 'TELEGRAM_BOT_TOKEN', '')


def _ready():
    t = _token()
    return bool(t) and t != 'your-telegram-bot-token-here'


def send_message(channel_id: str, text: str, parse_mode: str = 'HTML',
                 reply_markup: dict = None) -> dict:
    """Send a plain text message. Optional reply_markup attaches inline buttons
    (e.g. a 'Download Link' button). Returns API response dict."""
    if not _ready():
        logger.warning("Telegram token not configured — skipping.")
        return {}

    payload = {
        'chat_id': channel_id,
        'text': text,
        'parse_mode': parse_mode,
        'disable_web_page_preview': False,
    }
    if reply_markup:
        payload['reply_markup'] = reply_markup

    url = f"https://api.telegram.org/bot{_token()}/sendMessage"
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def send_photo(channel_id: str, photo_url: str, caption: str,
               reply_markup: dict = None) -> dict:
    """Send a photo with caption + optional inline buttons. Falls back to
    text-only (keeping the buttons) if the photo fails."""
    if not _ready():
        logger.warning("Telegram token not configured — skipping.")
        return {}

    payload = {
        'chat_id': channel_id,
        'photo': photo_url,
        'caption': caption,
        'parse_mode': 'HTML',
    }
    if reply_markup:
        payload['reply_markup'] = reply_markup

    url = f"https://api.telegram.org/bot{_token()}/sendPhoto"
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"Photo send failed ({e}), falling back to text.")
        return send_message(channel_id, caption, reply_markup=reply_markup)