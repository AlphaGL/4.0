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


def trailer(tmdb_id, media):
    """Best YouTube trailer URL for a title, or None."""
    data = _get(f'/{media}/{tmdb_id}/videos')
    vids = (data or {}).get('results') or []
    for want in ('Trailer', None):
        for v in vids:
            if v.get('site') == 'YouTube' and (want is None or v.get('type') == want):
                return f"https://www.youtube.com/watch?v={v['key']}"
    return None


def upcoming(media='movie', pages=1):
    """Genuinely upcoming titles (future release / first-air date)."""
    import datetime
    today = datetime.date.today().isoformat()
    out = []
    for p in range(1, pages + 1):
        if media == 'movie':
            data = _get('/movie/upcoming', {'page': p})
        else:
            data = _get('/discover/tv', {
                'page': p,
                'first_air_date.gte': today,
                'sort_by': 'popularity.desc',
            })
        for it in (data or {}).get('results') or []:
            rel = (it.get('release_date') or it.get('first_air_date') or '')
            if not it.get('id') or not rel or rel < today:
                continue  # must have a future date
            out.append({
                'tmdb_id': it['id'],
                'media': media,
                'title': (it.get('title') or it.get('name') or '').strip(),
                'overview': (it.get('overview') or '').strip(),
                'poster_url': f"{IMG}{it['poster_path']}" if it.get('poster_path') else None,
                'release_date': rel,
                'rating': round(it.get('vote_average') or 0, 1),
            })
    return out


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

    cast_raw = ((data.get('credits') or {}).get('cast') or [])[:10]
    cast = ', '.join(c.get('name', '') for c in cast_raw[:8])
    cast_list = [{
        'tmdb_id': c['id'],
        'name': c.get('name', ''),
        'profile_path': c.get('profile_path'),
        'character': c.get('character') or '',
        'order': c.get('order', i),
    } for i, c in enumerate(cast_raw) if c.get('id') and c.get('name')]
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
        'cast_list': cast_list,
        'overview': (data.get('overview') or '').strip(),
        'genres': ', '.join(g['name'] for g in (data.get('genres') or [])),
        'year': year,
        'runtime': str(runtime) if runtime else '',
    }
