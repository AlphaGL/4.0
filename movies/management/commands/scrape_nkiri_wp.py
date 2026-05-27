"""
scrape_nkiri_wp.py
==================
Django management command that scrapes thenkiri.ng and publishes
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

SOURCE_API_URL = 'https://thenkiri.ng/wp-json/wp/v2/posts/'

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
                "Referer":    "https://thenkiri.ng/",
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

    # Everything else — Thai, Chinese, Japanese drama, Bollywood, Spanish,
    # South African, other foreign movies/series, etc.
    return 'Foreign'


def _wp_get_or_create_category(cat_name: str, headers: dict, wp_base: str) -> int | None:
    """
    Resolve cat_name → NaijaDeleys fixed category → WP category ID.
    NEVER creates a new category on the target site.
    """
    mapped = _map_to_naijadeleys_category(cat_name)
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
    try:
        r = requests.get(
            f'{wp_base}/wp-json/wp/v2/posts',
            params={'search': title, 'per_page': 10, 'status': 'any'},
            headers=headers, timeout=10,
        )
        if r.status_code != 200:
            return None

        title_lower = title.strip().lower()
        for post in r.json():
            rendered = BeautifulSoup(
                post['title']['rendered'], 'html.parser'
            ).get_text().strip().lower()

            # Exact match
            if rendered == title_lower:
                print(f"    🔎 WP duplicate found (exact): {post['title']['rendered']}")
                return post

            # Starts-with match — stored title begins with our base title
            # e.g. stored = "zatima s04 (episode 14 added) | tv series"
            #      base   = "tyler perry's zatima s04"
            if rendered.startswith(title_lower):
                print(f"    🔎 WP duplicate found (prefix): {post['title']['rendered']}")
                return post

    except Exception as e:
        print(f"    ⚠️ WP search error: {e}")
    return None


def _build_wp_content(title: str, title_b: str, description: str,
                      meta_info: dict, image_url: str, video_url: str,
                      download_links: list, is_series: bool) -> str:
    """
    Build the HTML body for the WordPress post.

    Layout:
      1. Featured image (poster) — shown at very top
      2. Video information block (emoji rows, dark card with left border)
      3. Trailer embed
      4. Episode badge (series only)
      5. Synopsis
      6. Download buttons (NaijaDeleys yellow)
      7. SEO keyword paragraph (hidden)
    """
    parts = []
    C_YELLOW = '#F9C300'   # NaijaDeleys brand yellow
    C_DARK   = '#141414'   # dark bg
    C_CARD   = '#1a1a1a'   # card bg
    C_LIGHT  = '#f5f5f5'   # light text
    C_MUTED  = '#aaaaaa'   # muted text
    C_RED    = '#e50914'   # accent red (synopsis heading only)
    C_BORDER = '#00c853'   # green left border on info card (like reference image)

    year    = meta_info.get('year', '')
    genre   = meta_info.get('genre', '')
    country = meta_info.get('country', '')

    # ── 1. Featured image at top ─────────────────────────────────────────
    if image_url:
        parts.append(
            f'<div style="text-align:center;margin:0 0 24px 0;">'
            f'<img src="{image_url}" alt="{title}" '
            f'style="max-width:100%;border-radius:10px;box-shadow:0 4px 20px rgba(0,0,0,0.6);" />'
            f'</div>'
        )

    # ── 2. Video Information block ───────────────────────────────────────
    # Emoji map for each field (like the reference images)
    INFO_FIELDS = [
        ('title',    '🎬', 'Title'),
        ('year',     '📅', 'Year'),
        ('genre',    '🎭', 'Genre'),
        ('duration', '⏱',  'Duration'),
        ('type',     '📺', 'Type'),
        ('country',  '🌍', 'Country'),
        ('stars',    '⭐', 'Stars'),
        ('language', '🗣',  'Language'),
        ('subtitle', '📝', 'Subtitle Language'),
        ('source',   '💿', 'Source'),
        ('imdb',     '🔗', 'IMDB'),
    ]
    # Always inject title & type into meta_info if not already there
    display_meta = dict(meta_info)
    if 'title' not in display_meta:
        display_meta['title'] = title
    if 'type' not in display_meta:
        display_meta['type'] = 'TV Series' if is_series else 'Movie'

    rows_html = ''
    for key, emoji, label in INFO_FIELDS:
        val = display_meta.get(key, '').strip()
        if not val:
            continue
        # IMDB — make it a clickable link
        if key == 'imdb':
            val_html = (
                f'<a href="{val}" target="_blank" rel="nofollow noopener" '
                f'style="color:{C_YELLOW};text-decoration:none;">{val}</a>'
            )
        else:
            val_html = f'<span style="color:{C_LIGHT};">{val}</span>'

        rows_html += (
            f'<div style="padding:7px 0;border-bottom:1px solid #2a2a2a;">'
            f'<span style="font-size:15px;">{emoji}</span> '
            f'<strong style="color:{C_MUTED};font-size:13px;">{label}:</strong> '
            f'{val_html}'
            f'</div>'
        )

    if rows_html:
        parts.append(
            f'<div style="background:{C_CARD};border-left:4px solid {C_BORDER};'
            f'border-radius:8px;padding:16px 20px;margin:0 0 24px 0;">'
            f'<h3 style="color:{C_YELLOW};font-size:13px;font-weight:700;'
            f'text-transform:uppercase;letter-spacing:1px;margin:0 0 12px 0;">'
            f'📋 VIDEO INFORMATION</h3>'
            + rows_html +
            '</div>'
        )

    # ── 3. Trailer embed ─────────────────────────────────────────────────
    if video_url:
        parts.append(
            '<div style="position:relative;padding-bottom:56.25%;height:0;'
            f'overflow:hidden;margin:0 0 24px 0;border-radius:10px;background:{C_DARK};">'
            f'<iframe src="{video_url}" frameborder="0" allowfullscreen loading="lazy" '
            'title="Official Trailer" '
            'style="position:absolute;top:0;left:0;width:100%;height:100%;border-radius:10px;">'
            '</iframe></div>'
        )

    # ── 4. Episode badge (series only) ───────────────────────────────────
    if title_b and is_series:
        parts.append(
            f'<div style="display:inline-flex;align-items:center;gap:8px;'
            f'background:{C_YELLOW};color:#000;padding:8px 18px;'
            f'border-radius:20px;font-weight:700;font-size:14px;'
            f'letter-spacing:.4px;margin:0 0 20px 0;">'
            f'<span>&#9654;</span> Now Available: {title_b}</div>'
        )

    # ── 5. Synopsis ──────────────────────────────────────────────────────
    if description:
        label = 'Series Synopsis' if is_series else 'Movie Synopsis'
        parts.append(
            f'<div style="background:{C_DARK};border-radius:10px;'
            f'padding:18px 20px;margin:0 0 20px 0;">'
            f'<h3 style="color:{C_RED};font-size:14px;font-weight:700;'
            f'text-transform:uppercase;letter-spacing:.8px;margin:0 0 10px 0;">'
            f'{label}</h3>'
            f'<p style="color:{C_LIGHT};font-size:15px;line-height:1.7;margin:0;">'
            f'{description}</p>'
            f'</div>'
        )

    # ── 6. VLC / MX Player recommendation box ───────────────────────────
    parts.append(
        '<div style="background:#fffbe6;border:2px solid #F9C300;border-radius:8px;'
        'padding:14px 18px;margin:0 0 16px 0;font-size:14px;line-height:1.7;color:#222;">'
        '<strong style="color:#cc0000;">Highly Recommended!</strong> '
        '<strong style="color:#F9C300;background:#222;padding:1px 6px;border-radius:4px;">VLC or MX Player</strong>'
        ' app to watch this video (no audio or video issues).<br>'
        'It Also supports subtitle if stated on the post (Subtitle: English).<br>'
        '<strong style="color:#cc0000;">How to download from this site —</strong> '
        '<a href="https://t.me/naijadeleyschannel/8" target="_blank" rel="noopener" '
        'style="color:#1a73e8;font-weight:700;text-decoration:none;">Click HERE!</a>'
        '</div>'
    )

    # ── 7. Download buttons (NaijaDeleys YELLOW) ─────────────────────────
    if download_links:
        section_label = 'Download Episodes' if is_series else 'Download Movie'
        parts.append(
            f'<div style="background:{C_DARK};border-radius:10px;'
            f'padding:18px 20px;margin:0 0 20px 0;">'
            f'<h3 style="color:{C_YELLOW};font-size:14px;font-weight:700;'
            f'text-transform:uppercase;letter-spacing:.8px;margin:0 0 14px 0;">'
            f'&#11015; {section_label}</h3>'
            '<div style="display:flex;flex-wrap:wrap;gap:10px;">'
        )
        for i, dl in enumerate(download_links, 1):
            label = dl.get('label') or (f'Episode {i}' if is_series else f'Download {i}')
            url   = dl['url']
            # Alternate: solid yellow / outlined yellow
            if i % 2 == 1:
                btn_style = (
                    f'background:{C_YELLOW};color:#000;border:2px solid {C_YELLOW};'
                )
            else:
                btn_style = (
                    f'background:transparent;color:{C_YELLOW};border:2px solid {C_YELLOW};'
                )
            parts.append(
                f'<a href="{url}" target="_blank" rel="nofollow noopener" '
                f'style="display:inline-flex;align-items:center;gap:6px;{btn_style}'
                f'padding:10px 20px;border-radius:6px;text-decoration:none;'
                f'font-weight:700;font-size:13px;letter-spacing:.3px;'
                f'transition:opacity .2s;">&#11015; {label}</a>'
            )
        parts.append('</div></div>')

    # ── 8. SEO keyword paragraph (visually hidden, screen-reader friendly) ─
    kind     = 'series' if is_series else 'movie'
    seo_bits = [f'Download {title}']
    if year:
        seo_bits.append(f'{title} {year}')
    if genre:
        for g in genre.split(','):
            g = g.strip()
            if g:
                seo_bits.append(f'{g} {kind}')
    if country:
        seo_bits.append(f'{country} {kind}')
    if title_b and is_series:
        seo_bits.append(f'{title} {title_b}')
    seo_bits.append(f'{title} download free')
    seo_text = ', '.join(seo_bits) + '.'

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
      - Series:  "Tyler Perry's Zatima S04 (Episode 14 Added)"
      - Movie:   "Orí: Rebirth (2025)"
      No pipe suffix — categories handle grouping inside WordPress.

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
            cid = _wp_get_or_create_category(cat_name.strip(), headers, wp_base)
            if cid:
                cat_ids.append(cid)

        # ── Title & excerpt ──────────────────────────────────────────────
        # Series: "Show S01 (Episode 14 Added)"
        # Movie:  "Movie Title (2025)"   ← title already contains the year
        if is_series and title_b:
            full_title = f'{title} ({title_b})'
        else:
            full_title = title   # movies: title already looks like "Orí: Rebirth (2025)"

        # Excerpt: episode badge for series, synopsis for movies
        excerpt_text = title_b if (is_series and title_b) else description

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
    help = 'Scrape thenkiri.ng and publish directly to WordPress (no DB, no social media)'

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
                SKIP_KEYS = {'filesize', 'duration', 'imdb', 'status', 'subtitle', 'type'}
                meta_info: dict = {}
                for p in soup.find_all('p'):
                    raw = p.get_text(separator='\n').strip()
                    if any(k in raw.lower() for k in ('genre', 'stars', 'filesize')):
                        for line in raw.splitlines():
                            if ':' not in line:
                                continue
                            key, _, val = line.partition(':')
                            key = key.strip().lower().replace(' ', '_')
                            val = val.strip(' \u2013-\u2014').strip()
                            if val and key not in SKIP_KEYS:
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
                                f"https://thenkiri.ng/wp-json/wp/v2/media/{media_id}",
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
                            f"https://thenkiri.ng/wp-json/wp/v2/categories/{cat_id}",
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