"""
Shared fuzzy-title search helpers — used by the website's SearchResultsView
and the Telegram bot's free-text search, so spacing/punctuation variations
("Spider-Man" / "spider man" / "spiderman") all match the same way in both
places, instead of a plain substring match that only finds the exact
punctuation stored in the title.
"""
import re

from django.db.models import Value
from django.db.models.functions import Lower, Replace

# Characters stripped from both the stored title and the user's query before
# comparing — the punctuation people routinely type differently (or skip)
# when searching: "Spider-Man" vs "spider man" vs "spiderman";
# "Mission: Impossible" vs "mission impossible"; etc.
_STRIP_CHARS = (' ', '-', "'", '’', ':', '.', ',', '!', '?')
_STRIP_RE = re.compile('[' + re.escape(''.join(_STRIP_CHARS)) + ']+')


def normalize_for_search(text: str) -> str:
    """Python-side version — normalizes the user's query string."""
    return _STRIP_RE.sub('', text or '').lower()


def normalized_title_expression():
    """DB-side version — a queryset annotation stripping the same characters
    from `title`, so `.filter(norm_title__icontains=normalize_for_search(q))`
    matches regardless of how the query is punctuated/spaced."""
    expr = Lower('title')
    for ch in _STRIP_CHARS:
        expr = Replace(expr, Value(ch), Value(''))
    return expr
