# movies/external_ratings.py
"""
Fetches critic/audience scores for a Movie from external sources:

- IMDb + Rotten Tomatoes: via the OMDb API (free tier, needs OMDB_API_KEY).
- Letterboxd: has no public API, so its average rating is scraped from its
  public film page. This is best-effort — any failure (blocked, page moved,
  markup changed) is swallowed and simply means that source stays empty
  until the next run. Never raises, never blocks page rendering.

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
