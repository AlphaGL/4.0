"""
Shared Telegram private-channel uploader — download a resolved movie/episode
file and re-host it on the private Telethon channel, for archival + the
future "Download via Telegram" delivery path.

Used by both scrape_thenkiri.py and scrape_9jarocks.py (via --upload-files),
so the pipeline lives in exactly one place instead of being duplicated per
scraper.

How it works:
  1. resolve_direct_link()        — follows a downloadwella.com-style
     landing page and extracts the real ?pt= download URL before it expires.
  2. download_to_temp()           — streams the file to a temp dir on the
     server disk. Skips if file > MAX_UPLOAD_BYTES.
  3. upload_file_to_private_channel() — sends the file to your private
     Telegram channel via Telethon with a rich caption.
  4. Temp file is always deleted in a finally block.

Setup (one-time, on the server):
  pip install telethon
  Then run:  python manage.py scrape_thenkiri --telethon-login
  This saves a .session file so future --upload-files runs are fully automatic.

Required Django settings (add to settings.py or .env):
  TELETHON_API_ID        = 12345678          # from my.telegram.org
  TELETHON_API_HASH      = "abc123..."       # from my.telegram.org
  TELETHON_SESSION_NAME  = "uploader"        # any name you like
  TELETHON_PRIVATE_CHANNEL = -1001234567890  # your private channel ID
"""
import base64
import os
import re
import shutil
import tempfile
import time
from urllib.parse import urlparse

import cloudscraper
import requests

# Max file size to download+upload (2 GB — Telegram's hard limit)
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024   # 2 GB
# Min free disk space required before attempting a download
MIN_FREE_BYTES   = 5 * 1024 * 1024 * 1024   # 5 GB

# Temp directory for downloaded files (auto-cleaned after upload)
TEMP_DIR = os.path.join(tempfile.gettempdir(), "tg_uploads")


def _ensure_temp_dir():
    os.makedirs(TEMP_DIR, exist_ok=True)


def _cleanup_stale_temp_files():
    """
    Delete any leftover files from a previous crashed run.
    Called once at scraper startup when --upload-files is active.
    """
    if not os.path.isdir(TEMP_DIR):
        return
    count = 0
    for fname in os.listdir(TEMP_DIR):
        fpath = os.path.join(TEMP_DIR, fname)
        try:
            os.remove(fpath)
            count += 1
        except Exception:
            pass
    if count:
        print(f"🧹  Cleaned up {count} stale temp file(s) from {TEMP_DIR}")


def _free_disk_bytes() -> int:
    """Return free disk space in bytes for the TEMP_DIR partition."""
    stat = shutil.disk_usage(TEMP_DIR if os.path.isdir(TEMP_DIR) else tempfile.gettempdir())
    return stat.free


# ── Step 1: Resolve the landing page to a direct download URL ──
#
# downloadwella.com (and similar hosts used by the scrapers) serve a
# landing page. The real ?pt= download link is embedded in the page HTML.
# We replicate the exact same extraction logic that movie_detail.html uses
# on the client side, but here in Python so the server can start the
# download immediately.

def resolve_direct_link(landing_url: str, session: requests.Session) -> str | None:
    """
    Follow a downloadwella.com (or similar) landing page and return
    the actual direct download URL.

    Strategy:
      1. Return immediately if already a direct/resolved link
      2. Fetch landing page with cloudscraper (bypasses bot detection)
         and parse the HTML for the ?pt= token link using multiple patterns
      3. Follow redirects — downloadwella sometimes redirects directly
         to the file after a short delay

    NOTE: We intentionally skip calling the Django website resolver
    (watch2d.org/movies/resolve-download/) because the Render free tier
    may be sleeping and cause a 15-second timeout every single movie.
    Cloudscraper on the raw landing page is faster and more reliable
    when running on a GitHub Actions / VPS server.

    Returns the resolved URL string, or None if resolution fails.
    """
    url_lower = landing_url.lower()
    direct_exts = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.zip', '.rar')

    # ── Already resolved ───────────────────────────────────────
    if '?pt=' in url_lower:
        return landing_url

    # sabishares.com/file/?preview → strip query = direct link
    try:
        u = urlparse(landing_url)
        if u.hostname and 'sabishares.com' in u.hostname and '/file/' in u.path and 'preview' in u.query:
            return f"{u.scheme}://{u.netloc}{u.path}"
    except Exception:
        pass

    # Plain direct file extension (not a landing page host). loadedfiles.'s
    # own file *pages* often end in .mkv/.html, so it must stay excluded here
    # too — otherwise its gate page gets mistaken for the file itself.
    is_ext = any(url_lower.endswith(e) or (e + '?') in url_lower for e in direct_exts)
    if (is_ext and 'downloadwella.com' not in url_lower
            and 'loadedfiles.' not in url_lower
            and 'sabishares.com/file/' not in url_lower):
        return landing_url

    # ── Host-specific resolvers ──────────────────────────────────
    # Generic pattern-matching (below) doesn't handle these two hosts'
    # actual flows — downloadwella needs a POST + follow-up parse,
    # loadedfiles needs a warmed session cookie and a multi-hop ?pt= token
    # chain. Reuse the exact same resolvers the website's own download gate
    # uses (movies/views.py), so scraper uploads and the bot's live-link
    # fallback resolve exactly as well as the site does — no HTTP round trip,
    # just a direct in-process import.
    host = urlparse(landing_url).netloc.lower()
    if 'downloadwella.com' in host:
        from movies.views import _resolve_downloadwella
        result, _ = _resolve_downloadwella(landing_url, urlparse(landing_url))
        return result

    if 'loadedfiles.' in host:
        from movies.views import _resolve_loadedfiles
        result, _ = _resolve_loadedfiles(landing_url, urlparse(landing_url))
        return result

    print(f"      🔗 Resolving landing page: {landing_url[:80]}…")

    # ── Fetch with cloudscraper (handles Cloudflare / bot checks) ──
    for attempt in range(2):
        try:
            scraper = cloudscraper.create_scraper(
                browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
            )
            resp = scraper.get(landing_url, timeout=25, allow_redirects=True)
            html  = resp.text
            final = resp.url

            # ── Check if the redirect chain led to a direct file ──
            if '?pt=' in final or any(final.lower().endswith(e) for e in direct_exts):
                print(f"      ✅ Resolved via redirect chain")
                return final

            # ── Pattern 1: href with ?pt= token ───────────────────
            # e.g. <a href="https://cdn.downloadwella.com/dl/file.mkv?pt=TOKEN">
            m = re.search(
                r'href=[\'"]?(https?://[^\'">\s]+\?pt=[^\'">\s]+)[\'"]?',
                html
            )
            if m:
                print(f"      ✅ Resolved via href ?pt= pattern")
                return m[1]

            # ── Pattern 2: jQuery .html() injection ───────────────
            # e.g. $(...).html('...<a href="URL?pt=TOKEN">...')
            m = re.search(
                r'\.html\s*\(\s*["\'].*?href=[\'"](https?://[^\'">\s]+\?pt=[^\'">\s]+)[\'"]',
                html, re.DOTALL
            )
            if m:
                print(f"      ✅ Resolved via jQuery html() pattern")
                return m[1]

            # ── Pattern 3: JS string assignment with ?pt= ─────────
            # e.g. var url = "https://...?pt=TOKEN";
            m = re.search(
                r'[=:(,\s][\'\"](https?://[^\'">\s]+\?pt=[^\'">\s]+)[\'"]',
                html
            )
            if m:
                print(f"      ✅ Resolved via JS variable pattern")
                return m[1]

            # ── Pattern 4: location.href = download path ──────────
            m = re.search(
                r'location\.href\s*=\s*[\'"]((https?://)[^\'"]{20,})[\'"]',
                html
            )
            if m and re.search(r'/dl/|\.mkv|\.mp4|\.avi|\.zip', m[1], re.IGNORECASE):
                print(f"      ✅ Resolved via location.href pattern")
                return m[1]

            # ── Pattern 5: data-url or data-link attributes ───────
            m = re.search(
                r'data-(?:url|link|href|src)=[\'"]?(https?://[^\'">\s]+\?pt=[^\'">\s]+)[\'"]?',
                html
            )
            if m:
                print(f"      ✅ Resolved via data-attribute pattern")
                return m[1]

            # ── Pattern 6: raw direct file URL anywhere in page ───
            m = re.search(
                r'[\'\"](https?://[^\'"?\s]{10,}\.(?:mp4|mkv|webm|avi|zip|rar))[\'"]',
                html, re.IGNORECASE
            )
            if m:
                print(f"      ✅ Resolved via raw file URL pattern")
                return m[1]

            # ── Pattern 7: base64-encoded URL (some hosts encode it) ─
            b64_matches = re.findall(r'[\'\""]([A-Za-z0-9+/]{40,}={0,2})[\'\""]', html)
            for b64 in b64_matches:
                try:
                    decoded = base64.b64decode(b64 + '==').decode('utf-8', errors='ignore')
                    if decoded.startswith('http') and ('?pt=' in decoded or
                            any(decoded.lower().endswith(e) for e in direct_exts)):
                        print(f"      ✅ Resolved via base64 decode")
                        return decoded
                except Exception:
                    continue

            # If first attempt got nothing useful, wait briefly and retry
            # (some pages load the link after a JS timer)
            if attempt == 0:
                print(f"      ⏳ No link found yet — retrying after 3s…")
                time.sleep(3)
                continue
            break

        except Exception as e:
            print(f"      ⚠️  Cloudscraper fetch failed (attempt {attempt+1}): {e}")
            if attempt == 0:
                time.sleep(2)
                continue
            break

    print(f"      ❌ Could not resolve direct link from: {landing_url[:80]}")
    return None


# ── Step 2: Check file size via HEAD request ────────────────────

def _get_file_size(direct_url: str, session: requests.Session) -> int | None:
    """
    Return Content-Length in bytes, or None if unknown.
    Uses a HEAD request so no data is downloaded.
    """
    try:
        resp = session.head(direct_url, timeout=10, allow_redirects=True)
        cl = resp.headers.get('Content-Length') or resp.headers.get('content-length')
        if cl:
            return int(cl)
    except Exception as e:
        print(f"      ⚠️  HEAD request failed: {e}")
    return None


def _human_bytes(n: int) -> str:
    if n >= 1_073_741_824: return f"{n/1_073_741_824:.1f} GB"
    if n >= 1_048_576:     return f"{n/1_048_576:.1f} MB"
    if n >= 1024:          return f"{n/1024:.1f} KB"
    return f"{n} B"


# ── Step 3: Stream download to temp file ───────────────────────

def download_to_temp(direct_url: str, filename: str, session: requests.Session) -> str | None:
    """
    Stream-download direct_url to TEMP_DIR/filename.
    Shows a simple progress indicator.
    Returns the local file path on success, or None on failure.
    """
    _ensure_temp_dir()
    dest = os.path.join(TEMP_DIR, filename)

    # Safety: check free disk space
    free = _free_disk_bytes()
    if free < MIN_FREE_BYTES:
        print(f"      ⚠️  Only {_human_bytes(free)} free disk space — skipping download")
        return None

    print(f"      ⬇️  Downloading → {filename}")
    try:
        with session.get(direct_url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            total     = int(resp.headers.get('Content-Length', 0))
            received  = 0
            last_pct  = -1

            with open(dest, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):  # 1 MB chunks
                    if chunk:
                        f.write(chunk)
                        received += len(chunk)
                        if total:
                            pct = int(received / total * 100)
                            if pct != last_pct and pct % 10 == 0:
                                print(f"      📥  {pct}%  ({_human_bytes(received)} / {_human_bytes(total)})")
                                last_pct = pct

        actual_size = os.path.getsize(dest)
        print(f"      ✅ Download complete: {_human_bytes(actual_size)}")
        return dest

    except Exception as e:
        print(f"      ❌ Download failed: {e}")
        # Clean up partial file
        if os.path.exists(dest):
            os.remove(dest)
        return None


# ── Step 4: Build caption for private channel ──────────────────

def _build_upload_caption(movie) -> str:
    """
    Build the caption that appears with the uploaded file in the private
    channel. Plain text, no parse_mode — this channel is an internal
    archive, not user-facing polish, so formatting isn't worth the extra
    surface area. Telegram still auto-links the plain URL.
    """
    from django.conf import settings as _s
    site_url  = getattr(_s, 'SITE_URL', 'https://watch2d.org').rstrip('/')
    movie_url = f"{site_url}/movie/{movie.pk}/{movie.slug}/"

    lines = [
        f"🎬  {movie.title}",
        "",
    ]

    if getattr(movie, 'vi_year', ''):
        lines.append(f"📅  Year: {movie.vi_year}")
    if getattr(movie, 'vi_language', ''):
        lines.append(f"🗣  Language: {movie.vi_language}")
    if getattr(movie, 'vi_runtime', ''):
        lines.append(f"⏱  Runtime: {movie.vi_runtime}")
    if getattr(movie, 'vi_filesize', ''):
        lines.append(f"💾  Size: {movie.vi_filesize}")

    try:
        cats = movie.categories.all()
        if cats:
            lines.append(f"🏷  Genre: {', '.join(c.name for c in cats[:3])}")
    except Exception:
        pass

    lines += [
        "",
        f"🌍  {movie_url}",
    ]

    return "\n".join(lines)


# ── Step 5: Upload via Telethon ────────────────────────────────

def upload_file_to_private_channel(movie, file_path: str) -> int | None:
    """
    Upload file_path to the private Telegram channel using Telethon.
    Returns the sent message's id on success (the bot copyMessage()s this id
    to deliver the file to a requesting user later), or None on failure.

    Telethon is used (not aiogram) because:
      • Bot API hard-limits uploads to 50 MB
      • Telethon (userbot) supports up to 2 GB via MTProto
    """
    try:
        from django.conf import settings as _s
        from telethon.sync import TelegramClient
        from telethon.tl.types import DocumentAttributeFilename

        api_id       = getattr(_s, 'TELETHON_API_ID', None)
        api_hash     = getattr(_s, 'TELETHON_API_HASH', None)
        session_name = getattr(_s, 'TELETHON_SESSION_NAME', 'uploader')
        channel      = getattr(_s, 'TELETHON_PRIVATE_CHANNEL', None)

        if not all([api_id, api_hash, channel]):
            print("      ⚠️  Telethon credentials not configured in settings — skipping upload")
            return None

        caption  = _build_upload_caption(movie)
        filename = os.path.basename(file_path)
        filesize = os.path.getsize(file_path)

        print(f"      📤  Uploading to private channel: {filename} ({_human_bytes(filesize)})")

        # Progress callback shown every 10%
        _last_pct = [-1]
        def _progress(current, total):
            pct = int(current / total * 100)
            if pct != _last_pct[0] and pct % 10 == 0:
                print(f"      📤  {pct}%  ({_human_bytes(current)} / {_human_bytes(total)})")
                _last_pct[0] = pct

        with TelegramClient(session_name, api_id, api_hash) as client:
            sent = client.send_file(
                channel,
                file_path,
                caption          = caption,
                progress_callback= _progress,
                attributes       = [DocumentAttributeFilename(file_name=filename)],
                # Force sending as a document (not compressed video)
                # so the receiver gets the original file quality
                force_document   = True,
            )

        print(f"      ✅  Uploaded to private channel! (message id {sent.id})")
        return sent.id

    except ImportError:
        print("      ❌  Telethon not installed.  Run:  pip install telethon")
        return None
    except Exception as e:
        print(f"      ❌  Telethon upload failed: {e}")
        return None


# ── Master upload function — called from the scraper loop ──────

def upload_movie_file(movie, landing_url: str, http_session: requests.Session) -> int | None:
    """
    Full pipeline:
      resolve link → check size → download → upload → delete temp file

    Returns the uploaded message id on success (None on any failure/skip).
    Called only for newly created movies (created=True in the scraper loop).
    """
    temp_file = None
    try:
        # Step 1: Resolve landing page → direct download URL
        direct_url = resolve_direct_link(landing_url, http_session)
        if not direct_url:
            print(f"      ⛔ Upload skipped — could not resolve direct link")
            return None

        # Step 2: Check file size (HEAD request — no data used)
        size = _get_file_size(direct_url, http_session)
        if size is not None:
            print(f"      📏  File size: {_human_bytes(size)}")
            if size > MAX_UPLOAD_BYTES:
                print(f"      ⛔ Upload skipped — file too large "
                      f"({_human_bytes(size)} > {_human_bytes(MAX_UPLOAD_BYTES)})")
                return None
        else:
            print(f"      📏  File size unknown — will attempt download")

        # Step 3: Build a safe filename from the URL
        raw_name = direct_url.split('/')[-1].split('?')[0]
        # Strip any THENKIRI.COM / naijadeleyss watermarks from filename
        safe_name = re.sub(r'\.(THENKIRI\.COM|DOWNLOADED\.FROM\.[^.]+)\b', '',
                           raw_name, flags=re.IGNORECASE)
        safe_name = safe_name.strip('.') or f"movie_{movie.pk}.mkv"

        # Step 4: Download to server temp folder
        temp_file = download_to_temp(direct_url, safe_name, http_session)
        if not temp_file:
            return None

        # Step 5: Upload to private Telegram channel
        return upload_file_to_private_channel(movie, temp_file)

    finally:
        # Always delete the temp file — even if upload fails
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
                print(f"      🗑️  Temp file deleted")
            except Exception as e:
                print(f"      ⚠️  Could not delete temp file: {e}")


# ── One-time interactive Telethon login (run manually, not in CI) ──

def run_telethon_login():
    """
    One-time interactive login. Run this ONCE on the server:
        python manage.py scrape_thenkiri --telethon-login
        (or scrape_9jarocks --telethon-login — same account/session)

    It will ask for your phone number and the code Telegram sends you.
    After that, a .session file is saved and all future --upload-files
    runs are fully automatic — no phone needed again.
    """
    try:
        from telethon.sync import TelegramClient
        from django.conf import settings as _s

        api_id       = getattr(_s, 'TELETHON_API_ID', None)
        api_hash     = getattr(_s, 'TELETHON_API_HASH', None)
        session_name = getattr(_s, 'TELETHON_SESSION_NAME', 'uploader')

        if not all([api_id, api_hash]):
            print("\n❌  TELETHON_API_ID and TELETHON_API_HASH must be set in settings.py")
            print("    Get them from: https://my.telegram.org → API development tools")
            return

        print("\n📱  Telethon one-time login")
        print("    You will be asked for your phone number and the code Telegram sends you.")
        print(f"    Session will be saved as: {session_name}.session\n")

        with TelegramClient(session_name, api_id, api_hash) as client:
            me = client.get_me()
            print(f"\n✅  Logged in as: {me.first_name} (@{me.username})")
            print("    You can now run scrapers with --upload-files")

    except ImportError:
        print("❌  Telethon not installed.  Run:  pip install telethon")
    except Exception as e:
        print(f"❌  Login failed: {e}")
