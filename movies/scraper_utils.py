"""
Shared helpers for all scrapers:

  • is_valid_download_url(url)  — reject source pages, ad domains, malformed URLs
  • normalize_title(title)      — canonical key so "From S01" == "From Season 1"
  • find_duplicate_movie(title) — return an existing Movie that is the same title
                                  in a different notation/casing (dedupe on insert)

Keeping this in one place means every scraper stays consistent, and it matches
the one-off DB cleanup that was run.
"""
import re
from urllib.parse import urlparse

# Hosts that are NOT real download links (source/info sites, ad/redirect
# domains, social/video). Anything matching these is dropped.
JUNK_DOWNLOAD_HOSTS = {
    'thenkiri.com', 'asianwiki.com', 'mydramalist.com', 'deloplen.com',
    '9jarocks.net', 't.me', 'youtu.be', 'youtube.com', 'www.youtube.com',
    'bit.ly', 'oladblock.me',
}


def is_valid_download_url(url):
    """True only for plausible file-host download URLs."""
    if not url or not isinstance(url, str):
        return False
    try:
        host = (urlparse(url.strip()).netloc or '').lower()
    except Exception:
        return False
    # malformed: no host, or host isn't a domain (e.g. a title fragment)
    if not host or '.' not in host:
        return False
    # strip a leading "wwwNN." so www42.loadedfiles.org -> loadedfiles.org
    base = re.sub(r'^www\d*\.', '', host)
    for junk in JUNK_DOWNLOAD_HOSTS:
        if base == junk or host == junk or base.endswith('.' + junk):
            return False
    return True


def filter_download_urls(urls):
    """Keep only valid download URLs, de-duplicated, preserving order."""
    seen, out = set(), []
    for u in urls or []:
        if is_valid_download_url(u) and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def normalize_title(title):
    """
    Canonical comparison key. Mirrors the DB cleanup:
      - lowercase
      - "S01"/"s1"/"S01E05" -> "season 1"  (keeps the season number distinct)
      - drop "(complete)" / "completed"
      - collapse all punctuation/whitespace
    So "From S01", "From Season 1", "From S01 (Complete)" -> "from season 1",
    while "Money Heist S01" and "Money Heist S02" stay different.
    """
    if not title:
        return ''
    t = title.lower()
    t = re.sub(r'\bs0*(\d+)(e\d+)?\b', r'season \1', t)   # S01 / S1 / S01E05 -> season 1
    t = re.sub(r'\(?\s*complete[d]?\s*\)?', ' ', t)        # (complete)/(completed)
    t = re.sub(r'[^a-z0-9]+', ' ', t)                      # punctuation/space
    return t.strip()


def parse_show(title):
    """
    Split a title into (show_key, season_number) so every season of a show can
    be grouped under one parent.

      "From S01"          -> ("from", 1)
      "From Season 2"     -> ("from", 2)
      "From S01 (Complete)" -> ("from", 1)
      "From"              -> ("from", None)
      "Scary Movie"       -> ("scary-movie", None)

    show_key is a hyphenated, season-stripped slug (built from normalize_title,
    so it matches the same canonical form used for dedupe). season_number is the
    int season parsed from the title, or None for movies / unseasoned titles.
    """
    norm = normalize_title(title)                       # e.g. "from season 1"
    season = None
    m = re.search(r'\bseason (\d+)\b', norm)
    if m:
        season = int(m.group(1))
        norm = re.sub(r'\bseason \d+\b', ' ', norm)     # strip the season part
    norm = re.sub(r'\s+', ' ', norm).strip()
    key = re.sub(r'\s+', '-', norm)
    return key, season


# Maps the many name variants the scrapers produce to the ONE canonical
# category name (matching the cleaned-up DB). Keys are lowercased.
CATEGORY_ALIASES = {
    'series': 'TV Series', 'tv series': 'TV Series',
    'hollywood': 'Hollywood Movies', 'hollywood movie': 'Hollywood Movies',
    'hollywood movies': 'Hollywood Movies',
    'hollywood tv series': 'Hollywood TV Series',
    'nollywood': 'Nollywood Movies', 'nollywood movie': 'Nollywood Movies',
    'nollywood movies': 'Nollywood Movies',
    'nollywood tv series': 'Nollywood TV Series',
    'k drama': 'Korean Drama', 'korean drama': 'Korean Drama',
    'movie': 'Movies', 'movies': 'Movies',
    '18+ movie': 'Adult (18+)', '18plus': 'Adult (18+)', '18+': 'Adult (18+)',
    'adult': 'Adult (18+)',
    'animation': 'Animation', 'animation movie': 'Animation',
    'bollywood': 'Bollywood Movies', 'bollywood movies': 'Bollywood Movies',
    'filipino': 'Filipino Drama', 'filipino drama': 'Filipino Drama',
    'wrestling': 'Wrestling',
    'other foreign movies': 'Other Foreign Movies',
    'other foreign series': 'Other Foreign Movies',
    'other foreign': 'Other Foreign Movies',
    'chinese drama': 'Chinese Drama',
    'chinese movie': 'Chinese Movies', 'chinese movies': 'Chinese Movies',
    'thai drama': 'Thai Drama', 'turkish drama': 'Turkish Drama',
    'spanish drama': 'Spanish Drama',
    'sa series': 'SA Series', 'south africa': 'South Africa',
    'sci-fi': 'Sci-Fi', 'sci fi': 'Sci-Fi', 'scifi': 'Sci-Fi',
    'reality-tv': 'Reality TV', 'reality tv': 'Reality TV',
    'ongoing': 'Ongoing', 'anime': 'Anime',
}


def canonical_category_name(name):
    """Return the canonical category name for any scraper-supplied variant."""
    raw = (name or '').strip()
    if not raw:
        return ''
    return CATEGORY_ALIASES.get(raw.lower(), raw)


def get_or_create_category(name, model=None):
    """
    Return the existing canonical Category (case-insensitive), creating it only
    if it genuinely doesn't exist. Prevents the "Series" vs "TV Series" and
    "Hollywood movies" vs "Hollywood Movies" duplicate-category problem.
    """
    if model is None:
        from movies.models import Category as model
    canonical = canonical_category_name(name)
    if not canonical:
        return None
    obj = model.objects.filter(name__iexact=canonical).first()
    if obj:
        return obj
    return model.objects.create(name=canonical)


def find_duplicate_movie(title, model=None):
    """
    Return an existing Movie whose normalized title matches `title`, or None.
    Pass the Movie model to avoid an import cycle, or it's imported lazily.
    """
    if model is None:
        from movies.models import Movie as model
    key = normalize_title(title)
    if not key:
        return None
    # Fast path: exact title (case-insensitive) match first.
    exact = model.objects.filter(title__iexact=title).first()
    if exact:
        return exact
    # Fallback: scan candidates sharing the first word (cheap prefix filter),
    # then compare normalized keys in Python.
    first_word = key.split(' ', 1)[0]
    if not first_word:
        return None
    for m in model.objects.filter(title__icontains=first_word).only('id', 'title')[:200]:
        if normalize_title(m.title) == key:
            return m
    return None
