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
                  gets an inline episode picker (tap → dl<id> below).
    dl<id>      — from the website's per-episode "Download via Telegram"
                  button, or an episode picked from the list above. Sends
                  the ad-gate "Continue to Download" button for that exact
                  DownloadLink.

This bot only ever gets someone as far as the ad-gate button — it does NOT
deliver the file itself. Once tapped, that button opens telegram_ad_gate.html
in a Telegram Web App webview, which plays the Monetag ad and then calls
movies.views.telegram_ad_gate_deliver directly (a plain HTTPS POST,
authenticated with Telegram's signed initData) to actually resolve/copy and
send the file. That split exists because Telegram.WebApp.sendData() —
the obvious way to hand control back to *this* bot — only works for Web
Apps opened via a Keyboard button, not the inline button used here, so the
ad-gate page talks to the website instead of back to this process.

Run:  python manage.py run_telegram_bot
"""
import html
import re
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from movies.telegram_bot_api import SOCIAL_LINKS, TELEGRAM_FOLLOW_CHANNELS
from movies.telegram_bot_api import api_url as _api
from movies.telegram_bot_api import answer_callback as _answer_callback
from movies.telegram_bot_api import is_channel_member as _is_channel_member
from movies.telegram_bot_api import send_message as _send_message
from movies.telegram_bot_api import send_photo as _send_photo
from movies.telegram_bot_api import social_footer as _social_footer


def _has_followed_required_channels(user_id) -> bool:
    """True only if user_id is currently a member of every Telegram channel
    we can actually verify. WhatsApp/X/Facebook aren't checked — no API for
    that — they're shown as a request, not enforced."""
    return all(_is_channel_member(user_id, chat_id)
               for _, _, chat_id in TELEGRAM_FOLLOW_CHANNELS)


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
            if not links and movie.download_url:
                # Legacy titles stored a single flat download_url before
                # DownloadLink existed — backfill a real row on first request
                # so this behaves identically to newer titles (ad-gate,
                # follow-gate, Telegram-archive eligibility, etc.) instead of
                # being mistaken for a stream-only title below.
                links = [DownloadLink.objects.create(
                    movie=movie, url=movie.download_url, label='Download')]

            if not links:
                # Genuinely stream-only title — no file to hand over, send
                # the watch page as a button.
                site = getattr(settings, 'SITE_URL', 'https://watch2d.org').rstrip('/')
                slug = getattr(movie, 'slug', '') or ''
                url = f"{site}/movie/{movie.pk}/{slug}/" if slug else f"{site}/movie/{movie.pk}/"
                caption = self._movie_caption(movie) + "\n\nTap below to watch:"
                markup = {'inline_keyboard': [[{'text': '▶️ Watch Now', 'url': url}]]}
                if movie.image_url:
                    _send_photo(chat_id, movie.image_url, caption, reply_markup=markup)
                else:
                    _send_message(chat_id, caption, reply_markup=markup)
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

    def _movie_caption(self, movie, title_suffix: str = '') -> str:
        """Poster caption: title (+ episode, if any), rating, year, genre."""
        lines = [f"🎬 <b>{html.escape(movie.title)}{title_suffix}</b>", ""]
        if getattr(movie, 'rating', None):
            lines.append(f"⭐ Rating: {movie.rating:.1f}/10")
        if getattr(movie, 'vi_year', ''):
            lines.append(f"📅 Year: {movie.vi_year}")
        try:
            cats = movie.categories.all()
            if cats:
                lines.append(f"🏷 Genre: {', '.join(c.name for c in cats[:3])}")
        except Exception:
            pass
        return "\n".join(lines)

    def _episode_suffix(self, dl) -> str:
        # dl.label is often just generic scraped button text ("DOWNLOAD") for
        # a plain movie — only worth showing alongside the title when it
        # actually names an episode.
        label = (dl.label or '').strip()
        if re.search(r'episode|s\d+e\d+', label, re.IGNORECASE):
            return f" — {html.escape(label)}"
        return ''

    def _send_follow_gate(self, chat_id, dl):
        """
        Shown instead of the ad-gate button when the user hasn't (verifiably)
        followed everywhere yet. Only the two Telegram channels are actually
        checked (getChatMember) — WhatsApp/X/Facebook have no equivalent API,
        so those are a request, not an enforced gate.
        """
        caption = (self._movie_caption(dl.movie, self._episode_suffix(dl))
                   + "\n\n🔒 Follow us everywhere, then tap Continue:")
        buttons = [[{'text': label, 'url': url}]
                   for label, url, _chat_id in TELEGRAM_FOLLOW_CHANNELS]
        buttons += [[{'text': label, 'url': url}] for label, url in SOCIAL_LINKS]
        buttons.append([{'text': "✅ I've Followed — Continue", 'callback_data': f'dl{dl.pk}'}])
        markup = {'inline_keyboard': buttons}

        if dl.movie.image_url:
            _send_photo(chat_id, dl.movie.image_url, caption, reply_markup=markup)
        else:
            _send_message(chat_id, caption, reply_markup=markup)

    def _offer_ad_gate(self, chat_id, dl):
        """
        Send the 'Continue to Download' button — a Telegram Web App button,
        NOT a plain link. Tapping it opens telegram_ad_gate.html in a webview,
        which shows the Monetag ad, then calls the site's own
        /tg/ad-gate/deliver/ endpoint directly (NOT sendData() — that only
        works for Mini Apps opened via a Keyboard button, not this inline
        one) to actually deliver the file.
        """
        if not _has_followed_required_channels(chat_id):
            self._send_follow_gate(chat_id, dl)
            return

        site = getattr(settings, 'SITE_URL', 'https://watch2d.org').rstrip('/')
        gate_url = f"{site}/tg/ad-gate/?p=dl{dl.pk}"
        caption = (self._movie_caption(dl.movie, self._episode_suffix(dl))
                   + "\n\nTap below to continue — a short ad plays first, "
                     "then your download starts."
                   + f"\n\n{_social_footer()}")
        markup = {'inline_keyboard': [[
            {'text': '▶️ Continue to Download', 'web_app': {'url': gate_url}},
        ]]}

        if dl.movie.image_url:
            _send_photo(chat_id, dl.movie.image_url, caption, reply_markup=markup)
        else:
            _send_message(chat_id, caption, reply_markup=markup)
