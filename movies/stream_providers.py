"""
Embeddable streaming providers, keyed off a title's TMDB id.

These are deterministic URL templates — no scraping. A title with a tmdb_id can
get a stream instantly by formatting the template. Swapping a provider (when a
domain dies/rotates) is a one-line change here; nothing else needs to know.

Player chain: streamimdb (primary) → vidlink.pro (fallback, used only when
streamimdb won't play). vidsrc/2embed remain as optional alternates.
"""

# provider key → {'movie': tmpl, 'tv': tmpl}
# Placeholders: {tmdb} {season} {episode}
PROVIDERS = {
    'streamimdb': {
        'movie': 'https://streamimdb.ru/embed/movie/{tmdb}',
        'tv':    'https://streamimdb.ru/embed/tv/{tmdb}',
    },
    'vidlink': {
        'movie': 'https://vidlink.pro/movie/{tmdb}',
        'tv':    'https://vidlink.pro/tv/{tmdb}/{season}/{episode}',
    },
    'vidsrc': {
        'movie': 'https://vidsrc.to/embed/movie/{tmdb}',
        'tv':    'https://vidsrc.to/embed/tv/{tmdb}/{season}/{episode}',
    },
    '2embed': {
        'movie': 'https://www.2embed.cc/embed/{tmdb}',
        'tv':    'https://www.2embed.cc/embedtv/{tmdb}&s={season}&e={episode}',
    },
}

# The fallback chain, in order. Players try each until one plays.
# streamimdb leads (it's the in-house embed); vidlink.pro catches the cases where
# streamimdb is down/slow.
PROVIDER_ORDER = ['streamimdb', 'vidlink']


def build_stream_url(provider, tmdb_id, is_series=False, season=1, episode=1):
    """Return the embed URL for one provider, or '' if unknown/unavailable."""
    if not tmdb_id:
        return ''
    prov = PROVIDERS.get(provider)
    if not prov:
        return ''
    tmpl = prov['tv'] if is_series else prov['movie']
    return tmpl.format(tmdb=tmdb_id, season=season or 1, episode=episode or 1)


def build_stream_chain(tmdb_id, is_series=False, season=1, episode=1,
                       order=None):
    """Ordered list of embed URLs (main → support) for a title's tmdb_id."""
    order = order or PROVIDER_ORDER
    urls = [build_stream_url(p, tmdb_id, is_series, season, episode)
            for p in order]
    return [u for u in urls if u]
