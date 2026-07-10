"""
Attach TMDB's canonical genres to movies as browsable Categories.

The scraped `vi_genre` is unreliable, so genres come straight from TMDB (via the
movie's tmdb_id). TMDB uses slightly different names for film vs TV genres, so we
normalise them into one clean set before linking.
"""
from movies.scraper_utils import get_or_create_category

# Merge TMDB's film/TV genre variants into one clean, browsable set.
# A value of None means "drop it" (not a real browse genre).
GENRE_MAP = {
    'science fiction': 'Sci-Fi',
    'sci-fi & fantasy': 'Sci-Fi',
    'action & adventure': 'Action',
    'war & politics': 'War',
    'kids': 'Family',
    'soap': 'Drama',
    'tv movie': None,
    'reality': None,
    'talk': None,
    'news': None,
}


def _clean(name):
    key = (name or '').strip().lower()
    if key in GENRE_MAP:
        return GENRE_MAP[key]          # may be None → dropped by caller
    return (name or '').strip() or None


def genre_names(genres_str):
    """'Horror, Science Fiction' → ['Horror', 'Sci-Fi'] (deduped, cleaned)."""
    out = []
    for raw in (genres_str or '').split(','):
        g = _clean(raw)
        if g and g not in out:
            out.append(g)
    return out


def link_tmdb_genres(movie, genres_str):
    """Link a movie to genre Categories from TMDB's genre string.

    `movie` is a Movie instance. Returns the number of genre categories linked.
    """
    names = genre_names(genres_str)
    if not names:
        return 0
    cats = [c for c in (get_or_create_category(n) for n in names) if c]
    if cats:
        movie.categories.add(*cats)
    return len(cats)
