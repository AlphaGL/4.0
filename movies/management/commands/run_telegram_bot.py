"""
Long-polling Telegram bot: answers /start deep links from the site's
"Download via Telegram" button and channel posts' "Fast Download" button.

This is the delivery side of the pipeline whose ingest side lives in
_telegram_upload.py. It's a persistent process — run it as a systemd service
(alongside gunicorn on the VPS), NOT via GitHub Actions (those jobs are
one-shot and would have to run forever to keep listening).

Payload formats (from the 'start_param' after /start):
    movie<id>   — from channel posts / a plain movie page. Resolves the
                  movie's download link(s); a series with more than one
                  gets an inline episode picker (tap → same delivery below).
    dl<id>      — from the website's per-episode "Download via Telegram"
                  button. Resolves that exact DownloadLink directly.

Delivery, once a DownloadLink is resolved:
    1. telegram_message_id is set → copyMessage from the private
       "Watch2D File Storage" channel straight into the user's chat.
       Instant, no re-upload, works regardless of file size.
    2. Not archived yet → resolve_direct_link() (the exact same resolver the
       scrapers use) against the source landing page, and send that raw
       direct URL as a message instead — tapping it starts the download
       immediately, with a note explaining it's not from Telegram's CDN.
    3. Movie has no download links at all (stream-only title) → send the
       site's Watch Now / stream page link.

Run:  python manage.py run_telegram_bot
"""
import html
import re
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand


def _api(method: str) -> str:
    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
    return f"https://api.telegram.org/bot{token}/{method}"


def _send_message(chat_id, text: str, reply_markup: dict = None):
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': False,
    }
    if reply_markup:
        payload['reply_markup'] = reply_markup
    try:
        requests.post(_api('sendMessage'), json=payload, timeout=15)
    except Exception as e:
        print(f"⚠️  sendMessage failed: {e}")


def _copy_message(chat_id, from_chat_id, message_id) -> bool:
    try:
        resp = requests.post(_api('copyMessage'), json={
            'chat_id': chat_id,
            'from_chat_id': from_chat_id,
            'message_id': message_id,
        }, timeout=20)
        return resp.ok and resp.json().get('ok', False)
    except Exception as e:
        print(f"⚠️  copyMessage failed: {e}")
        return False


def _answer_callback(callback_query_id):
    try:
        requests.post(_api('answerCallbackQuery'),
                      json={'callback_query_id': callback_query_id}, timeout=10)
    except Exception:
        pass


class Command(BaseCommand):
    help = 'Long-polling Telegram bot for ad-gated file delivery (Download via Telegram).'

    def handle(self, *args, **options):
        if not getattr(settings, 'TELEGRAM_BOT_TOKEN', ''):
            self.stderr.write("❌  TELEGRAM_BOT_TOKEN not configured — nothing to run.")
            return

        # A webhook and getUpdates polling can't both be active — make sure
        # nothing from an earlier setup is still registered.
        try:
            requests.post(_api('deleteWebhook'), timeout=15)
        except Exception:
            pass

        print("🤖  Watch2D delivery bot — long polling started.")
        offset = None
        while True:
            try:
                params = {'timeout': 30}
                if offset is not None:
                    params['offset'] = offset
                resp = requests.get(_api('getUpdates'), params=params, timeout=40)
                data = resp.json()
                for update in data.get('result', []):
                    offset = update['update_id'] + 1
                    try:
                        self._handle_update(update)
                    except Exception as e:
                        print(f"⚠️  update handling error: {e}")
            except Exception as e:
                print(f"⚠️  polling error: {e}")
                time.sleep(5)

    # ── Update routing ──────────────────────────────────────────────────
    def _handle_update(self, update: dict):
        if 'message' in update:
            self._handle_message(update['message'])
        elif 'callback_query' in update:
            self._handle_callback(update['callback_query'])

    def _handle_message(self, message: dict):
        chat_id = message['chat']['id']

        # Sent automatically when the ad-gate Mini App page calls
        # Telegram.WebApp.sendData() after the Monetag ad resolves.
        web_app_data = message.get('web_app_data')
        if web_app_data:
            payload = (web_app_data.get('data') or '').strip()
            if payload:
                self._deliver_after_ad(chat_id, payload)
            return

        text = (message.get('text') or '').strip()

        if text.startswith('/start'):
            parts = text.split(maxsplit=1)
            payload = parts[1].strip() if len(parts) > 1 else ''
            if not payload:
                _send_message(
                    chat_id,
                    "👋 Welcome to Watch2D!\n\n"
                    "Tap a <b>Download via Telegram</b> button on the site or "
                    "in the channel to get a file here — or just type a movie "
                    "or show title to search for it."
                )
                return
            self._deliver(chat_id, payload)
            return

        if text.startswith('/'):
            return  # unrecognized command — ignore

        self._search(chat_id, text)

    def _search(self, chat_id, query: str):
        """Free-text title search — lets someone just type e.g. 'Avatar the
        Last Airbender' instead of needing a deep link at all."""
        from movies.models import Movie

        query = query.strip()
        if len(query) < 2:
            _send_message(chat_id, "🔎 Type at least 2 characters to search.")
            return

        safe_query = html.escape(query)
        matches = list(
            Movie.objects.filter(title__icontains=query).order_by('-created_at')[:8]
        )
        if not matches:
            _send_message(chat_id, f"😕 No results for '<b>{safe_query}</b>'. "
                                    f"Try a different spelling?")
            return

        buttons = []
        for m in matches:
            label = f"{m.title} ({m.vi_year})" if getattr(m, 'vi_year', '') else m.title
            buttons.append([{'text': label[:64], 'callback_data': f'movie{m.pk}'}])

        _send_message(
            chat_id, f"🔎 Results for '<b>{safe_query}</b>':",
            reply_markup={'inline_keyboard': buttons},
        )

    def _handle_callback(self, cq: dict):
        chat_id = cq['message']['chat']['id']
        data = (cq.get('data') or '').strip()
        _answer_callback(cq['id'])
        if data:
            self._deliver(chat_id, data)

    # ── Payload resolution ──────────────────────────────────────────────
    def _deliver(self, chat_id, payload: str):
        """Resolve a movie<id>/dl<id> payload to a specific file/link, then
        offer the ad-gate button — actual delivery only happens after the
        Monetag ad resolves (see _deliver_after_ad)."""
        from movies.models import Movie, DownloadLink

        if payload.startswith('dl'):
            try:
                dl = DownloadLink.objects.select_related('movie').get(pk=int(payload[2:]))
            except (DownloadLink.DoesNotExist, ValueError):
                _send_message(chat_id, "⚠️ That link isn't available anymore.")
                return
            self._offer_ad_gate(chat_id, dl)
            return

        if payload.startswith('movie'):
            try:
                movie = Movie.objects.get(pk=int(payload[5:]))
            except (Movie.DoesNotExist, ValueError):
                _send_message(chat_id, "⚠️ That title isn't available anymore.")
                return

            links = list(movie.download_links.all())
            if not links:
                # Stream-only title — no file to hand over, send the watch page.
                site = getattr(settings, 'SITE_URL', 'https://watch2d.org').rstrip('/')
                slug = getattr(movie, 'slug', '') or ''
                url = f"{site}/movie/{movie.pk}/{slug}/" if slug else f"{site}/movie/{movie.pk}/"
                _send_message(chat_id, f"▶️ Watch it here: {url}")
                return

            if len(links) == 1:
                self._offer_ad_gate(chat_id, links[0])
                return

            # Series with multiple episodes/qualities — let them pick.
            buttons = [
                [{'text': dl.label or f'Link {dl.pk}', 'callback_data': f'dl{dl.pk}'}]
                for dl in links[:20]
            ]
            _send_message(
                chat_id, f"📺 <b>{html.escape(movie.title)}</b>\nPick an episode:",
                reply_markup={'inline_keyboard': buttons},
            )
            return

        _send_message(chat_id, "⚠️ Unrecognized link.")

    def _offer_ad_gate(self, chat_id, dl):
        """
        Send the 'Continue to Download' button — a Telegram Web App button,
        NOT a plain link. This is what makes the Monetag ad actually play:
        tapping it opens telegram_ad_gate.html in a webview, which shows the
        ad, then calls Telegram.WebApp.sendData() to hand the payload back to
        this bot as a web_app_data message (see _handle_message).
        """
        site = getattr(settings, 'SITE_URL', 'https://watch2d.org').rstrip('/')
        # dl.label is often just generic scraped button text ("DOWNLOAD") for
        # a plain movie — only worth showing alongside the title when it
        # actually names an episode.
        label = (dl.label or '').strip()
        title = html.escape(dl.movie.title)
        if re.search(r'episode|s\d+e\d+', label, re.IGNORECASE):
            title += f" — {html.escape(label)}"
        gate_url = f"{site}/tg/ad-gate/?p=dl{dl.pk}"
        _send_message(
            chat_id,
            f"🎬 <b>{title}</b>\n\n"
            "Tap below to continue — a short ad plays first, then your "
            "download starts.",
            reply_markup={'inline_keyboard': [[
                {'text': '▶️ Continue to Download', 'web_app': {'url': gate_url}},
            ]]},
        )

    def _deliver_after_ad(self, chat_id, payload: str):
        """Called once the ad-gate page confirms the ad resolved. payload is
        always a dl<id> here — episode/movie ambiguity was already resolved
        before the ad-gate button was ever shown."""
        from movies.models import DownloadLink

        if not payload.startswith('dl'):
            return
        try:
            dl = DownloadLink.objects.select_related('movie').get(pk=int(payload[2:]))
        except (DownloadLink.DoesNotExist, ValueError):
            _send_message(chat_id, "⚠️ That link isn't available anymore.")
            return

        file_storage = getattr(settings, 'TELETHON_PRIVATE_CHANNEL', None)
        self._deliver_link(chat_id, dl, file_storage)

    def _deliver_link(self, chat_id, dl, file_storage):
        from movies.management.commands._telegram_upload import resolve_direct_link

        if dl.telegram_message_id and file_storage:
            if _copy_message(chat_id, file_storage, dl.telegram_message_id):
                return
            # Message was deleted/inaccessible — fall through to the live link.

        _send_message(chat_id, "⏳ Fetching your link…")
        session = requests.Session()
        direct = resolve_direct_link(dl.url, session)
        target = direct or dl.url
        _send_message(
            chat_id,
            f"📥 Not yet on our fast servers — here's your direct link 👇\n{target}",
        )
