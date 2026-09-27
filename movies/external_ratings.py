# movies/external_ratings.py
"""
Fetches critic/audience scores for a Movie from external sources:

- IMDb: via the OMDb API (free tier, needs OMDB_API_KEY).
- Rotten Tomatoes: scraped directly from rottentomatoes.com — its own page
  embeds both Tomatometer (critic) AND Popcornmeter (audience) scores as
  structured JSON, which OMDb's free tier never provided (critic score only).
  Tried first; OMDb's RT figure (fetch_omdb) is only used as a fallback if
  the scrape finds nothing.
- Letterboxd: has no public API, so its average rating is scraped from its
  public film page. As of 2026 Letterboxd sits behind a Cloudflare bot
  challenge that blocks this approach entirely (confirmed: even cloudscraper
  gets a 403) — kept here in case that ever changes, but expect it to return
  None indefinitely.

All of these are best-effort — any failure (blocked, page moved, markup
changed) is swallowed and simply means that source stays empty until the
next run. Never raises, never blocks page rendering.

Called only from the fetch_external_ratings management command (cron), never
from a request/view, so a slow or failing source can't slow down page loads.
"""
import logging
import re
import time
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from decouple import config

logger = logging.getLogger(__name__)

OMDB_URL = 'http://www.omdbapi.com/'
LETTERBOXD_SEARCH_URL = 'https://letterboxd.com/search/films/{}/'
LETTERBOXD_FILM_URL = 'https://letterboxd.com/film/{}/'
IMDB_URL = 'https://www.imdb.com/title/{}/'
RT_SEARCH_URL = 'https://www.rottentomatoes.com/search?search={}'
RT_URL = 'https://www.rottentomatoes.com{}'

REQUEST_TIMEOUT = 12
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)
HEADERS = {'User-Agent': USER_AGENT}


def _omdb_key():
    return config('OMDB_API_KEY', default='')


def fetch_omdb(imdb_id):
    """Return {'imdb': {...}, 'rt': {...}} (either key may be missing) or {}.
    Each value: {'score': float 0-10, 'display': str, 'url': str}."""
    if not imdb_id or not _omdb_key():
        return {}
    try:
        resp = requests.get(
            OMDB_URL,
            params={'i': imdb_id, 'apikey': _omdb_key()},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return {}
        data = resp.json()
        if data.get('Response') != 'True':
            return {}
    except Exception:
        logger.warning('OMDb fetch failed for %s', imdb_id, exc_info=True)
        return {}

    out = {}
    for entry in data.get('Ratings', []):
        source = entry.get('Source', '')
        value = entry.get('Value', '')
        if source == 'Internet Movie Database':
            m = re.match(r'([\d.]+)\s*/\s*10', value)
            if m:
                out['imdb'] = {
                    'score': float(m.group(1)),
                    'display': value,
                    'url': IMDB_URL.format(imdb_id),
                }
        elif source == 'Rotten Tomatoes':
            m = re.match(r'(\d+)\s*%', value)
            if m:
                out['rt'] = {
                    'score': round(float(m.group(1)) / 10, 1),
                    'display': value,
                    'url': '',
                }
    return out


def fetch_rotten_tomatoes(title, year=None, is_series=False):
    """Return {'score','display','url','audience_score','audience_display'}
    or None. Scraped directly (not via OMDb) so we get the Popcornmeter
    (audience) figure OMDb's free tier never provides."""
    if not title:
        return None
    try:
        search_resp = requests.get(
            RT_SEARCH_URL.format(quote(title)),
            headers=HEADERS, timeout=REQUEST_TIMEOUT,
        )
        if search_resp.status_code != 200:
            return None
        soup = BeautifulSoup(search_resp.text, 'html.parser')

        media_type = 'tv' if is_series else 'movie'
        path_prefix = '/tv/' if is_series else '/m/'
        rows = soup.find_all('search-page-media-row')
        chosen = None
        for row in rows:
            link = row.find('a', href=True)
            if not link or path_prefix not in link['href']:
                continue
            if year and row.get('release-year') and row['release-year'] != str(year):
                continue  # keep looking for the matching release year
            chosen = link['href']
            if year and row.get('release-year') == str(year):
                break  # exact year match — stop here
        if not chosen and rows:
            # No year match — fall back to the first same-media-type result.
            for row in rows:
                link = row.find('a', href=True)
                if link and path_prefix in link['href']:
                    chosen = link['href']
                    break
        if not chosen:
            return None

        time.sleep(1)  # be polite between the two requests

        film_resp = requests.get(chosen, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if film_resp.status_code != 200:
            return None
        m = re.search(
            r'<script[^>]*id="media-scorecard-json"[^>]*>\s*(\{.*?\})\s*</script>',
            film_resp.text, re.DOTALL)
        if not m:
            return None
        import json as _json
        data = _json.loads(m.group(1))

        result = {}
        critics = data.get('criticsScore') or {}
        if critics.get('scorePercent'):
            pct = int(re.sub(r'\D', '', critics['scorePercent']) or 0)
            result['score'] = round(pct / 10, 1)
            result['display'] = critics['scorePercent']
            result['url'] = chosen

        audience = data.get('audienceScore') or {}
        if audience.get('scorePercent'):
            apct = int(re.sub(r'\D', '', audience['scorePercent']) or 0)
            result['audience_score'] = round(apct / 10, 1)
            result['audience_display'] = audience['scorePercent']

        return result or None
    except Exception:
        logger.warning('Rotten Tomatoes scrape failed for %r', title, exc_info=True)
        return None


def _parse_letterboxd_rating(html):
    """Try a few known ways Letterboxd exposes the average rating; return
    (score_0_to_10, display_str) or None if nothing matched."""
    soup = BeautifulSoup(html, 'html.parser')

    # 1) JSON-LD aggregateRating block.
    for script in soup.find_all('script', type='application/ld+json'):
        text = script.string or ''
        m = re.search(r'"ratingValue"\s*:\s*"?([\d.]+)"?', text)
        if m:
            val = float(m.group(1))
            return round(val * 2, 1), f'{val:.1f}★'

    # 2) twitter:data2 meta tag ("4.12 out of 5").
    meta = soup.find('meta', attrs={'name': 'twitter:data2'})
    if meta and meta.get('content'):
        m = re.search(r'([\d.]+)\s*out of\s*5', meta['content'])
        if m:
            val = float(m.group(1))
            return round(val * 2, 1), f'{val:.1f}★'

    # 3) Fallback: a span with a data-average-rating attribute.
    span = soup.find(attrs={'data-average-rating': True})
    if span:
        try:
            val = float(span['data-average-rating'])
            return round(val * 2, 1), f'{val:.1f}★'
        except (TypeError, ValueError):
            pass

    return None


def fetch_letterboxd(title, year=None):
    """Return {'score': float 0-10, 'display': str, 'url': str} or None."""
    if not title:
        return None
    try:
        search_resp = requests.get(
            LETTERBOXD_SEARCH_URL.format(quote(title)),
            headers=HEADERS, timeout=REQUEST_TIMEOUT,
        )
        if search_resp.status_code != 200:
            return None
        soup = BeautifulSoup(search_resp.text, 'html.parser')
        link = soup.select_one('a[href^="/film/"]')
        if not link or not link.get('href'):
            return None
        slug = link['href'].strip('/').split('/')[-1]

        time.sleep(1)  # be polite between the two requests

        film_resp = requests.get(
            LETTERBOXD_FILM_URL.format(slug),
            headers=HEADERS, timeout=REQUEST_TIMEOUT,
        )
        if film_resp.status_code != 200:
            return None
        parsed = _parse_letterboxd_rating(film_resp.text)
        if not parsed:
            return None
        score, display = parsed
        return {'score': score, 'display': display, 'url': LETTERBOXD_FILM_URL.format(slug)}
    except Exception:
        logger.warning('Letterboxd scrape failed for %r', title, exc_info=True)
        return None
