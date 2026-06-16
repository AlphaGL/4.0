"""
Thin TheMovieDB (TMDB) client used to enrich scraped titles with rating,
trailer, cast, overview and a clean poster. Needs TMDB_API_KEY in the env.

Cache-friendly: each title is looked up once (the result is stored on the
Movie), so TMDB is barely touched.
"""
import re

import requests
from decouple import config

BASE = 'https://api.themoviedb.org/3'
IMG = 'https://image.tmdb.org/t/p/w500'


def _key():
    return config('TMDB_API_KEY', default='')


def is_configured():
    return bool(_key())


def _get(path, params=None):
    params = dict(params or {})
    params['api_key'] = _key()
    try:
        r = requests.get(f'{BASE}{path}', params=params, timeout=20)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def clean_title(title):
    """Strip scraper junk + season/completion suffixes for a better TMDB match.
    'NKIRI DOWNLOAD Anniversary (2025)' -> 'Anniversary'
    'From S01 (Complete)' -> 'From'."""
    t = title or ''
    t = re.sub(r'^\s*(nkiri|thenkiri|9jarocks|moviebox)\s+download\s+', '', t,
               flags=re.IGNORECASE)
    t = re.sub(r'^\s*download\s+', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s+(s\d{1,2}(e\d+)?|season\s+\d+)\b.*$', '', t,
               flags=re.IGNORECASE)
    t = re.sub(r'\(?\s*complete[d]?\s*\)?', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\(\s*(19|20)\d{2}\s*\)\s*$', '', t)   # trailing (year)
    return re.sub(r'\s+', ' ', t).strip()


def search(title, year=None, is_series=False):
    """Return (tmdb_id, media_type) for the best match, or None."""
    title = clean_title(title)
    if not title:
        return None
    order = ['tv', 'movie'] if is_series else ['movie', 'tv']
    for media in order:
        params = {'query': title}
        if year:
            params['first_air_date_year' if media == 'tv' else 'year'] = year
        data = _get(f'/search/{media}', params)
        results = (data or {}).get('results') or []
        if results:
            return results[0]['id'], media
    return None


def details(tmdb_id, media):
    """Fetch full metadata for a matched title. Returns a dict or None."""
    data = _get(f'/{media}/{tmdb_id}',
                {'append_to_response': 'credits,videos'})
    if not data:
        return None

    # trailer: prefer an official YouTube "Trailer"
    trailer = None
    vids = (data.get('videos') or {}).get('results') or []
    for v in vids:
        if v.get('site') == 'YouTube' and v.get('type') == 'Trailer':
            trailer = f"https://www.youtube.com/watch?v={v['key']}"
            break
    if not trailer:
        for v in vids:
            if v.get('site') == 'YouTube':
                trailer = f"https://www.youtube.com/watch?v={v['key']}"
                break

    cast = ', '.join(
        c['name'] for c in ((data.get('credits') or {}).get('cast') or [])[:8])
    poster = data.get('poster_path')
    rating = data.get('vote_average')
    year = (data.get('release_date') or data.get('first_air_date') or '')[:4]
    runtime = data.get('runtime')
    if not runtime:
        ert = data.get('episode_run_time') or []
        runtime = ert[0] if ert else None

    return {
        'tmdb_id': tmdb_id,
        'rating': round(rating, 1) if rating else None,
        'poster_url': f'{IMG}{poster}' if poster else None,
        'trailer_url': trailer,
        'cast': cast,
        'overview': (data.get('overview') or '').strip(),
        'genres': ', '.join(g['name'] for g in (data.get('genres') or [])),
        'year': year,
        'runtime': str(runtime) if runtime else '',
    }
