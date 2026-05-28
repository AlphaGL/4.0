"""
scrape_nkiri_wp.py
==================
Django management command that scrapes naijavault.com and publishes
DIRECTLY to WordPress — zero DB interaction, zero social posting.

Usage:
    python manage.py scrape_nkiri_wp
    python manage.py scrape_nkiri_wp --startpage 3
    python manage.py scrape_nkiri_wp --startpage 1 --endpage 5
    python manage.py scrape_nkiri_wp --max-pages 10

Place this file at:
    <your_app>/management/commands/scrape_nkiri_wp.py
"""

from django.core.management.base import BaseCommand
import requests
from bs4 import BeautifulSoup
import re
import cloudscraper
from urllib.parse import urlparse, unquote
import time
import base64

# ══════════════════════════════════════════════════════════════
# SCRAPER CONSTANTS
# ══════════════════════════════════════════════════════════════

SOURCE_API_URL = 'https://naijavault.com/wp-json/wp/v2/posts/'

KNOWN_DOWNLOAD_DOMAINS = [
    'dl.downloadwella.com.ng', 'archive.org', 'mega.nz', 'drive.google.com',
    'mediafire.com', 'pixeldrain.com', 'terabox.com', 'onedrive.live.com',
    'downloadwella.com', 'netnaijafiles.xyz', 'loadedfiles.org',
    'sabishares.com', 'meetdownload.com', 'webloaded.com.ng', 'wideshares.org',
    'plutomovies.com', 'dl.plutomovies.com',
]

FILE_EXTENSIONS = ['.mp4', '.mkv', '.zip', '.rar', '.srt']

# In-memory WP category cache (name → ID) — avoids repeated API calls per run
_wp_category_cache: dict = {}


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    return unquote(f"{parsed.scheme}://{parsed.netloc}{parsed.path}").lower()


# ══════════════════════════════════════════════════════════════
# DOWNLOAD LINK EXTRACTOR
# ══════════════════════════════════════════════════════════════

def extract_real_download_link(url: str) -> str:
    """Follow intermediate pages (downloadwella etc.) to find the real file URL."""
    print(f"    🔍 Extracting real link from: {url}")
    try:
        if 'downloadwella.com' in url:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer":    "https://naijavault.com/",
                "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            try:
                scraper = cloudscraper.create_scraper()
                res = scraper.get(url, headers=headers, timeout=15)
                res.raise_for_status()
            except Exception:
                res = requests.get(url, headers=headers, timeout=15, verify=False)
                res.raise_for_status()

            soup = BeautifulSoup(res.text, 'html.parser')
            page_title = soup.find('title')
            if page_title:
                print(f"    📄 Page title: {page_title.get_text()}")

            # Try every selector in order
            for selector in [
                {'class_': 'bdpg-button'},
                {'id':     'download_link'},
                {'class_': 'download-btn'},
                {'class_': 'btn-download'},
                {'class_': 'download_button'},
                {'class_': 'button'},
                {'class_': 'btn'},
            ]:
                tag = soup.find('a', selector)
                if tag and tag.get('href'):
                    real_url = tag.get('href', '').split('?')[0]
                    print(f"    ✅ Real link found ({list(selector.values())[0]}): {real_url}")
                    return real_url

            all_links = soup.find_all('a', href=True)
            print(f"    🔍 Found {len(all_links)} total links on page")

            for link in all_links:
                href = link.get('href', '')
                text = link.get_text().strip().lower()
                if 'downloadwella.com.ng' in href and any(ext in href for ext in ['.mkv', '.mp4', '.zip']):
                    print(f"    🎯 Direct file link: {text} -> {href}")
                    return href.split('?')[0]
                if any(d in href.lower() for d in ['mega.nz', 'mediafire.com', 'drive.google.com',
                                                    'archive.org', 'pixeldrain.com', 'terabox.com']):
                    print(f"    🎯 External download link: {text} -> {href}")
                    return href.split('?')[0]

            for link in all_links:
                href           = link.get('href', '')
                text           = link.get_text().strip().lower()
                parent_classes = ' '.join(link.parent.get('class', [])) if link.parent else ''
                if 'download' in text or 'download' in parent_classes:
                    if href and 'downloadwella.com' not in href.rstrip('/'):
                        if href.count('/') > 3:
                            print(f"    🎯 Download button link: {text} -> {href}")
                            return href.split('?')[0]

            print("    ⚠️ No real download link found — keeping original URL.")
    except Exception as e:
        print(f"    ⚠️ Error extracting link: {e}")

    return url


# ══════════════════════════════════════════════════════════════
# WORDPRESS API HELPERS
# ══════════════════════════════════════════════════════════════

def _get_wp_auth_header() -> dict:
    from django.conf import settings
    username = getattr(settings, 'WP_USERNAME', '')
    password = getattr(settings, 'WP_APP_PASSWORD', '')
    token    = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {
        'Authorization': f'Basic {token}',
        'Content-Type':  'application/json',
    }


def _get_wp_base_url() -> str:
    from django.conf import settings
    return getattr(settings, 'WP_SITE_URL', '').rstrip('/')


def _map_to_naijadeleys_category(cat_name: str) -> str:
    """
    Map ANY incoming category name to one of NaijaDeleys' fixed categories.
    Never creates new categories — unrecognised content goes to 'Foreign'.

    Your live categories:
        Adult (18+) | Foreign | Hollywood | Nollywood | TV Series |
        Korean | Anime | Animation | Music | Entertainment
    """
    c = cat_name.strip().lower()

    # Adult / 18+
    if any(x in c for x in ('adult', '18+', '18 plus', 'erotic', 'xxx')):
        return 'Adult (18+)'

    # Nollywood — must come before Hollywood/Foreign so Nigerian content wins
    if any(x in c for x in ('nollywood', 'nigerian', 'nigeria')):
        return 'Nollywood'

    # Hollywood (includes general "movie" when origin is Western/American)
    if any(x in c for x in ('hollywood', 'american movie', 'english movie')):
        return 'Hollywood'

    # TV Series (US/UK/general Western series)
    if any(x in c for x in ('tv series', 'television', 'web series', 'american series',
                             'english series', 'ongoing', 'completed series')):
        return 'TV Series'

    # Korean
    if any(x in c for x in ('korean', 'kdrama', 'k-drama', 'korea')):
        return 'Korean'

    # Anime
    if any(x in c for x in ('anime', 'manga', 'japanese animation')):
        return 'Anime'

    # Animation / Cartoon
    if any(x in c for x in ('animation', 'cartoon', 'animated')):
        return 'Animation'

    # Music
    if any(x in c for x in ('music', 'audio', 'album', 'song')):
        return 'Music'

    # Entertainment (gossip, celeb news, etc.)
    if any(x in c for x in ('entertainment', 'celebrity', 'gossip', 'news')):
        return 'Entertainment'

    # Unrecognised — return '' so the caller can apply the right fallback
    # (Movie for standalone films, Drama for series)
    return ''


def _wp_get_or_create_category(cat_name: str, headers: dict, wp_base: str,
                               is_series: bool = False) -> int | None:
    """
    Resolve cat_name → NaijaDeleys fixed category → WP category ID.
    NEVER creates a new category on the target site.

    Fallback when category is unrecognised:
      - Series → 'Drama'
      - Movie  → 'Movie'
    """
    mapped = _map_to_naijadeleys_category(cat_name)
    if not mapped:
        mapped = 'Drama' if is_series else 'Movie'
        print(f"    ℹ️  '{cat_name}' unrecognised → fallback to '{mapped}'")
    key    = mapped.strip().lower()

    if key in _wp_category_cache:
        return _wp_category_cache[key]

    try:
        r = requests.get(
            f'{wp_base}/wp-json/wp/v2/categories',
            params={'search': mapped, 'per_page': 20},
            headers=headers, timeout=10,
        )
        if r.status_code == 200:
            for cat in r.json():
                if cat['name'].strip().lower() == key:
                    _wp_category_cache[key] = cat['id']
                    print(f"    📁 Category mapped: '{cat_name}' → '{mapped}' (ID {cat['id']})")
                    return cat['id']
        print(f"    ⚠️ Category '{mapped}' not found on WP site — skipping (will NOT create).")
    except Exception as e:
        print(f"    ⚠️ WP category error ({mapped}): {e}")
    return None


def _wp_upload_image(image_url: str, title: str, headers: dict, wp_base: str) -> int | None:
    """Download image from image_url and upload it to the WP media library."""
    try:
        img_resp = requests.get(image_url, timeout=20, stream=True)
        if img_resp.status_code != 200:
            print(f"    ⚠️ Image download failed: HTTP {img_resp.status_code}")
            return None

        content_type = img_resp.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip()
        ext_map      = {'image/jpeg': 'jpg', 'image/jpg': 'jpg',
                        'image/png': 'png', 'image/webp': 'webp', 'image/gif': 'gif'}
        ext      = ext_map.get(content_type, 'jpg')
        filename = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-') + f'.{ext}'

        upload_headers = {**headers,
                          'Content-Type':        content_type,
                          'Content-Disposition': f'attachment; filename="{filename}"'}
        r = requests.post(
            f'{wp_base}/wp-json/wp/v2/media',
            headers=upload_headers,
            data=img_resp.content,
            timeout=30,
        )
        if r.status_code == 201:
            media_id = r.json().get('id')
            print(f"    🖼️  WP image uploaded → ID {media_id}")
            return media_id
        else:
            print(f"    ⚠️ WP image upload failed: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"    ⚠️ WP image upload error: {e}")
    return None


def _wp_find_existing_post(title: str, headers: dict, wp_base: str) -> dict | None:
    """
    Search WordPress for an existing post that belongs to this series/movie.

    Matching strategy (in order):
    1. Exact rendered-title match  (e.g. re-running the exact same scrape)
    2. Starts-with match           (e.g. stored title is "Show S01 (Episode 5 Added) | Tv Series"
                                    and we're scraping "Show S01" with episode 8 — same post)

    The `title` argument is the *base* title produced by clean_title_parts,
    e.g. "Tyler Perry's Zatima S04" or "Avatar (2009)".
    """
    # Strip (Complete)/(Completed) so bare season base is used for lookup
    search_title = re.sub(r'\s*\(Complet(?:e|ed)\)\s*$', '', title, flags=re.IGNORECASE).strip()

    try:
        r = requests.get(
            f'{wp_base}/wp-json/wp/v2/posts',
            params={'search': search_title, 'per_page': 10, 'status': 'any'},
            headers=headers, timeout=10,
        )
        if r.status_code != 200:
            return None

        search_lower = search_title.strip().lower()
        title_lower  = title.strip().lower()
        for post in r.json():
            rendered = BeautifulSoup(
                post['title']['rendered'], 'html.parser'
            ).get_text().strip().lower()

            # Exact match (full or stripped)
            if rendered in (title_lower, search_lower):
                print(f"    🔎 WP duplicate found (exact): {post['title']['rendered']}")
                return post

            # Starts-with match — stored title begins with our base title
            # e.g. stored = "zatima s04 (episode 14 added) | tv series"
            #      base   = "tyler perry's zatima s04"
            if rendered.startswith(search_lower):
                print(f"    🔎 WP duplicate found (prefix): {post['title']['rendered']}")
                return post

    except Exception as e:
        print(f"    ⚠️ WP search error: {e}")
    return None


def _build_wp_content(title: str, title_b: str, description: str,
                      meta_info: dict, image_url: str, video_url: str,
                      download_links: list, is_series: bool) -> str:
    """
    Build the HTML body for a WordPress post that exactly matches the
    NaijaDeleys manual-post design (Jannah theme).

    Layout — identical to what is produced when posting manually:
      1.  DOWNLOAD heading  (bold, e.g. "DOWNLOAD The Boroughs Season 1 (2026) Complete | Free DOWNLOAD Mp4")
      2.  Description paragraph(s)
      3.  VIDEO INFORMATION  blockquote-style card  (Filesize · Duration · IMDb · Title · Year …)
      4.  TRAILER / WATCH heading  +  YouTube iframe embed
      5.  Download buttons:
            Series → one green-outlined "EPISODE N" button per link
            Movie  → one green-outlined "DOWNLOAD HERE" button
      6.  VLC / MX Player tip box  (yellow-bordered, matches manual style)
      7.  Hidden SEO keyword paragraph

    NOTE: WhatsApp/Telegram channel buttons, the Notice box, and the
    FAST DOWNLOAD button are all injected automatically by WordPress
    (Ad Inserter / theme hooks) — they must NOT be added here.
    """
    parts = []

    # ── 0. FEATURED IMAGE inline (top of post body) ──────────────────────
    # Embed the poster directly in content so it appears right at the top,
    # below any Ad Inserter buttons, matching the screenshot layout.
    if image_url:
        _img_alt = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()
        parts.append(
            f'<p style="text-align:center;">'
            f'<img decoding="async" src="{image_url}" alt="{_img_alt}" '
            f'style="max-width:100%;height:auto;border-radius:8px;" />'
            f'</p>'
        )

    # ── Pull metadata fields ─────────────────────────────────────────────
    year           = meta_info.get('year',           '').strip()
    genre          = meta_info.get('genre',          '').strip()
    country        = meta_info.get('country',        '').strip()
    lang           = meta_info.get('language',       '').strip()
    stars          = meta_info.get('stars',          '').strip()
    dur            = meta_info.get('duration',       '').strip()
    sub            = meta_info.get('subtitle',       '').strip()
    imdb           = meta_info.get('imdb',           '').strip()
    filesize       = meta_info.get('filesize',       '').strip()
    total_episodes = meta_info.get('total_episodes', '').strip()
    status         = meta_info.get('status',         '').strip()
    content_type   = meta_info.get('type',           'TV Series' if is_series else 'Movie').strip()

    # Clean title — strip trailing "(YYYY)" for display only
    _title_clean = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()

    # Build the human-readable "complete" suffix for series headings
    # e.g. "Season 1 (2026) Complete" or just "(2026)"
    _is_nollywood = any(
        x in (genre + ' ' + country).lower()
        for x in ('nollywood', 'nigerian', 'nigeria')
    )

    # ── 1. DOWNLOAD HEADING ──────────────────────────────────────────────
    # Series:  "DOWNLOAD The Boroughs Season 1 (2026) Complete | Free DOWNLOAD Mp4"
    # Movie:   "DOWNLOAD Omo Ghetto The Saga (2026) | Free DOWNLOAD Mp4"
    if is_series:
        # Detect "complete" flag from title or status
        _is_complete = bool(
            re.search(r'\b(complete[d]?)\b', title, re.IGNORECASE)
            or (status and 'complete' in status.lower())
        )
        _complete_str = ' Complete' if _is_complete else ''
        _yr_str       = f' ({year})'  if year else ''
        dl_heading    = f'DOWNLOAD {_title_clean}{_yr_str}{_complete_str} | Free DOWNLOAD Mp4'
    else:
        # For movies: always add year in parens unless it's already part of the clean title
        _yr_str    = f' ({year})' if year else ''
        dl_heading = f'DOWNLOAD {_title_clean}{_yr_str} | Free DOWNLOAD Mp4'

    parts.append(f'<p><strong>{dl_heading}</strong></p>')

    # ── 2. DESCRIPTION ───────────────────────────────────────────────────
    if description:
        # Nollywood movies often have a branded intro line; keep as-is.
        for para in description.split('\n'):
            para = para.strip()
            if para:
                parts.append(f'<p>{para}</p>')

    # ── 3. VIDEO INFORMATION CARD ────────────────────────────────────────
    # Rendered as a Jannah blockquote ("<p>" inside a <blockquote>) so it
    # gets the theme's native left-accent styling — exactly like the manual post.
    #
    # Field order matches the screenshots:
    #   Series: Filesize · Duration · Imdb · Title · Year · Type · Country · Language · Genre · Stars · Total Episodes · Status · Subtitle
    #   Movie:  Genre · Stars · Release Date · Runtime  (simpler card)

    if is_series:
        # Full metadata card — series style (The Boroughs screenshots)
        info_lines = []
        if filesize:        info_lines.append(f'Filesize: {filesize}')
        if dur:             info_lines.append(f'Duration: {dur}')
        if imdb:
            info_lines.append(f'Imdb: \u2013<a href="{imdb}" target="_blank" rel="nofollow noopener">{imdb}</a>')
        # Always include Title
        info_lines.append(f'Title: {_title_clean}')
        if year:            info_lines.append(f'Year: {year}')
        if content_type:    info_lines.append(f'Type: {content_type}')
        if country:         info_lines.append(f'Country: {country}')
        if lang:            info_lines.append(f'Language: {lang}')
        if genre:           info_lines.append(f'Genre: {genre}')
        if stars:           info_lines.append(f'Stars: {stars}')
        if total_episodes:  info_lines.append(f'Total Episodes: {total_episodes}')
        if status:          info_lines.append(f'Status:{status}')
        if sub:             info_lines.append(f'Subtitle: {sub}')
    else:
        # Compact card — movie style (Omo Ghetto screenshots)
        info_lines = []
        if genre:    info_lines.append(f'Genre: {genre}')
        if stars:    info_lines.append(f'Stars: {stars}')
        if year:     info_lines.append(f'Release Date: {year}')
        if dur:      info_lines.append(f'Runtime: {dur}')
        # Add remaining fields if present
        if country:  info_lines.append(f'Country: {country}')
        if lang:     info_lines.append(f'Language: {lang}')
        if sub:      info_lines.append(f'Subtitle: {sub}')
        if imdb:
            info_lines.append(f'Imdb: \u2013<a href="{imdb}" target="_blank" rel="nofollow noopener">{imdb}</a>')

    if info_lines:
        inner = '<br />\n'.join(info_lines)
        parts.append(f'<blockquote><p>{inner}</p></blockquote>')

    # ── 4. TRAILER / WATCH HEADING + EMBED ──────────────────────────────
    # Series use "TRAILER", movies use "WATCH" (matches screenshots exactly).
    if video_url:
        section_head = 'TRAILER' if is_series else 'WATCH'
        parts.append(f'<p><strong>{section_head}</strong></p>')

        # Convert a YouTube watch URL to embed URL if needed
        yt_match = re.search(
            r'(?:youtube\.com/watch\?v=|youtu\.be/)([\w\-]{11})', video_url
        )
        if yt_match:
            embed_url = f'https://www.youtube.com/embed/{yt_match.group(1)}'
        else:
            embed_url = video_url  # already an embed or non-YT source

        parts.append(
            f'<p><iframe class="BLOG_video_class" src="{embed_url}" '
            f'width="780" height="439" allowfullscreen="allowfullscreen"></iframe></p>'
        )

    # ── 5. VLC / MX PLAYER TIP ───────────────────────────────────────────
    # Exact HTML copied from your live manual posts (Omukade / The Boroughs).
    parts.append(
        '<div style="background:#fff9e6; border:2px solid #ffd700; padding:10px 12px; '
        'margin:15px 0; border-radius:12px; font-family:Arial; line-height:1.5; text-align:left;">'
        '<span style="color:#8b4513; font-weight:bold; font-size:14px;">Highly Recommended!</span> '
        '<span style="color:#ff0000; font-weight:bold; font-size:14px;">VLC or MX Player</span> '
        '<span style="color:#5d4037; font-size:14px;">use app to watch this video (no audio or video issues).</span><br />'
        '<span style="color:#5d4037; font-size:14px;">It Also supports subtitle if stated on the post (Subtitle: English).</span><br />'
        '<span style="color:#8b4513; font-weight:bold; font-size:14px;">How to download from this site &#8212;</span> '
        '<a href="https://t.me/naijadeleyschannel/8" '
        'style="color:#0056b3; font-weight:900; text-decoration:none; font-size:14px;">Click HERE!</a>'
        '</div>'
    )

    # ── 6. DOWNLOAD BUTTONS ───────────────────────────────────────────────
    # Exact HTML from your live manual posts:
    #   Series → individual green-outlined "EPISODE N" buttons (The Boroughs style)
    #   Movie  → single "DOWNLOAD HERE" green-outlined button (Omukade style)
    # Each button is wrapped in its own <div style="margin-bottom:8px;">
    if download_links:
        if is_series:
            # Series: one div per button, left-aligned block
            parts.append('<div style="text-align: left; font-family: Arial; margin-top: 10px;">')
            for i, dl in enumerate(download_links, 1):
                raw_label = dl.get('label', '').strip()
                url       = dl['url']
                ep_match  = re.search(r'episode\s*(\d+)', raw_label, re.IGNORECASE)
                btn_label = f'EPISODE {ep_match.group(1)}' if ep_match else f'EPISODE {i}'
                # Check if this is a ZIP link
                if 'zip' in url.lower() or 'zip' in raw_label.lower():
                    _season_match = re.search(r's(\d+)', title, re.IGNORECASE)
                    _season_num = _season_match.group(1) if _season_match else '1'
                    btn_label = f'DOWNLOAD ZIP SEASON {_season_num}'
                parts.append(
                    f'<div style="margin-bottom: 8px;">'
                    f'<a style="display: inline-flex; align-items: center; background: #fff; '
                    f'border: 3px solid #28a745; color: #28a745; padding: 6px 15px; '
                    f'text-decoration: none; font-weight: 900; border-radius: 6px; '
                    f'box-shadow: 0 3px 8px rgba(0,0,0,.5); text-transform: uppercase; font-size: 13px;" '
                    f'href="{url}">'
                    f'<img decoding="async" style="width: 16px; margin-right: 8px;" '
                    f'src="https://img.icons8.com/material-sharp/24/28a745/download.png" />'
                    f'{btn_label}</a>'
                    f'</div>'
                )
            parts.append('</div>')
        else:
            # Movie: single DOWNLOAD HERE button (Omukade style)
            url = download_links[0]['url']
            parts.append(
                '<div style="text-align:left; margin:10px 0 15px; font-family:Arial;">'
                f'<a href="{url}" '
                'style="display:inline-flex; align-items:center; background:#fff; '
                'border:3px solid #28a745; color:#28a745; padding:8px 18px; '
                'text-decoration:none; font-weight:900; border-radius:6px; '
                'box-shadow:0 3px 10px rgba(0,0,0,.5); text-transform:uppercase; font-size:14px;">'
                '<img decoding="async" '
                'src="https://img.icons8.com/material-sharp/24/28a745/download.png" '
                'style="width:18px; height:18px; margin-right:10px;">'
                '  DOWNLOAD HERE'
                '</a>'
                '</div>'
            )
            # If there are additional links beyond the first, render them too
            for dl in download_links[1:]:
                url = dl['url']
                parts.append(
                    '<div style="text-align:left; margin:10px 0 15px; font-family:Arial;">'
                    f'<a href="{url}" '
                    'style="display:inline-flex; align-items:center; background:#fff; '
                    'border:3px solid #28a745; color:#28a745; padding:8px 18px; '
                    'text-decoration:none; font-weight:900; border-radius:6px; '
                    'box-shadow:0 3px 10px rgba(0,0,0,.5); text-transform:uppercase; font-size:14px;">'
                    '<img decoding="async" '
                    'src="https://img.icons8.com/material-sharp/24/28a745/download.png" '
                    'style="width:18px; height:18px; margin-right:10px;">'
                    '  DOWNLOAD HERE'
                    '</a>'
                    '</div>'
                )

    # ── 7. SEO KEYWORD PARAGRAPH (hidden) ────────────────────────────────
    _title_no_yr = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()
    yr_str       = f' ({year})' if year else ''
    base_yr      = f'{_title_no_yr}{yr_str}'

    ep_label = ''
    if title_b and is_series:
        ep_m = re.search(r'(episode\s*\d+)', title_b, re.IGNORECASE)
        if ep_m:
            ep_label = ep_m.group(1).title()

    season_label = ''
    if is_series:
        s_m = re.search(r'\bS(\d{1,2})\b', title, re.IGNORECASE)
        if s_m:
            season_label = f'Season {int(s_m.group(1))}'
        else:
            s_m2 = re.search(r'Season\s*(\d{1,2})', title, re.IGNORECASE)
            if s_m2:
                season_label = f'Season {int(s_m2.group(1))}'

    seas_str = f' {season_label}' if season_label else ''
    ep_str   = f' {ep_label}'     if ep_label     else ''

    if is_series:
        seo_text = (
            f'Download {base_yr}{seas_str}{ep_str} mp4 mkv, '
            f'latest Tv Series {base_yr}{seas_str} 720p 480p, '
            f'{base_yr}{seas_str}{ep_str} Tv Series Download.'
        )
    elif _is_nollywood:
        seo_text = (
            f'Download {base_yr} mp4 mkv, '
            f'latest Nollywood movie {base_yr} 720p 480p, '
            f'{base_yr} Nollywood movie Download.'
        )
    else:
        seo_text = (
            f'Download {base_yr} mp4 mkv, '
            f'latest Hollywood movie {base_yr} 720p 480p, '
            f'{base_yr} Hollywood movie Download.'
        )

    parts.append(
        f'<p style="font-size:1px;color:transparent;line-height:1;'
        f'margin:0;padding:0;pointer-events:none;" aria-hidden="true">'
        f'{seo_text}</p>'
    )

    return '\n'.join(parts)



def _make_slug(text: str, is_series: bool = False) -> str:
    """
    Convert a title into a clean WordPress-style slug.

    For SERIES — slug stops at the season identifier (SXX / Season X).
    Episode info is NEVER included in the slug (preserves SEO on updates).
      e.g. "Tyler Perry's Zatima S04 (Episode 16 Added)"  -> "tyler-perrys-zatima-s04"
      e.g. "Tell Me Lies S03 (Episode 8 Added)"           -> "tell-me-lies-s03"

    For MOVIES — use the full title (year included, no episode noise).
      e.g. "Hellfire (2026)"  -> "hellfire-2026"
    """
    import unicodedata

    if is_series:
        # Strip everything from the first episode/complete marker onward
        # Handles: "(Episode N Added)", "(Complete)", "(Completed)", "Episode N Added"
        text = re.sub(
            r'\s*[\(\[]?\s*(?:episode\s*\d+\s*(?:added)?|complete[d]?)\s*[\)\]]?.*$',
            '', text, flags=re.IGNORECASE,
        ).strip()
        # Also strip a trailing pipe-category suffix e.g. "| Tv Series"
        text = re.sub(r'\s*\|.*$', '', text).strip()

    text = unicodedata.normalize('NFKD', text)
    text = text.encode('ascii', 'ignore').decode('ascii')
    text = text.lower()
    # Drop apostrophes so "Perry's" -> "perrys" not "perry-s"
    text = re.sub(r"[`']+", '', text)
    # Everything non-alphanumeric -> hyphen
    text = re.sub(r'[^a-z0-9]+', '-', text)
    text = text.strip('-')
    return text

def _build_rank_math_seo(title: str, title_b: str, description: str,
                         meta_info: dict, categories: list, is_series: bool) -> dict:
    """
    Build Rank Math SEO fields:
      - rank_math_focus_keyword  → the primary keyword to rank for
      - rank_math_description    → SEO meta description (rich, keyword-stuffed per your templates)
      - rank_math_title          → SEO title tag

    Returns a dict ready to drop into post_data['meta'].
    """
    year    = meta_info.get('year', '')
    country = meta_info.get('country', '').strip()
    genre   = meta_info.get('genre', '').strip()

    # Detect drama type from categories
    cat_lower = ' '.join(c.lower() for c in categories)
    if 'korean' in cat_lower or 'kdrama' in cat_lower:
        drama_type = 'Korean'
    elif 'thai' in cat_lower:
        drama_type = 'Thai'
    elif 'chinese' in cat_lower:
        drama_type = 'Chinese'
    elif 'japanese' in cat_lower:
        drama_type = 'Japanese'
    elif 'bollywood' in cat_lower or 'indian' in cat_lower:
        drama_type = 'Indian'
    else:
        drama_type = country if country else ''

    is_nollywood  = any(x in cat_lower for x in ('nollywood', 'nigerian'))
    is_anime      = 'anime' in cat_lower
    is_drama      = drama_type and not is_anime and is_series
    is_completed  = any(x in title.lower() for x in ('complete', 'completed'))
    ep_num        = ''
    ep_match      = re.search(r'episode\s*(\d+)', title_b, re.IGNORECASE)
    if ep_match:
        ep_num = ep_match.group(1)

    # ── FOCUS KEYWORD ──────────────────────────────────────────────────
    if is_anime and is_series:
        focus_kw = f'Download {title} Episode {ep_num} Anime' if ep_num else f'Download {title} Anime'
    elif is_anime and not is_series:
        focus_kw = f'Download {title} ({year}) Anime Movie' if year else f'Download {title} Anime'
    elif is_series and is_drama and is_completed:
        focus_kw = f'Download {title} Complete {drama_type} Drama'
    elif is_series and is_drama:
        focus_kw = f'Download {title} Episode {ep_num} {drama_type} Drama' if ep_num else f'Download {title} {drama_type} Drama'
    elif is_series and is_completed:
        focus_kw = f'Download {title} Season Complete Series'
    elif is_series:
        focus_kw = f'{title} Episode {ep_num} Download' if ep_num else f'{title} Season Download'
    elif is_nollywood:
        focus_kw = f'Download {title} ({year}) Nollywood Movie' if year else f'Download {title} Nollywood Movie'
    else:
        focus_kw = f'Download {title} ({year}) Movie' if year else f'Download {title} Movie'

    # ── SEO TITLE ──────────────────────────────────────────────────────
    if is_series and title_b:
        seo_title = f'{title} ({title_b}) - NaijaDeleys'
    elif year and f'({year})' not in title:
        seo_title = f'{title} ({year}) - NaijaDeleys'
    else:
        seo_title = f'{title} - NaijaDeleys'

    # ── META DESCRIPTION (keyword-rich, matches your templates) ────────
    if is_anime and is_series:
        desc = (
            f'{title} Season, {title} {year} episode {ep_num} Anime download, '
            f'Download {title} Episode {ep_num} Anime, '
            f'Download {title} in 480p Mkv Mp4, '
            f'{title} Episode {ep_num} Anime Download, '
            f'DOWNLOAD {title} ({year}) | Free DOWNLOAD Mp4, '
            f'{title} ({title_b}) | Mp4 Mkv DOWNLOAD, '
            f'DOWNLOAD {title} Episode {ep_num} Anime For FREE In 480p, 720p, 4K'
        )
    elif is_series and is_drama and is_completed:
        ep_range = meta_info.get('total_episodes', '')
        ep_range_str = f'1 - {ep_range}' if ep_range else 'complete'
        desc = (
            f'{title}, {title} {year} episode {ep_range_str} {drama_type} series download, '
            f'Download {title} complete episodes, '
            f'Download {title} {drama_type} drama in 480p Mkv Mp4, '
            f'Download {title} complete series English sub, '
            f'DOWNLOAD {title} ({year}) (Complete) | Free DOWNLOAD Mp4, '
            f'{title} (Complete) | Mp4 Mkv DOWNLOAD, '
            f'DOWNLOAD {title} Complete {drama_type} Drama For FREE In 480p, 720p, 1080p, x264 x265'
        )
    elif is_series and is_drama:
        desc = (
            f'{title}, {title} {year} episode {ep_num} {drama_type} series download, '
            f'Download {title} Episode {ep_num}, '
            f'Download {title} {drama_type} drama in 480p Mkv Mp4, '
            f'Download {title} Episode {ep_num} English sub, '
            f'DOWNLOAD {title} ({year}) | Free DOWNLOAD Mp4, '
            f'{title} ({title_b}) | Mp4 Mkv DOWNLOAD, '
            f'DOWNLOAD {title} Episode {ep_num} {drama_type} Drama For FREE In 480p, 720p, 1080p'
        )
    elif is_series and is_completed:
        ep_range = meta_info.get('total_episodes', '')
        ep_range_str = f'Episode 1 - {ep_range} Complete' if ep_range else 'Complete'
        desc = (
            f'{title}, {title} {year} complete series download, '
            f'Download {title} {ep_range_str}, '
            f'Download {title} complete series in 480p Mkv Mp4, '
            f'DOWNLOAD {title} ({year}) Complete | Free DOWNLOAD Mp4, '
            f'{title} (Complete) | Mp4 Mkv DOWNLOAD, '
            f'DOWNLOAD {title} (Complete) TV Series For FREE In 480p, 720p, 1080p, x265 x264'
        )
    elif is_series:
        desc = (
            f'{title} Episode {ep_num}, {title} Episode {ep_num} {year} series download, '
            f'Download {title} Episode {ep_num}, '
            f'Download {title} tv series in 480p Mkv Mp4, '
            f'DOWNLOAD {title} ({title_b}) Tv Series | Free DOWNLOAD Mp4, '
            f'{title} ({title_b}) Series | Mp4 Mkv DOWNLOAD, '
            f'DOWNLOAD {title} Episode {ep_num} Tv Series For FREE In 480p, 720p, 1080p, x264 x265'
        )
    elif is_nollywood:
        desc = (
            f'{title} ({year}), Download {title} ({year}) Mp4 Mkv Nigerian Movie, '
            f'{title} ({year}) Nigerian Movie Download 480p, '
            f'Download {title} ({year}) Nollywood Movie, '
            f'Download {title} ({year}) Full Movie, '
            f'DOWNLOAD {title} ({year}) Nollywood Movie | Free DOWNLOAD, '
            f'{title} ({year}) Nollywood Movie | Mp4 Mkv DOWNLOAD, '
            f'Download {title} ({year}) Nollywood Movie For FREE In 480p, 720p, 1080p, x264 x265'
        )
    else:
        # Hollywood / general movie
        desc = (
            f'{title} ({year}), {title} ({year}) Movie Download, '
            f'Download {title} ({year}) Movie, '
            f'Download {title} ({year}) Movie in 480p 4K Mkv Mp4, '
            f'Download {title} ({year}) Movie For FREE In 480p, 720p, 1080p, x264 x265, '
            f'DOWNLOAD {title} ({year}) Movie | Free DOWNLOAD, '
            f'{title} ({year}) Movie | Mp4 Mkv DOWNLOAD'
        )

    # Trim to ~320 chars (Google shows ~160 but rich snippet can use more)
    if len(desc) > 320:
        desc = desc[:317] + '...'

    return {
        'rank_math_focus_keyword': focus_kw,
        'rank_math_description':   desc,
        'rank_math_title':         seo_title,
    }


def _post_to_wordpress(
    title: str,
    title_b: str,
    description: str,
    meta_info: dict,
    image_url: str,
    video_url: str,
    download_links: list,
    categories: list,        # list of category name strings
    is_series: bool,
) -> bool:
    """
    Create or update a WordPress post for this title.

    Title format:
      - Series:  "Tyler Perry's Zatima S04 (Episode 14 Added) | Download TV Series"
      - Movie:   "Orí: Rebirth (2025)"
      No pipe suffix on movies — categories handle grouping inside WordPress.

    Excerpt:
      - Series:  "Episode 14 Added"  (the episode badge)
      - Movie:   synopsis/description (used by Rank Math as meta description)

    Post format: always "video" (your theme uses it for the video icon/layout).

    - If WP already has a post whose title starts with `title`  → UPDATE
    - Otherwise                                                  → CREATE

    Returns True on success, False on failure.
    """
    try:
        headers = _get_wp_auth_header()
        wp_base = _get_wp_base_url()

        if not wp_base:
            print("    ⚠️ WP_SITE_URL not configured in settings — skipping WordPress.")
            return False

        content = _build_wp_content(
            title, title_b, description, meta_info, image_url, video_url, download_links, is_series
        )

        # ── Rank Math SEO meta ───────────────────────────────────────────
        rank_math_meta = _build_rank_math_seo(
            title, title_b, description, meta_info, categories, is_series
        )
        print(f"    🔑 Focus keyword: {rank_math_meta['rank_math_focus_keyword']}")

        # Resolve category IDs on target WP site
        cat_ids = []
        for cat_name in categories:
            cid = _wp_get_or_create_category(cat_name.strip(), headers, wp_base, is_series=is_series)
            if cid:
                cat_ids.append(cid)

        # ── Title & excerpt ──────────────────────────────────────────────
        # Series: "Show S01 (Episode 14 Added) | Download TV Series"
        # Movie:  "Movie Title (2025)"
        mapped_cat_name = _map_to_naijadeleys_category(categories[0]) if categories else ''
        if not mapped_cat_name:
            mapped_cat_name = 'Drama' if is_series else ''
        if is_series and title_b:
            pipe_suffix = f' | Download {mapped_cat_name}' if mapped_cat_name else ''
            full_title = f'{title} ({title_b}){pipe_suffix}'
        else:
            full_title = title

        # Excerpt: combine episode/complete badge + description
        _has_episode   = bool(re.search(r'episode\s*\d+', title_b or '', re.IGNORECASE))
        _is_comp_badge = bool(re.search(r'complet', title_b or '', re.IGNORECASE))
        if is_series and (_has_episode or _is_comp_badge):
            _badge = title_b.strip()
            excerpt_text = f"{_badge} — {description}" if description else _badge
        else:
            excerpt_text = description

        existing_post = _wp_find_existing_post(title, headers, wp_base)

        # ── UPDATE ────────────────────────────────────────────────────────
        if existing_post:
            post_id = existing_post['id']

            stored_rendered = BeautifulSoup(
                existing_post['title']['rendered'], 'html.parser'
            ).get_text().strip()

            title_changed = full_title.strip().lower() != stored_rendered.strip().lower()
            if title_changed:
                print(f"    🆕 Update: [{stored_rendered}] → [{full_title}]")

            patch: dict = {'content': content, 'meta': rank_math_meta}
            if title_changed:
                patch['title'] = full_title
                # ⚠️ NEVER change the slug on update — preserves SEO / Google indexing.
                # The slug was set correctly on first publish (season-level only).
                # Bump published date so post rises to top of the site
                from datetime import datetime, timezone
                now_utc = datetime.now(timezone.utc)
                patch['date']     = now_utc.strftime('%Y-%m-%dT%H:%M:%S')
                patch['date_gmt'] = now_utc.strftime('%Y-%m-%dT%H:%M:%S')
                print(f"    🔗 Slug preserved (no change) — SEO safe.")
                print(f"    ⏫ Bumping date to: {patch['date']}")
            if excerpt_text:
                patch['excerpt'] = excerpt_text
            if cat_ids:
                existing_cats = existing_post.get('categories', [])
                patch['categories'] = list(set(existing_cats + cat_ids))

            r = requests.post(
                f'{wp_base}/wp-json/wp/v2/posts/{post_id}',
                headers=headers,
                json=patch,
                timeout=15,
            )
            if r.status_code == 200:
                action = 'title+slug+date bumped' if title_changed else 'content only'
                print(f"    ✏️  WP updated ({action}, ID {post_id}) — {full_title}")
                return True
            else:
                print(f"    ⚠️ WP update failed: {r.status_code} {r.text[:150]}")
                return False

        # ── CREATE ────────────────────────────────────────────────────────
        post_data: dict = {
            'title':   full_title,
            'slug':    _make_slug(title, is_series=is_series),
            'content': content,
            'status':  'publish',
            'format':  'video',
            'excerpt': excerpt_text or '',
            'meta':    rank_math_meta,
        }
        if cat_ids:
            post_data['categories'] = cat_ids

        # Upload featured image to WP media library
        if image_url:
            media_id = _wp_upload_image(image_url, title, headers, wp_base)
            if media_id:
                post_data['featured_media'] = media_id

        r = requests.post(
            f'{wp_base}/wp-json/wp/v2/posts',
            headers=headers,
            json=post_data,
            timeout=20,
        )
        if r.status_code == 201:
            wp_id = r.json().get('id')
            print(f"    ✅ WP created (ID {wp_id}) — {full_title}")
            return True
        else:
            print(f"    ⚠️ WP create failed: {r.status_code} {r.text[:150]}")
            return False

    except Exception as e:
        print(f"    ⚠️ WordPress error: {e}")
        return False


# ══════════════════════════════════════════════════════════════
# DJANGO MANAGEMENT COMMAND
# ══════════════════════════════════════════════════════════════

class Command(BaseCommand):
    help = 'Scrape naijavault.com and publish directly to WordPress (no DB, no social media)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--startpage', type=int, default=1,
            help='Page to start scraping from (default: 1)',
        )
        parser.add_argument(
            '--endpage', type=int, default=None,
            help='Page to stop at (inclusive)',
        )
        parser.add_argument(
            '--max-pages', type=int, default=None,
            help='Maximum number of pages to scrape this run',
        )

    # ── title cleaning ──────────────────────────────────────────────────

    def clean_title_parts(self, raw_title: str):
        """
        Returns (title, title_b, is_series).

        - Series:  title = "Show Name S01", title_b = "Episode 5 Added"
        - Movie:   title = "Movie Name (2024)", title_b = ""

        The pipe-category suffix (e.g. "| Tv Series", "| Korean Series") is
        stripped BEFORE the regex runs so it never leaks into title_b.
        This prevents the double-bracket bug:
            BAD:  "Zatima S04 ((Episode 14 Added) | Tv Series) | Tv Series"
            GOOD: "Zatima S04 (Episode 14 Added) | Tv Series"
        """
        title       = re.sub(r'\s+', ' ', raw_title).strip()
        title_lower = title.lower()
        is_complete = 'complete' in title_lower or 'completed' in title_lower

        # ── 1. Peel off the trailing "| Category" suffix so the regex
        #       never swallows it into title_b.
        pipe_suffix = ''
        pipe_match  = re.search(r'\s*\|\s*[^|]+$', title)
        if pipe_match:
            pipe_suffix = pipe_match.group(0)          # e.g. ' | Tv Series'
            title       = title[:pipe_match.start()].strip()

        # ── 2. Detect series (SXX / Season X pattern)
        series_pat = re.compile(r'(?i)(.*?\b(S\d{1,2}|Season\s?\d{1,2}))[\s\-–:]*\s*(.*)')
        match      = series_pat.match(title)
        if match:
            base    = match.group(1).strip()
            title_b = re.sub(r'^\(|\)$', '', match.group(3)).strip()  # strip outer parens
            # Append Complete/Completed to base only if it's not already there
            # and not already in title_b (e.g. title_b = "Complete Season")
            if is_complete and 'complete' not in base.lower() and 'complete' not in title_b.lower():
                base += ' (Completed)' if 'completed' in title_lower else ' (Complete)'
            return base, title_b, True

        # ── 3. Movie with year
        movie_match = re.search(r'^(.*?\(\d{4}\))', title)
        if movie_match:
            return movie_match.group(1).strip(), '', False

        return title, '', False

    # ── main entry point ────────────────────────────────────────────────

    def handle(self, *args, **options):
        start_page = options['startpage']
        end_page   = options['endpage']
        max_pages  = options['max_pages']

        page               = start_page
        pages_scraped      = 0
        consecutive_errors = 0
        max_consecutive_errors = 5

        print(f"\n🚀 Starting WordPress scrape from page {start_page}")
        print("📝 Mode: WordPress only — no DB, no social media\n")
        if end_page:
            print(f"📄 Will stop at page {end_page}")
        if max_pages:
            print(f"📊 Max pages: {max_pages}")

        scraper = cloudscraper.create_scraper()
        api_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/114.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }

        while True:
            # ── stop conditions ─────────────────────────────────────────
            if end_page and page > end_page:
                print(f"\n✅ Reached end page {end_page}. Stopping.")
                break
            if max_pages and pages_scraped >= max_pages:
                print(f"\n✅ Scraped {max_pages} page(s). Stopping.")
                break

            # ── fetch one page from source API ──────────────────────────
            try:
                print(f"\n🌐 Fetching page {page}...")
                response = scraper.get(
                    SOURCE_API_URL,
                    params={'page': page, 'per_page': 10},
                    headers=api_headers,
                    timeout=15,
                )
                response.raise_for_status()
                items = response.json()
            except requests.exceptions.HTTPError as e:
                if response.status_code == 404:
                    print("✅ All pages processed (404 received). Done.")
                    break
                print(f"🔥 HTTP error on page {page}: {e}")
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    print("❌ Too many consecutive errors. Stopping.")
                    return
                time.sleep(5)
                continue
            except Exception as e:
                print(f"🔥 Request failed on page {page}: {e}")
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    print("❌ Too many consecutive errors. Stopping.")
                    return
                time.sleep(5)
                continue

            consecutive_errors = 0
            pages_scraped     += 1

            if not items:
                print("✅ No items returned. All done.")
                break

            # ── process each post on the page ───────────────────────────
            for item in items:
                raw_title = item.get('title', {}).get('rendered', '').strip()
                if not raw_title:
                    print("⚠️ Skipped: empty title.")
                    continue

                print(f"\n🎬 Processing: {raw_title}")
                title, title_b, is_series = self.clean_title_parts(raw_title)

                # Parse post content HTML
                content_html = item.get('content', {}).get('rendered', '')
                soup         = BeautifulSoup(content_html, 'html.parser')

                # ── Synopsis / description ───────────────────────────────
                description = ''
                synopsis_heading = soup.find(
                    lambda tag: tag.name in ('h2', 'h3')
                    and 'synopsis' in tag.get_text().lower()
                )
                if synopsis_heading:
                    for sibling in synopsis_heading.find_next_siblings():
                        if sibling.name in ('h2', 'h3', 'h4'):
                            break
                        if sibling.name == 'p':
                            text = sibling.get_text(separator=' ').strip()
                            if text and 'filesize' not in text.lower() and 'imdb' not in text.lower():
                                description = text
                                break
                if not description:
                    for p in soup.find_all('p'):
                        text = p.get_text(separator=' ').strip()
                        if text and 'filesize' not in text.lower() and 'imdb' not in text.lower():
                            description = text
                            break
                if not description:
                    description = BeautifulSoup(
                        item.get('excerpt', {}).get('rendered', ''), 'html.parser'
                    ).get_text().strip()

                # ── Movie/series metadata block ──────────────────────────
                # Source site embeds a <p> with lines like:
                #   Genre: Drama, Comedy\nStars: Actor 1, Actor 2\nYear: 2026\n…
                # We extract these for display in the info table and SEO tags.
                # Nothing is skipped — we keep ALL fields (filesize, duration,
                # imdb, status, subtitle, type, total_episodes …) so the info
                # card in _build_wp_content can display them just like a manual post.
                meta_info: dict = {}
                for p in soup.find_all('p'):
                    raw = p.get_text(separator='\n').strip()
                    if any(k in raw.lower() for k in ('genre', 'stars', 'filesize', 'title')):
                        for line in raw.splitlines():
                            if ':' not in line:
                                continue
                            key, _, val = line.partition(':')
                            key = key.strip().lower().replace(' ', '_')
                            val = val.strip(' \u2013-\u2014').strip()
                            if val:
                                meta_info[key] = val
                        break
                if meta_info:
                    print(f"📋 Meta: {meta_info}")

                # ── Video URL (trailer embed) ────────────────────────────
                video_url = ''
                iframe    = soup.find('iframe')
                if iframe and iframe.get('src'):
                    video_url = iframe['src']
                    print(f"🎥 Video URL: {video_url}")

                # Download links
                download_links = []
                print("🔗 Looking for download links...")
                for a in soup.find_all('a', href=True):
                    href       = a['href'].strip()
                    label      = ' '.join(a.stripped_strings).strip()
                    href_lower = href.lower()

                    if (any(domain in href_lower for domain in KNOWN_DOWNLOAD_DOMAINS)
                            or any(href_lower.endswith(ext) for ext in FILE_EXTENSIONS)
                            or 'dl' in href_lower):
                        print(f"🔍 Found: {label} -> {href}")
                        real = extract_real_download_link(href)
                        download_links.append({'url': real, 'label': label})

                if not download_links:
                    print(f"⛔ No download links found for: {title} — skipping.")
                    continue

                # Featured image — prefer the pre-resolved URL in the post JSON
                # (jetpack_featured_media_url) to avoid an extra API round-trip.
                image_url = item.get('jetpack_featured_media_url', '').strip()
                if not image_url:
                    image_url = item.get('meta', {}).get('fifu_image_url', '').strip()
                if not image_url:
                    media_id = item.get('featured_media')
                    if media_id:
                        try:
                            img_res = scraper.get(
                                f"https://naijavault.com/wp-json/wp/v2/media/{media_id}",
                                headers=api_headers, timeout=10,
                            )
                            img_res.raise_for_status()
                            image_url = img_res.json().get('source_url', '')
                        except Exception:
                            print("⚠️ Could not fetch featured image.")
                if image_url:
                    print(f"🖼️ Image: {image_url}")

                # Categories from source post
                categories = []
                for cat_id in item.get('categories', []):
                    try:
                        r = scraper.get(
                            f"https://naijavault.com/wp-json/wp/v2/categories/{cat_id}",
                            headers=api_headers, timeout=10,
                        )
                        r.raise_for_status()
                        cat_name = r.json().get('name', '').strip()
                        if cat_name:
                            categories.append(cat_name)
                            print(f"📁 Category: {cat_name}")
                    except Exception:
                        print(f"⚠️ Could not fetch category ID {cat_id}.")

                # ── Publish to WordPress ─────────────────────────────────
                _post_to_wordpress(
                    title=title,
                    title_b=title_b,
                    description=description,
                    meta_info=meta_info,
                    image_url=image_url,
                    video_url=video_url,
                    download_links=download_links,
                    categories=categories,
                    is_series=is_series,
                )

            page += 1

        print(f"\n🎉 Done! Scraped {pages_scraped} page(s) "
              f"(page {start_page} → {page - 1}).")